from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import inspect
import logging

import yaml

logger = logging.getLogger(__name__)

MAX_ITERATION_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools."
)

DEFAULT_PROMPT = (
    "Continue autonomously from the current state. Do not repeat completed work. "
    "Stop and summarize if blocked, if approval is required, or before destructive "
    "or externally visible actions."
)


@dataclass
class GatewayContext:
    gateway: Any
    source: Any
    session_key: str
    adapter: Any
    platform: str


@dataclass
class SessionLink:
    parent_id: str | None
    child_end_reason: str | None
    parent_end_reason: str | None


@dataclass
class ContextResolution:
    context: GatewayContext | None
    ancestor_session_ids: tuple[str, ...] = ()
    blocked_reason: str | None = None


class AutoContinuePlugin:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.max_auto_continues = max(0, int(cfg.get("max_auto_continues", 3) or 0))
        self.prompt = str(cfg.get("prompt") or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
        self.platforms = _normalize_platform_allowlist(cfg.get("platforms"))
        self._contexts: dict[str, GatewayContext] = {}
        self._counts: dict[str, int] = {}

    @classmethod
    def from_runtime_config(cls, plugin_config_path: str | Path | None = None) -> "AutoContinuePlugin":
        path = Path(plugin_config_path) if plugin_config_path is not None else Path(__file__).resolve().parent / "config.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            logger.debug("auto-continue: could not load plugin config from %s", path, exc_info=True)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return cls(raw)

    def pre_gateway_dispatch(self, *, event: Any, gateway: Any, session_store: Any, **_: Any) -> None:
        if not self.enabled:
            return
        source = getattr(event, "source", None)
        if source is None:
            return
        platform = _platform_name(getattr(source, "platform", ""))
        if not self._platform_enabled(platform):
            return
        try:
            session_entry = session_store.get_or_create_session(source)
        except Exception:
            logger.debug("auto-continue: could not resolve session", exc_info=True)
            return
        session_id = str(getattr(session_entry, "session_id", "") or "")
        session_key = str(getattr(session_entry, "session_key", "") or "")
        if not session_id or not session_key:
            return
        adapter = _adapter_for(gateway, getattr(source, "platform", None))
        if adapter is None:
            return
        self._contexts[session_id] = GatewayContext(
            gateway=gateway,
            source=source,
            session_key=session_key,
            adapter=adapter,
            platform=platform,
        )

    def post_llm_call(
        self,
        *,
        session_id: str,
        conversation_history: list[dict[str, Any]] | None = None,
        assistant_response: str = "",
        platform: str = "",
        **_: Any,
    ) -> None:
        if not self.enabled or self.max_auto_continues <= 0:
            return
        sid = str(session_id or "")
        if not sid:
            return
        if not _is_max_iteration_summary_turn(conversation_history or []):
            self._counts.pop(sid, None)
            return

        resolution = self._context_for_session(sid)
        context = resolution.context
        platform_name = _platform_name(platform or (context.platform if context else ""))
        if context is not None and platform_name and context.platform != platform_name:
            return
        if not self._platform_enabled(platform_name):
            return

        if context is None:
            logger.info(
                "auto-continue: no gateway context for session %s reason=%s",
                sid,
                resolution.blocked_reason or "missing_context",
            )
            return

        goal_session_ids = {sid, *resolution.ancestor_session_ids}
        if any(self._active_goal_exists(goal_sid) for goal_sid in goal_session_ids):
            logger.info("auto-continue: skipping session %s because /goal is active", sid)
            return

        used = self._counts.get(sid, 0)
        if used >= self.max_auto_continues:
            logger.info(
                "auto-continue: session %s reached bound %d/%d",
                sid,
                used,
                self.max_auto_continues,
            )
            return

        event = _make_message_event(text=self.prompt, source=context.source)
        enqueue = getattr(context.gateway, "_enqueue_fifo", None)
        if not callable(enqueue):
            logger.warning("auto-continue: gateway has no _enqueue_fifo; cannot continue")
            return
        enqueue(context.session_key, event, context.adapter)
        current_count = used + 1
        self._counts[sid] = current_count
        self._register_visible_notice(context, self._format_notice(current_count, context.platform))
        logger.info(
            "auto-continue: queued continuation for session %s (%d/%d)",
            sid,
            current_count,
            self.max_auto_continues,
        )

    def _format_notice(self, current_count: int, platform: str) -> str:
        return (
            f"{_notice_emoji_for_platform(platform)} "
            f"Injected auto-continue prompt ({current_count}/{self.max_auto_continues}):\n{self.prompt}"
        )

    def _adapter_generation(self, context: GatewayContext) -> int | None:
        try:
            active = getattr(context.adapter, "_active_sessions", {}).get(context.session_key)
            generation = getattr(active, "_hermes_run_generation", None)
            if generation is not None:
                return int(generation)
        except Exception:
            pass
        try:
            generations = getattr(context.gateway, "_session_run_generation", {})
            generation = generations.get(context.session_key)
            return int(generation) if generation is not None else None
        except Exception:
            return None

    async def _send_visible_notice(self, context: GatewayContext, notice: str) -> None:
        metadata = None
        metadata_for_source = getattr(context.gateway, "_thread_metadata_for_source", None)
        if callable(metadata_for_source):
            try:
                metadata = metadata_for_source(context.source)
            except Exception:
                logger.debug("auto-continue: could not build notice metadata", exc_info=True)
        try:
            await context.adapter.send(context.source.chat_id, notice, metadata=metadata)
        except Exception:
            logger.warning("auto-continue: visible notice send failed", exc_info=True)

    def _schedule_visible_notice(self, context: GatewayContext, notice: str) -> None:
        loop = getattr(context.gateway, "_gateway_loop", None)
        if loop is None:
            logger.info("auto-continue: skipping visible notice because gateway loop is unavailable")
            return

        coro = self._send_visible_notice(context, notice)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            logger.warning("auto-continue: could not schedule visible notice", exc_info=True)

    def _unwrap_post_delivery_entry(self, entry: Any) -> tuple[int | None, Any | None]:
        if isinstance(entry, tuple) and len(entry) == 2:
            generation, callback = entry
            try:
                return int(generation), callback
            except Exception:
                return None, callback
        return None, entry if callable(entry) else None

    def _register_visible_notice(self, context: GatewayContext, notice: str) -> None:
        callbacks = getattr(context.adapter, "_post_delivery_callbacks", None)
        register = getattr(context.adapter, "register_post_delivery_callback", None)
        generation = self._adapter_generation(context)

        if generation is None and (callbacks is not None or callable(register)):
            logger.info("auto-continue: no run generation for post-delivery notice; sending visible notice immediately")
            self._schedule_visible_notice(context, notice)
            return

        existing_generation = None
        existing_callback = None
        if isinstance(callbacks, dict):
            existing_generation, existing_callback = self._unwrap_post_delivery_entry(callbacks.get(context.session_key))

        effective_generation = generation if generation is not None else existing_generation

        async def _after_delivery() -> None:
            if callable(existing_callback):
                try:
                    result = existing_callback()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.debug("auto-continue: existing post-delivery callback failed", exc_info=True)
            await self._send_visible_notice(context, notice)

        try:
            if isinstance(callbacks, dict) and callable(existing_callback):
                if existing_generation is not None and effective_generation is not None:
                    if effective_generation < existing_generation:
                        logger.info("auto-continue: skipping stale visible notice callback registration")
                        return
                    if effective_generation == existing_generation:
                        callbacks[context.session_key] = (effective_generation, _after_delivery)
                        return
                    existing_callback = None
                    callbacks[context.session_key] = (effective_generation, _after_delivery)
                    return
                callbacks[context.session_key] = (
                    (effective_generation, _after_delivery)
                    if effective_generation is not None
                    else _after_delivery
                )
                return
            if callable(register):
                register(context.session_key, _after_delivery, generation=effective_generation)
                return
        except TypeError:
            try:
                if callable(register):
                    register(context.session_key, _after_delivery)
                    return
            except Exception:
                pass
        except Exception:
            logger.warning("auto-continue: could not register visible notice callback", exc_info=True)

        self._schedule_visible_notice(context, notice)

    def command(self, args: str = "") -> str:
        arg = (args or "").strip().lower()
        if arg in {"reset", "clear"}:
            self._counts.clear()
            self._contexts.clear()
            return "Reset hermes-auto-continue state."
        if arg not in {"", "status"}:
            return "Usage: /auto-continue [status|reset]"
        if not self._counts:
            return "No auto-continue sessions are currently counted."
        lines = ["Auto-continue sessions:"]
        for sid, count in sorted(self._counts.items()):
            lines.append(f"- {sid}: {count}/{self.max_auto_continues}")
        return "\n".join(lines)

    def _platform_enabled(self, platform: str) -> bool:
        if not platform:
            return False
        if self.platforms is None:
            return True
        return platform in self.platforms

    def _active_goal_exists(self, session_id: str) -> bool:
        try:
            from hermes_cli.goals import GoalManager

            return GoalManager(session_id=session_id).is_active()
        except Exception:
            return False

    def _context_for_session(self, session_id: str) -> ContextResolution:
        context = self._contexts.get(session_id)
        visited: set[str] = {session_id}
        ancestors: list[str] = []
        current = session_id

        while True:
            link = self._session_link(current)
            parent = link.parent_id
            if not parent:
                if context is not None:
                    self._migrate_counts(session_id, ancestors)
                    return ContextResolution(context=context, ancestor_session_ids=tuple(ancestors))
                return ContextResolution(context=None, blocked_reason="no_parent")
            if parent in visited:
                if context is not None:
                    self._migrate_counts(session_id, ancestors)
                    return ContextResolution(context=context, ancestor_session_ids=tuple(ancestors))
                return ContextResolution(context=None, blocked_reason="cycle")
            if not _is_compression_link(link):
                if context is not None:
                    self._migrate_counts(session_id, ancestors)
                    return ContextResolution(context=context, ancestor_session_ids=tuple(ancestors))
                return ContextResolution(context=None, blocked_reason="non_compression_parent")

            visited.add(parent)
            ancestors.append(parent)
            parent_context = self._contexts.get(parent)
            if parent_context is not None and context is None:
                context = parent_context
                self._contexts[session_id] = context
                logger.info(
                    "auto-continue: recovered gateway context for session %s from compression ancestor %s",
                    session_id,
                    parent,
                )
            current = parent

    def _migrate_counts(self, session_id: str, ancestors: list[str]) -> None:
        inherited = self._counts.get(session_id, 0)
        for ancestor in ancestors:
            inherited = max(inherited, self._counts.pop(ancestor, 0))
        if inherited:
            self._counts[session_id] = inherited

    def _state_db_path(self) -> Path:
        try:
            from hermes_constants import get_hermes_home

            return get_hermes_home() / "state.db"
        except Exception:
            return Path.home() / ".hermes" / "state.db"

    def _session_link(self, session_id: str) -> SessionLink:
        try:
            import sqlite3

            db_path = self._state_db_path()
            if not db_path.exists():
                logger.info(
                    "auto-continue: state db not found while resolving parent for session %s",
                    session_id,
                )
                return SessionLink(parent_id=None, child_end_reason=None, parent_end_reason=None)
            with sqlite3.connect(db_path) as con:
                row = con.execute(
                    """
                    select child.parent_session_id, child.end_reason, parent.end_reason
                    from sessions child
                    left join sessions parent on parent.id = child.parent_session_id
                    where child.id = ?
                    """,
                    (session_id,),
                ).fetchone()
            if not row:
                return SessionLink(parent_id=None, child_end_reason=None, parent_end_reason=None)
            parent, child_end_reason, parent_end_reason = row
            return SessionLink(
                parent_id=str(parent) if parent else None,
                child_end_reason=str(child_end_reason) if child_end_reason else None,
                parent_end_reason=str(parent_end_reason) if parent_end_reason else None,
            )
        except Exception:
            logger.info("auto-continue: parent lookup failed for session %s", session_id, exc_info=True)
            return SessionLink(parent_id=None, child_end_reason=None, parent_end_reason=None)


def _is_compression_link(link: SessionLink) -> bool:
    return link.child_end_reason == "compression" or link.parent_end_reason == "compression"


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def _notice_emoji_for_platform(platform: Any) -> str:
    if _platform_name(platform) == "slack":
        return ":robot_face:"
    return "🤖"


def _normalize_platform_allowlist(raw_platforms: Any) -> set[str] | None:
    if raw_platforms is None:
        return None
    if isinstance(raw_platforms, dict):
        allowed = {
            _platform_name(platform)
            for platform, platform_cfg in raw_platforms.items()
            if not isinstance(platform_cfg, dict) or bool(platform_cfg.get("enabled", True))
        }
        return allowed or set()
    if isinstance(raw_platforms, (list, tuple, set)):
        return {_platform_name(platform) for platform in raw_platforms if _platform_name(platform)}
    return None


def _adapter_for(gateway: Any, platform: Any) -> Any:
    adapters = getattr(gateway, "adapters", None)
    if not adapters:
        return None
    try:
        adapter = adapters.get(platform)
        if adapter is not None:
            return adapter
    except Exception:
        pass
    platform_name = _platform_name(platform)
    for key, adapter in getattr(adapters, "items", lambda: [])():
        if _platform_name(key) == platform_name:
            return adapter
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _is_max_iteration_summary_turn(history: list[dict[str, Any]]) -> bool:
    if len(history) < 2:
        return False
    for msg in reversed(history[-6:]):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        if _message_text(msg.get("content")) == MAX_ITERATION_SUMMARY_REQUEST:
            return True
    return False


def _make_message_event(*, text: str, source: Any) -> Any:
    try:
        from gateway.platforms.base import MessageEvent, MessageType

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=None,
            channel_prompt=None,
        )
    except Exception:
        from datetime import datetime
        from types import SimpleNamespace

        return SimpleNamespace(
            text=text,
            message_type="text",
            source=source,
            raw_message=None,
            message_id=None,
            platform_update_id=None,
            media_urls=[],
            media_types=[],
            reply_to_message_id=None,
            reply_to_text=None,
            auto_skill=None,
            channel_prompt=None,
            channel_context=None,
            internal=False,
            timestamp=datetime.now(),
        )


_PLUGIN = AutoContinuePlugin.from_runtime_config()


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _PLUGIN.pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", _PLUGIN.post_llm_call)
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="auto-continue",
            description="Show or reset hermes-auto-continue gateway state.",
            handler=_PLUGIN.command,
        )

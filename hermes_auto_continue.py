from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging

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


class AutoContinuePlugin:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.max_auto_continues = max(0, int(cfg.get("max_auto_continues", 3) or 0))
        self.prompt = str(cfg.get("prompt") or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
        self.platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
        self._contexts: dict[str, GatewayContext] = {}
        self._counts: dict[str, int] = {}

    @classmethod
    def from_runtime_config(cls) -> "AutoContinuePlugin":
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            logger.debug("auto-continue: could not load Hermes config", exc_info=True)
            cfg = {}
        plugin_cfg = cfg.get("hermes_auto_continue") or cfg.get("auto_continue") or {}
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = {}
        return cls(plugin_cfg)

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
        context = self._contexts.get(sid)
        platform_name = _platform_name(platform or (context.platform if context else ""))
        if not self._platform_enabled(platform_name):
            return

        if not _is_max_iteration_summary_turn(conversation_history or []):
            self._counts.pop(sid, None)
            return

        if context is None:
            logger.debug("auto-continue: no gateway context for session %s", sid)
            return
        if self._active_goal_exists(sid):
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
        self._counts[sid] = used + 1
        logger.info(
            "auto-continue: queued continuation for session %s (%d/%d)",
            sid,
            used + 1,
            self.max_auto_continues,
        )

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
        cfg = self.platforms.get(platform)
        if isinstance(cfg, dict) and "enabled" in cfg:
            return bool(cfg.get("enabled"))
        return True

    def _active_goal_exists(self, session_id: str) -> bool:
        try:
            from hermes_cli.goals import GoalManager

            return GoalManager(session_id=session_id).is_active()
        except Exception:
            return False


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


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
        from gateway.session import MessageEvent, MessageType

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=None,
            channel_prompt=None,
        )
    except Exception:
        return type(
            "AutoContinueMessageEvent",
            (),
            {
                "text": text,
                "message_type": "text",
                "source": source,
                "message_id": None,
                "channel_prompt": None,
            },
        )()


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

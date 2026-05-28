from __future__ import annotations

from pathlib import Path
import types

import pytest

from hermes_auto_continue import AutoContinuePlugin, MAX_ITERATION_SUMMARY_REQUEST, SessionLink, _make_message_event


ALL_PLATFORM_CONFIG = {
    "platforms": [
        "telegram",
        "discord",
        "slack",
        "whatsapp",
        "signal",
        "matrix",
        "mattermost",
        "email",
        "sms",
        "dingtalk",
        "wecom",
        "weixin",
        "feishu",
        "qqbot",
        "bluebubbles",
        "yuanbao",
        "webhook",
        "api_server",
        "homeassistant",
    ]
}


class FakeSessionEntry:
    def __init__(self, session_id: str = "session-1", session_key: str = "slack:chat:thread"):
        self.session_id = session_id
        self.session_key = session_key


class FakeSessionStore:
    def __init__(self, entry: FakeSessionEntry | None = None):
        self.entry = entry or FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry


class FakeSessionLinkLookup:
    def __init__(self, links: dict[str, tuple[str | None, str | None, str | None]]):
        self.links = links

    def link_for(self, session_id: str):
        parent_id, child_end_reason, parent_end_reason = self.links.get(session_id, (None, None, None))
        return SessionLink(
            parent_id=parent_id,
            child_end_reason=child_end_reason,
            parent_end_reason=parent_end_reason,
        )


class FakeGateway:
    def __init__(self):
        self.adapters = {"slack": object()}
        self.enqueued = []

    def _enqueue_fifo(self, session_key, event, adapter):
        self.enqueued.append((session_key, event, adapter))


def make_event(text: str = "hello", platform: str = "slack"):
    source = types.SimpleNamespace(
        platform=platform,
        chat_id="C123",
        thread_id="177",
        user_id="U123",
        user_name="Ryo",
    )
    return types.SimpleNamespace(text=text, source=source, message_id="m1")


def max_iteration_history(summary: str = "I reached the limit; here is progress so far."):
    return [
        {"role": "user", "content": "do a long task"},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": MAX_ITERATION_SUMMARY_REQUEST},
        {"role": "assistant", "content": summary},
    ]


def test_config_example_is_valid_and_matches_runtime_shape():
    config_example = Path("config.example.yaml")
    assert config_example.exists()

    plugin = AutoContinuePlugin.from_runtime_config(plugin_config_path=config_example)

    assert plugin.enabled is True
    assert plugin.max_auto_continues == 3
    assert plugin.prompt
    assert plugin._platform_enabled("slack") is True
    assert plugin._platform_enabled("telegram") is True


def test_reads_config_from_plugin_directory_config_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "enabled: true\n"
        "max_auto_continues: 7\n"
        "prompt: Local config prompt.\n"
        "platforms:\n"
        "  - slack\n"
        "  - telegram\n",
        encoding="utf-8",
    )

    plugin = AutoContinuePlugin.from_runtime_config(plugin_config_path=config_path)

    assert plugin.enabled is True
    assert plugin.max_auto_continues == 7
    assert plugin.prompt == "Local config prompt."
    assert plugin._platform_enabled("slack") is True
    assert plugin._platform_enabled("telegram") is True
    assert plugin._platform_enabled("discord") is False


def test_platforms_array_is_an_allowlist():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed.", **ALL_PLATFORM_CONFIG})
    gateway = FakeGateway()
    gateway.adapters["matrix"] = object()
    gateway.adapters["sms"] = object()
    store = FakeSessionStore()
    matrix_event = make_event(platform="matrix")
    sms_event = make_event(platform="sms")
    unknown_event = make_event(platform="mastodon")

    plugin.pre_gateway_dispatch(event=matrix_event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="matrix",
    )
    assert len(gateway.enqueued) == 1

    plugin.pre_gateway_dispatch(event=sms_event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="sms",
    )
    assert len(gateway.enqueued) == 2

    plugin.pre_gateway_dispatch(event=unknown_event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="mastodon",
    )
    assert len(gateway.enqueued) == 2


def test_records_gateway_context_and_enqueues_continuation_after_max_iteration_summary():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore()
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert len(gateway.enqueued) == 1
    session_key, queued_event, adapter = gateway.enqueued[0]
    assert session_key == "slack:chat:thread"
    assert adapter is gateway.adapters["slack"]
    assert queued_event.text == "Proceed."
    assert queued_event.source is event.source
    assert queued_event.message_id is None
    assert queued_event.channel_prompt is None
    assert queued_event.media_urls == []
    assert queued_event.media_types == []
    assert queued_event.reply_to_message_id is None
    assert queued_event.reply_to_text is None
    assert queued_event.raw_message is None
    assert queued_event.platform_update_id is None
    assert queued_event.auto_skill is None
    assert queued_event.channel_context is None
    assert queued_event.internal is False
    assert queued_event.timestamp is not None


def test_make_message_event_is_compatible_with_gateway_inbound_preparation():
    from gateway.platforms.base import MessageEvent

    source = types.SimpleNamespace(platform="slack")

    event = _make_message_event(text="Proceed.", source=source)

    assert isinstance(event, MessageEvent)
    assert event.text == "Proceed."
    assert event.source is source
    assert event.message_id is None
    assert event.channel_prompt is None
    assert event.media_urls == []
    assert event.media_types == []
    assert event.reply_to_message_id is None
    assert event.reply_to_text is None
    assert event.raw_message is None
    assert event.platform_update_id is None
    assert event.auto_skill is None
    assert event.channel_context is None
    assert event.internal is False
    assert event.timestamp is not None


def test_make_message_event_fallback_has_gateway_compatible_fields(monkeypatch):
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gateway.platforms.base":
            raise ImportError("forced fallback")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    source = types.SimpleNamespace(platform="slack")

    event = _make_message_event(text="Proceed.", source=source)

    assert event.text == "Proceed."
    assert event.source is source
    assert event.media_urls == []
    assert event.media_types == []
    assert event.reply_to_message_id is None
    assert event.reply_to_text is None
    assert event.raw_message is None
    assert event.platform_update_id is None
    assert event.auto_skill is None
    assert event.channel_prompt is None
    assert event.channel_context is None
    assert event.internal is False
    assert event.timestamp is not None

    second = _make_message_event(text="Again.", source=source)
    assert second.media_urls == []
    assert second.media_urls is not event.media_urls


@pytest.mark.asyncio
async def test_synthetic_event_survives_prepare_inbound_message_text_no_media():
    from gateway.run import GatewayRunner

    source = types.SimpleNamespace(
        platform="slack",
        chat_id="C123",
        thread_id="177",
        user_id="U123",
        user_name="Ryo",
        chat_type="channel",
    )
    event = _make_message_event(text="Proceed.", source=source)
    fake_runner = types.SimpleNamespace(
        config=types.SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False),
        adapters={},
        _pending_native_image_paths_by_session={},
        _session_key_for_source=lambda _source: "slack:C123:177",
        _consume_pending_native_image_paths=lambda _session_key: None,
    )

    message = await GatewayRunner._prepare_inbound_message_text(
        fake_runner,
        event=event,
        source=source,
        history=[],
    )

    assert "Proceed." in message


def test_recovers_gateway_context_from_compression_parent_session():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", None, "compression")}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert len(gateway.enqueued) == 1
    session_key, queued_event, adapter = gateway.enqueued[0]
    assert session_key == "slack:chat:thread"
    assert queued_event.text == "Proceed."
    assert adapter is gateway.adapters["slack"]
    assert plugin._counts["new-session"] == 1
    assert "new-session" in plugin._contexts


def test_does_not_recover_gateway_context_from_non_compression_parent_session():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", "branch", None)}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
    assert "new-session" not in plugin._contexts


def test_compression_parent_recovery_preserves_auto_continue_bound():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 1, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._counts["old-session"] = 1
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", None, "compression")}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
    assert plugin._counts["new-session"] == 1
    assert "old-session" not in plugin._counts


def test_skips_when_compression_parent_has_active_goal(monkeypatch):
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", None, "compression")}).link_for
    monkeypatch.setattr(plugin, "_active_goal_exists", lambda session_id: session_id == "old-session")

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []


def test_skips_when_intermediate_compression_ancestor_has_active_goal(monkeypatch):
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup(
        {
            "new-session": ("mid-session", None, "compression"),
            "mid-session": ("old-session", None, "compression"),
        }
    ).link_for
    monkeypatch.setattr(plugin, "_active_goal_exists", lambda session_id: session_id == "mid-session")

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []


def test_recovers_gateway_context_when_child_row_marks_compression(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as con:
        con.execute("create table sessions (id text primary key, parent_session_id text, end_reason text)")
        con.execute(
            "insert into sessions (id, parent_session_id, end_reason) values (?, ?, ?)",
            ("parent-session", None, None),
        )
        con.execute(
            "insert into sessions (id, parent_session_id, end_reason) values (?, ?, ?)",
            ("child-session", "parent-session", "compression"),
        )

    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="parent-session", session_key="slack:chat:thread"))
    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=store)
    monkeypatch.setattr(plugin, "_state_db_path", lambda: db_path)

    plugin.post_llm_call(
        session_id="child-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert len(gateway.enqueued) == 1
    assert gateway.enqueued[0][0] == "slack:chat:thread"


def test_session_link_reads_parent_and_parent_end_reason_from_state_db(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as con:
        con.execute("create table sessions (id text primary key, parent_session_id text, end_reason text)")
        con.execute(
            "insert into sessions (id, parent_session_id, end_reason) values (?, ?, ?)",
            ("parent-session", None, "compression"),
        )
        con.execute(
            "insert into sessions (id, parent_session_id, end_reason) values (?, ?, ?)",
            ("child-session", "parent-session", None),
        )

    plugin = AutoContinuePlugin({"enabled": True})
    monkeypatch.setattr(plugin, "_state_db_path", lambda: db_path)

    link = plugin._session_link("child-session")

    assert link.parent_id == "parent-session"
    assert link.parent_end_reason == "compression"


def test_cached_child_context_still_checks_compression_parent_goal(monkeypatch):
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    old_store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    new_store = FakeSessionStore(FakeSessionEntry(session_id="new-session", session_key="slack:chat:thread"))

    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=old_store)
    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=new_store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", None, "compression")}).link_for
    monkeypatch.setattr(plugin, "_active_goal_exists", lambda session_id: session_id == "old-session")

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []


def test_cached_child_context_inherits_compression_parent_count_bound():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 1, "prompt": "Proceed."})
    gateway = FakeGateway()
    old_store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    new_store = FakeSessionStore(FakeSessionEntry(session_id="new-session", session_key="slack:chat:thread"))

    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=old_store)
    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=new_store)
    plugin._counts["old-session"] = 1
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", None, "compression")}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
    assert plugin._counts["new-session"] == 1
    assert "old-session" not in plugin._counts


def test_recovers_gateway_context_through_long_compression_chain():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="session-0", session_key="slack:chat:thread"))
    plugin.pre_gateway_dispatch(event=make_event(), gateway=gateway, session_store=store)
    links = {
        f"session-{i}": (f"session-{i - 1}", None, "compression")
        for i in range(1, 11)
    }
    plugin._session_link = FakeSessionLinkLookup(links).link_for

    plugin.post_llm_call(
        session_id="session-10",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert len(gateway.enqueued) == 1


def test_enforces_per_session_auto_continue_bound():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 1, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore()
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    for _ in range(2):
        plugin.post_llm_call(
            session_id="session-1",
            conversation_history=max_iteration_history(),
            assistant_response="summary",
            platform="slack",
        )

    assert len(gateway.enqueued) == 1


def test_does_not_enqueue_for_non_max_iteration_turn_and_resets_count_after_normal_turn():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 1, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore()
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=[{"role": "user", "content": "normal"}, {"role": "assistant", "content": "done"}],
        assistant_response="done",
        platform="slack",
    )
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert len(gateway.enqueued) == 2


def test_skips_disabled_platform_and_active_goal(monkeypatch):
    plugin = AutoContinuePlugin(
        {
            "enabled": True,
            "max_auto_continues": 2,
            "prompt": "Proceed.",
            "platforms": {"slack": {"enabled": False}},
        }
    )
    gateway = FakeGateway()
    store = FakeSessionStore()
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )
    assert gateway.enqueued == []

    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    monkeypatch.setattr(plugin, "_active_goal_exists", lambda session_id: True)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )
    assert gateway.enqueued == []


def test_command_status_and_reset():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore()
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin.post_llm_call(
        session_id="session-1",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert "session-1" in plugin.command("status")
    assert "1/2" in plugin.command("status")
    assert "Reset" in plugin.command("reset")
    assert "No auto-continue sessions" in plugin.command("status")

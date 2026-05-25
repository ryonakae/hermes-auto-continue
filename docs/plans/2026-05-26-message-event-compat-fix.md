# MessageEvent Compatibility Fix Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Fix `hermes-auto-continue` so the synthetic continuation event queued after a max-iteration summary is accepted by the current Hermes gateway instead of failing with missing `media_urls`.

**Architecture:** Keep the plugin gateway-only and avoid Hermes core changes. Make `_make_message_event()` construct the real current gateway `MessageEvent` when available, with a fallback that mirrors the current `MessageEvent` field contract closely enough for gateway FIFO processing. Add regression tests that prove queued continuation events have the media/reply/channel/internal attributes required by `_prepare_inbound_message_text()` and nearby gateway paths.

**Tech Stack:** Python, pytest, Hermes standalone plugin API, Hermes gateway `gateway.platforms.base.MessageEvent`.

---

## Evidence from live logs

Live gateway failure after the plugin queued a continuation:

```text
2026-05-25 23:41:53,823 INFO ... auto-continue: queued continuation for session 20260525_222013_f100b8 (1/3)
2026-05-25 23:41:54,425 ERROR gateway.run: Agent error in session agent:main:slack:group:C0ATJKG7AER:1779667436.813699
AttributeError: 'AutoContinueMessageEvent' object has no attribute 'media_urls'
```

Local import probe:

```text
gateway.session import failed: ImportError cannot import name 'MessageEvent' from 'gateway.session'
base import ok <class 'gateway.platforms.base.MessageEvent'>
```

Root cause: `_make_message_event()` imports `MessageEvent` from `gateway.session`, which is stale for the current Hermes checkout. The import fails, so the plugin falls back to a minimal dynamic object missing `media_urls`, `media_types`, and reply/raw fields expected by the gateway.

---

## Non-goals

- Do not modify `~/.hermes/hermes-agent` core in this slice.
- Do not change auto-continue policy, prompt, platform allowlist, or max count behavior.
- Do not make CLI/ACP auto-continue work in this slice; README already scopes v0 to gateway.
- Do not bypass approval/destructive-action boundaries.

---

## Task 1: Add a failing regression test for gateway-compatible queued events

**Objective:** Prove that a continuation event enqueued by the plugin exposes the fields current gateway code reads.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Add assertions to the existing enqueue test**

In `test_records_gateway_context_and_enqueues_continuation_after_max_iteration_summary`, after the existing assertions for `queued_event.text`, `source`, `message_id`, and `channel_prompt`, add:

```python
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
```

**Step 2: Add a focused direct factory test**

Add imports and test:

```python
from hermes_auto_continue import AutoContinuePlugin, MAX_ITERATION_SUMMARY_REQUEST, _make_message_event
```

```python
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
```

**Step 3: Run targeted test and verify failure**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
PYTHONPATH=/Users/ryo.nakae/.hermes/hermes-agent:. "$HERMES_PY" -m pytest tests/test_auto_continue.py::test_make_message_event_is_compatible_with_gateway_inbound_preparation -q
```

Expected before implementation: FAIL with missing `media_urls` or another missing event attribute.

---

## Task 2: Construct the current Hermes gateway MessageEvent first

**Objective:** Make `_make_message_event()` use the real current gateway event dataclass when Hermes source is importable.

**Files:**
- Modify: `hermes_auto_continue.py`

**Step 1: Replace the stale import path in `_make_message_event()`**

Change the `try` block from:

```python
    try:
        from gateway.session import MessageEvent, MessageType
```

to:

```python
    try:
        from gateway.platforms.base import MessageEvent, MessageType
```

Keep the constructor call shape:

```python
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=None,
            channel_prompt=None,
        )
```

Current Hermes `gateway.platforms.base.MessageEvent` provides defaults for `raw_message`, `media_urls`, `media_types`, `reply_to_message_id`, and `reply_to_text`.

**Step 2: Run the focused factory test**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
PYTHONPATH=/Users/ryo.nakae/.hermes/hermes-agent:. "$HERMES_PY" -m pytest tests/test_auto_continue.py::test_make_message_event_is_compatible_with_gateway_inbound_preparation -q
```

Expected: PASS.

---

## Task 3: Harden the fallback event shape without broad abstraction

**Objective:** If Hermes internals move again and the import fails, keep the fallback safe for current gateway preparation paths.

**Files:**
- Modify: `hermes_auto_continue.py`

**Step 1: Replace fallback dynamic class with per-instance fallback object**

The fallback must not use class-level mutable lists. Add a local `SimpleNamespace`/`datetime` fallback that creates new lists per event:

```python
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
```

`message_type="text"` is acceptable only in the fallback because the normal path should return the real `MessageType.TEXT`. The fallback is a last-resort compatibility object, not a replacement for the real dataclass.

Do not add behavior, methods, queues, or platform-specific special cases.

**Step 2: Add a fallback-specific unit test**

Use monkeypatching to force import failure cleanly. Add:

```python
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
```

**Step 3: Add a lightweight gateway preparation smoke test**

Add an async smoke test only for the no-media synthetic event path. Keep it minimal and do not instantiate the full gateway runner:

```python
@pytest.mark.asyncio
async def test_synthetic_event_survives_prepare_inbound_message_text_no_media():
    from gateway.run import GatewayRunner

    source = types.SimpleNamespace(
        platform="slack",
        chat_id="C123",
        thread_id="177",
        user_id="U123",
        user_name="Ryo",
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
```

If this test needs one more no-op helper because Hermes core changed, add only that helper to `fake_runner` and keep the test scoped to the no-media synthetic event path. Do not build a full gateway fixture for this bug.

**Step 4: Run fallback and smoke tests**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
PYTHONPATH=/Users/ryo.nakae/.hermes/hermes-agent:. "$HERMES_PY" -m pytest \
  tests/test_auto_continue.py::test_make_message_event_fallback_has_gateway_compatible_fields \
  tests/test_auto_continue.py::test_synthetic_event_survives_prepare_inbound_message_text_no_media \
  -q
```

Expected: PASS.

---

## Task 4: Run plugin validation and discovery smoke

**Objective:** Verify the fix did not break plugin behavior, syntax, or discovery.

**Files:**
- No new files.

**Step 1: Run plugin test suite**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
PYTHONPATH=/Users/ryo.nakae/.hermes/hermes-agent:. "$HERMES_PY" -m pytest tests/test_auto_continue.py -q
```

Expected: all tests pass.

**Step 2: Run py_compile**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
PYTHONPATH=/Users/ryo.nakae/.hermes/hermes-agent:. "$HERMES_PY" -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Expected: exit code 0.

**Step 3: Run plugin discovery smoke from Hermes checkout**

Run:

```bash
cd /Users/ryo.nakae/.hermes/hermes-agent
HERMES_PY=/Users/ryo.nakae/.hermes/hermes-agent/.venv/bin/python
[ -x "$HERMES_PY" ] || HERMES_PY=python3
"$HERMES_PY" - <<'PY'
from hermes_cli.plugins import PluginManager
pm = PluginManager()
pm.discover_and_load(force=True)
loaded = pm._plugins.get('hermes-auto-continue')
print('found=', bool(loaded))
print('enabled=', getattr(loaded, 'enabled', None))
print('error=', getattr(loaded, 'error', None))
print('hooks=', sorted(getattr(loaded, 'hooks_registered', []) or []))
PY
```

Expected:

```text
found= True
enabled= True
error= None
hooks= ['post_llm_call', 'pre_gateway_dispatch']
```

---

## Task 5: Commit, deploy locally, and verify runtime readiness

**Objective:** Preserve the fix and make clear that live Slack behavior requires gateway restart and a future max-iteration event to verify end-to-end.

**Files:**
- Commit modified plugin files and this plan if desired.

**Step 1: Review diff**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
git diff -- tests/test_auto_continue.py hermes_auto_continue.py docs/plans/2026-05-26-message-event-compat-fix.md
git status --short --branch
```

Expected: only planned files are modified/added.

**Step 2: Commit**

Run:

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-auto-continue
git add hermes_auto_continue.py tests/test_auto_continue.py docs/plans/2026-05-26-message-event-compat-fix.md
git commit -m "fix: create gateway-compatible auto-continue events"
```

**Step 3: Restart gateway after implementation**

After implementation is committed, restart via Ryo's normal gateway wrapper/slash path. Do not restart from inside Safehouse unless explicitly directed.

**Step 4: Runtime verification after restart**

Use Slack command after restart:

```text
@Hermes /auto-continue status
```

Expected: command is recognized. Counts may be empty until the next max-iteration summary.

For the next natural max-iteration event, verify logs:

```bash
grep -iE 'auto-continue: queued continuation|AttributeError: .*media_urls|Agent error in session' ~/.hermes/logs/gateway.log ~/.hermes/logs/errors.log | tail -80
```

Expected after fix:

- `auto-continue: queued continuation ...` appears when max iteration summary is detected.
- No `AttributeError: 'AutoContinueMessageEvent' object has no attribute 'media_urls'` appears.
- A subsequent synthetic user turn should appear with the configured prompt, unless the session is blocked by `/goal`, count bound, platform allowlist, or approval/destructive-action boundary.

---

## Risks and mitigations

- **Risk:** Hermes moves `MessageEvent` again.  
  **Mitigation:** fallback event has gateway-compatible fields and tests cover it.

- **Risk:** The import succeeds in local tests but live gateway runs a different Hermes checkout.  
  **Mitigation:** discovery smoke should be run from `/Users/ryo.nakae/.hermes/hermes-agent`; live behavior still requires gateway restart.

- **Risk:** Synthetic continuation queues correctly but another downstream attribute is missing.  
  **Mitigation:** regression test covers fields observed in `_prepare_inbound_message_text()`; if another live error appears, add the exact missing attribute as a focused regression, not a broad fake event framework.

- **Risk:** The first max-iteration event after fix is skipped because `/goal` is active or `max_auto_continues` bound is reached.  
  **Mitigation:** use `/auto-continue reset` if counts are stale; inspect logs for `because /goal is active` or `reached bound` before diagnosing as failure.

---

## Completion criteria

- `tests/test_auto_continue.py` includes regression coverage for real/fallback event fields.
- `_make_message_event()` imports `MessageEvent` from `gateway.platforms.base` first.
- fallback synthetic event has `media_urls`, `media_types`, `reply_to_message_id`, `reply_to_text`, `raw_message`, `platform_update_id`, `auto_skill`, `channel_prompt`, `channel_context`, `internal`, and `timestamp`.
- fallback creates per-instance `media_urls` / `media_types` lists, not shared class-level mutable lists.
- lightweight `_prepare_inbound_message_text()` smoke test passes for no-media synthetic events.
- Plugin tests pass.
- py_compile passes.
- plugin discovery smoke reports loaded/enabled hooks.
- Gateway is restarted after implementation before live Slack verification.

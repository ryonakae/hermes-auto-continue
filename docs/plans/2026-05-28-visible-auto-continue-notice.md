# Visible Auto-Continue Notice Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make every successful `hermes-auto-continue` injected continuation visible to the user with a short platform message.

**Architecture:** Keep continuation control and user visibility separate. The plugin should continue to enqueue the synthetic continuation with `gateway._enqueue_fifo(...)`, then send a side notice through the captured platform adapter. The side notice must not be queued as a `MessageEvent` and must not become another LLM input.

**Tech Stack:** Python standalone Hermes plugin, gateway hook callbacks, `asyncio.run_coroutine_threadsafe`, pytest.

**Execution status:** Implemented in this slice. Focused tests cover visible notice text, count display, thread metadata, notice-send failure, and no-enqueue behavior.

**Verification:** `python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py`; `python -m pytest tests/test_auto_continue.py -q`; plugin discovery smoke from `~/.hermes/hermes-agent`.

---

## Decisions from the design thread

- Continue using the existing plugin-only design; do not edit Hermes core for this slice.
- The continuation input remains an internal FIFO enqueue of the configured prompt.
- The user-visible notification is a separate direct platform send via `adapter.send(...)`.
- Do not rely on Slack/Discord/Telegram echoing the plugin's own visible message back into Hermes.
- Send the notice only after continuation enqueue succeeds.
- Notification failure must not cancel or roll back the queued continuation.
- The visible message format is:

  ```text
  🤖 Injected auto-continue prompt (1/3):
  <config prompt>
  ```

- `(1/3)` means `used_count_after_enqueue / max_auto_continues` for the current session.
- The prompt body must be the actual runtime prompt from plugin config, including custom local `config.yaml` overrides.

## Non-goals

- Do not add platform-specific message formatting branches unless a failing test proves they are needed.
- Do not make the notice ephemeral/private-only; it should appear in the same conversation/thread/topic as the active session.
- Do not put the visible notice into the transcript as a user message.
- Do not add a new config option for the format in this slice.

---

### Task 1: Add focused notification tests

**Objective:** Capture the agreed behavior before implementation.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Extend fakes for visible sends and thread metadata**

Update `FakeGateway` and/or add a fake adapter so tests can observe direct `send(...)` calls independently from `_enqueue_fifo(...)` calls.

Suggested shape:

```python
class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return types.SimpleNamespace(success=True, message_id="notice-1")
```

Add `FakeGateway._thread_metadata_for_source(...)` returning a sentinel metadata dict, e.g. `{"thread_id": source.thread_id}`.

**Step 2: Write test for the visible notice content**

Add a test that:

1. Creates `AutoContinuePlugin({"enabled": True, "max_auto_continues": 3, "prompt": "Proceed carefully."})`.
2. Captures gateway context with `pre_gateway_dispatch(...)`.
3. Calls `post_llm_call(...)` with `max_iteration_history()`.
4. Verifies one FIFO continuation was enqueued.
5. Verifies one direct adapter notice was sent.
6. Asserts the notice is exactly:

```text
🤖 Injected auto-continue prompt (1/3):
Proceed carefully.
```

**Step 3: Write test that counts advance across repeated continuations**

Call `post_llm_call(...)` twice for the same session and assert the second notice starts with:

```text
🤖 Injected auto-continue prompt (2/3):
```

**Step 4: Write test that thread metadata is used**

Assert the fake adapter receives metadata from `gateway._thread_metadata_for_source(source)`, not a hand-built platform-specific dict.

**Step 5: Write test that notification failure does not block continuation**

Use a fake adapter whose `send(...)` raises. Assert:

- `_enqueue_fifo(...)` still receives the synthetic continuation.
- `plugin._counts[session_id]` is incremented.
- No exception escapes `post_llm_call(...)`.

**Step 6: Write test that no notice is sent when enqueue cannot happen**

Use a fake gateway without callable `_enqueue_fifo`. Assert no direct notice is sent and no count is incremented.

**Step 7: Run tests to verify RED**

Run from plugin root:

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: the new visible-notice tests fail because the plugin does not send notices yet.

---

### Task 2: Implement side-notice scheduling

**Objective:** Send the agreed notice after a continuation is successfully queued.

**Files:**
- Modify: `hermes_auto_continue.py`

**Step 1: Add imports**

Add `import asyncio` near the existing imports.

**Step 2: Add a notice builder helper**

Add a small helper near the plugin class or as a private method:

```python
def _format_notice(self, used: int) -> str:
    return f"🤖 Injected auto-continue prompt ({used}/{self.max_auto_continues}):\n{self.prompt}"
```

Use the count after enqueue (`used + 1`).

**Step 3: Add a scheduling helper**

Add a method that schedules an async send on the gateway loop:

```python
def _schedule_visible_notice(self, context: GatewayContext, notice: str) -> None:
    loop = getattr(context.gateway, "_gateway_loop", None)
    if loop is None:
        logger.info("auto-continue: skipping visible notice because gateway loop is unavailable")
        return

    async def _send_notice() -> None:
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

    try:
        asyncio.run_coroutine_threadsafe(_send_notice(), loop)
    except Exception:
        logger.warning("auto-continue: could not schedule visible notice", exc_info=True)
```

Keep this best-effort. It must not raise into `post_llm_call(...)`.

**Step 4: Call the helper after enqueue/count update**

In `post_llm_call(...)`, after:

```python
enqueue(context.session_key, event, context.adapter)
self._counts[sid] = used + 1
```

build and schedule the notice:

```python
current_count = used + 1
self._counts[sid] = current_count
self._schedule_visible_notice(context, self._format_notice(current_count))
```

Do not call this before `enqueue(...)` succeeds.

**Step 5: Run focused tests**

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: all tests pass.

---

### Task 3: Handle tests without a real running loop cleanly

**Objective:** Keep test code deterministic even though production uses `run_coroutine_threadsafe`.

**Files:**
- Modify: `tests/test_auto_continue.py`
- Modify only if needed: `hermes_auto_continue.py`

**Step 1: Decide the test strategy**

Prefer one of these simple approaches:

- Provide a real event loop in the fake gateway and await scheduled tasks in async tests.
- Or monkeypatch `asyncio.run_coroutine_threadsafe` to run/capture the coroutine deterministically.

Use the smallest approach that keeps tests clear.

**Step 2: Avoid sleeping-based tests**

Do not use arbitrary `sleep(...)` in unit tests. Await explicit futures/tasks or monkeypatch the scheduler.

**Step 3: Re-run focused tests**

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: deterministic pass.

---

### Task 4: Update docs and config comments

**Objective:** Document the visible-notice behavior and the agreed format.

**Files:**
- Modify: `README.md`
- Modify: `config.example.yaml` if it already documents `max_auto_continues` / `prompt`
- Modify: `docs/plans/2026-05-28-visible-auto-continue-notice.md` progress section after implementation

**Step 1: Update README behavior section**

Add a short note:

```markdown
When the plugin injects a continuation, it also posts a visible side notice in the same gateway conversation:

```text
🤖 Injected auto-continue prompt (1/3):
<configured prompt>
```

The notice is not queued as user input; the actual continuation is still delivered internally through the gateway FIFO.
```

**Step 2: Keep config wording aligned**

If `config.example.yaml` comments mention `max_auto_continues`, clarify that the count is shown in the visible notice and is used as the loop-safety bound.

**Step 3: Run docs-safe checks**

```bash
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
python -m pytest tests/test_auto_continue.py -q
```

Expected: compile and tests pass.

---

### Task 5: Verify plugin discovery and runtime readiness

**Objective:** Confirm a fresh Hermes process can still discover the standalone plugin after the change.

**Files:**
- No code changes expected.

**Step 1: Run compile and unit tests from plugin root**

```bash
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
python -m pytest tests/test_auto_continue.py -q
```

Expected: success.

**Step 2: Run plugin discovery smoke from Hermes checkout**

```bash
cd ~/.hermes/hermes-agent
python - <<'PY'
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

or equivalent hook ordering.

**Step 3: Check git diff**

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only intended plugin/doc/test files changed.

---

### Task 6: Commit and operational note

**Objective:** Preserve the change and make the runtime rollout requirement explicit.

**Files:**
- Commit all intended changes.

**Step 1: Commit**

```bash
git add hermes_auto_continue.py tests/test_auto_continue.py README.md config.example.yaml docs/plans/2026-05-28-visible-auto-continue-notice.md
git commit -m "feat: show auto-continue injected prompt notices"
```

If `config.example.yaml` is unchanged, omit it from `git add`.

**Step 2: Push if requested by the user**

```bash
git push origin main
```

Only push when Ryo asks for push or when continuing the existing repo convention explicitly requires it.

**Step 3: Report runtime rollout**

Tell the user:

- The plugin now sends a visible side notice after successful auto-continue enqueue.
- The notice format is `🤖 Injected auto-continue prompt (N/max):` plus the configured prompt.
- The visible notice is not treated as user input.
- Gateway restart is required for the live Slack/Telegram/etc. process to load the updated plugin code.

---

## Acceptance criteria

- A successful auto-continue enqueue posts exactly one visible side notice.
- The notice header is exactly `🤖 Injected auto-continue prompt (N/max):`.
- The body is the actual configured plugin prompt.
- The notice uses gateway thread/topic metadata when available.
- The notice is not enqueued as a synthetic `MessageEvent`.
- Notification failure does not prevent continuation or count increment.
- No notice is sent if continuation enqueue cannot happen.
- Existing session-split context recovery and `/goal` blocking behavior remain intact.
- Focused plugin tests and compile checks pass.

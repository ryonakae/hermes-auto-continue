# hermes-auto-continue Post-Delivery Visible Notice Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after review approval.

**Goal:** Ensure the user-visible auto-continue notice is delivered only after the assistant's max-iteration summary has fully finished on gateway platforms, including Slack long-message split cases.

**Architecture:** Keep `hermes-auto-continue` standalone and gateway-only. Continue to detect the max-iteration summary turn in `post_llm_call`, enqueue the synthetic continuation immediately, and increment the bounded continuation count. Change only visible-notice timing: register a generation-aware post-delivery callback on the current adapter so the notice is sent after the main assistant response has completed delivery. Where possible, make the callback await the notice send before the queued follow-up turn starts.

**Tech Stack:** Python 3.11, Hermes standalone plugin hooks, gateway platform adapter post-delivery callbacks, `asyncio`, pytest.

**Execution status:** Implemented and validated. Unit tests, py_compile, plugin discovery smoke, and `git diff --check` passed on 2026-05-28.

---

## Background / Problem

`hermes-auto-continue` currently works and sends a readable visible notice. The issue is ordering when the max-iteration summary is long enough for Slack/Hermes delivery to split it across visible messages.

Current undesirable order:

```text
1. assistant summary chunk A
2. :robot_face: Injected auto-continue prompt (...)
3. assistant summary chunk B
```

Desired order:

```text
1. assistant summary chunk A
2. assistant summary chunk B
3. :robot_face: Injected auto-continue prompt (...)
```

The current plugin schedules the notice directly in `post_llm_call` using `asyncio.run_coroutine_threadsafe(...)`. That races with streaming/final delivery cleanup. `hermes-context-notifier` avoids this class of UX issue by registering a post-delivery callback and sending/editing after the main assistant response has been delivered.

## External Review Incorporated

Codex reviewed the draft twice in read-only mode. The plan now incorporates these findings:

1. **Generation-aware tests must model Hermes base adapter behavior.** A fake that only stores `generation` is not enough; tests need a `pop_post_delivery_callback(..., generation=...)` path that refuses wrong-generation callbacks.
2. **Generation lookup must not rely only on `adapter._active_sessions`.** Use `adapter._active_sessions[session_key]._hermes_run_generation` first, then `gateway._session_run_generation[session_key]` as a fallback.
3. **A scheduled fire-and-forget notice can race the queued follow-up turn.** Prefer an awaitable post-delivery callback that awaits the notice send, because gateway cleanup awaits awaitable callback results. If an adapter/callback path cannot preserve awaitability, explicitly document and test that fallback as best-effort only.
4. **Add a direct split-order regression.** The tests should model `summary chunk A`, `summary chunk B`, then post-delivery callback notice, not only manual callback invocation.
5. **Do not bypass BasePlatformAdapter stale-generation ownership.** If an existing callback is registered for a different generation, mirror Base behavior: older registrations do not overwrite newer callbacks; newer registrations replace older callbacks without running them; only same-generation callbacks are chained.

## Scope

In scope:

- Modify only `~/.hermes/plugins/hermes-auto-continue`.
- Keep the auto-continue FIFO enqueue timing unchanged.
- Defer only the visible notice send until post-delivery.
- Preserve the existing notice format and platform emoji behavior:
  - Slack: `:robot_face: Injected auto-continue prompt (1/3):\n...`
  - Non-Slack: `🤖 Injected auto-continue prompt (1/3):\n...`
- Preserve continuation bounds and `/goal` skip behavior.
- Preserve best-effort notice delivery: notice send failure must not cancel the queued continuation.
- Add focused regression tests before implementation.
- Update docs to document post-delivery notice timing and gateway restart requirement.

Out of scope:

- Do not edit Hermes core.
- Do not implement context-notifier-style edit-in-place for auto-continue notices.
- Do not add platform history lookups.
- Do not add config options for notice timing or format.
- Do not change the continuation prompt semantics.
- Do not broaden CLI/ACP support.

## Design Decisions

1. **Use post-delivery callback when available.**
   - Preferred lifecycle: adapter callback registered under `session_key`, generation-aware where possible.
   - The callback fires after the main assistant response has been delivered by gateway cleanup / in-band drain.

2. **Preserve awaitability for the normal BasePlatformAdapter path.**
   - Gateway cleanup calls the popped callback and awaits the result if it is awaitable.
   - Therefore the auto-continue notice callback should be `async def` and should `await adapter.send(...)` directly.
   - Do not make the normal callback merely schedule a background coroutine; that can let the next queued continuation response overtake the notice.

3. **Handle existing callbacks carefully.**
   - `BasePlatformAdapter.register_post_delivery_callback(...)` chains callbacks with a synchronous wrapper. That wrapper does not await awaitable inner results.
   - If an existing callback is already registered and `adapter._post_delivery_callbacks` is available, the plugin may directly replace the slot with its own async callback, but it must mirror Base generation ownership:
     1. if the existing callback has a newer generation than this run, do not overwrite it and do not send a stale notice;
     2. if this run has a newer generation than the existing callback, replace the old callback without running it;
     3. if both callbacks are same-generation, chain them by running the existing callback, awaiting it if needed, then awaiting the auto-continue notice send.
   - If direct callback-slot access is unavailable, fall back to `register_post_delivery_callback(...)` and document that this is best-effort for strict follow-up ordering.

4. **Fallback remains immediate scheduling only when no post-delivery path exists.**
   - Some test doubles or future adapters may not expose callback registration or callback storage.
   - In that case, keep a best-effort immediate send fallback rather than dropping visibility.
   - This fallback does not guarantee the Slack split-order fix; it is only for unsupported adapters.

5. **Use the current run generation when possible.**
   - First source: `context.adapter._active_sessions[context.session_key]._hermes_run_generation`.
   - Second source: `context.gateway._session_run_generation[context.session_key]`.
   - If neither exists and the adapter has Base-like generation-aware pop semantics, avoid registering a bare callback that would never fire under `pop_post_delivery_callback(..., generation=run_generation)`. Prefer best-effort immediate scheduling with an INFO log.

6. **Register after enqueue/count update.**
   - If `_enqueue_fifo(...)` fails or is unavailable, no notice should be registered.
   - The notice count should reflect the successful enqueue count.

7. **Do not queue the notice as a `MessageEvent`.**
   - The notice is user-visible status, not LLM input.

## Acceptance Criteria

1. A successful max-iteration auto-continue registers an awaitable post-delivery notice callback instead of sending the notice immediately when the adapter supports a safe callback path.
2. Executing that callback sends the exact existing notice text to the same chat/thread/topic metadata.
3. A Base-like fake adapter refuses wrong-generation callback pops, and tests prove the plugin registers with the active generation.
4. If generation cannot be resolved for a Base-like callback path, the plugin does not register a bare callback that will be stranded; it falls back to best-effort immediate send and logs the reason.
5. A split-order regression records visible delivery order as `summary chunk A`, `summary chunk B`, `notice`.
6. If the adapter lacks any safe post-delivery path, the plugin still schedules/sends the notice through the existing best-effort fallback.
7. If continuation enqueue cannot happen, no post-delivery callback is registered and no notice is sent.
8. Notice send failure does not prevent continuation count increment or queued continuation.
9. Repeated continuations still show `(1/N)`, `(2/N)`, etc.
10. Existing compression-parent context recovery tests still pass.
11. Focused validation passes:
    - `python -m pytest tests/test_auto_continue.py -q`
    - `python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py`
12. Plugin discovery smoke from `~/.hermes/hermes-agent` still shows `hermes-auto-continue` enabled with `pre_gateway_dispatch` and `post_llm_call` hooks registered.
13. Final implementation report says the live gateway must be restarted for Slack to load changed plugin code.

---

## Task 1: Add Base-like fake adapter callback semantics

**Objective:** Make tests model Hermes post-delivery generation ownership closely enough to catch stale callback bugs.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Extend `FakeAdapter` with callback storage and Base-like pop**

Suggested code:

```python
class FakeAdapter:
    def __init__(self, *, fail_send: bool = False):
        self.fail_send = fail_send
        self.sent = []
        self.deliveries = []
        self._post_delivery_callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        self.deliveries.append(content)
        return types.SimpleNamespace(success=True, message_id=f"msg-{len(self.sent)}")

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self._post_delivery_callbacks[session_key] = (
            (int(generation), callback) if generation is not None else callback
        )

    def pop_post_delivery_callback(self, session_key, *, generation=None):
        entry = self._post_delivery_callbacks.get(session_key)
        if entry is None:
            return None
        if isinstance(entry, tuple) and len(entry) == 2:
            entry_generation, callback = entry
            if generation is not None and int(entry_generation) != int(generation):
                return None
            self._post_delivery_callbacks.pop(session_key, None)
            return callback if callable(callback) else None
        if generation is not None:
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return entry if callable(entry) else None
```

**Step 2: Add helper to execute a post-delivery callback**

```python
def run_post_delivery_callback(adapter, session_key="slack:chat:thread", *, generation=None):
    callback = adapter.pop_post_delivery_callback(session_key, generation=generation)
    assert callable(callback)
    result = callback()
    if inspect.isawaitable(result):
        asyncio.run(result)
```

Add `import inspect` if needed.

**Step 3: Add active generation setup helper**

```python
def set_active_generation(adapter, session_key="slack:chat:thread", generation=7):
    adapter._active_sessions[session_key] = types.SimpleNamespace(_hermes_run_generation=generation)
```

**Step 4: Run existing tests**

Run:

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected before implementation: existing immediate-send notice tests may need expectation changes in the next task. Do not change production code yet.

---

## Task 2: Add RED tests for deferred notice ordering and generation ownership

**Objective:** Capture the ordering requirement and stale-generation safety before implementation.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Add explicit post-delivery registration test**

```python
def test_registers_visible_notice_for_post_delivery_after_successful_continuation_enqueue():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 3, "prompt": "Proceed carefully."})
    adapter = FakeAdapter()
    set_active_generation(adapter, generation=7)
    gateway = FakeGateway(adapter=adapter)
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
    assert adapter.sent == []
    assert "slack:chat:thread" in adapter._post_delivery_callbacks

    # Wrong generation must not fire or pop the callback.
    assert adapter.pop_post_delivery_callback("slack:chat:thread", generation=6) is None
    assert "slack:chat:thread" in adapter._post_delivery_callbacks

    run_post_delivery_callback(adapter, generation=7)

    assert adapter.sent == [
        {
            "chat_id": "C123",
            "content": ":robot_face: Injected auto-continue prompt (1/3):\nProceed carefully.",
            "reply_to": None,
            "metadata": {"thread_id": "177"},
        }
    ]
```

**Step 2: Add gateway generation fallback test**

Use an adapter with empty `_active_sessions` and a fake gateway exposing `_session_run_generation = {"slack:chat:thread": 9}`. Assert the callback is registered with generation 9 and fires only when popped with generation 9.

**Step 3: Add existing-callback generation ownership tests**

Add three focused tests that mirror `BasePlatformAdapter.register_post_delivery_callback(...)` semantics:

1. **Same generation chains:** pre-register an existing callback at generation 7, trigger auto-continue at generation 7, execute the callback, and assert the existing callback fires before the notice.
2. **Existing newer generation is preserved:** pre-register an existing callback at generation 8, trigger auto-continue at generation 7, and assert the existing generation-8 callback remains in `_post_delivery_callbacks` unchanged and no stale notice is sent.
3. **Current newer generation replaces old:** pre-register an existing callback at generation 6, trigger auto-continue at generation 7, execute generation 7, and assert the old callback did not fire while the notice did.

**Step 4: Add no-generation fallback test**

Use a Base-like fake adapter with no active generation and a fake gateway without `_session_run_generation`. Assert the plugin does not leave a bare callback that `pop_post_delivery_callback(..., generation=7)` cannot pop. Expected behavior should be immediate best-effort send fallback.

**Step 5: Add split-order regression**

Create a fake adapter helper that records visible deliveries:

```python
async def send_summary_chunks(adapter):
    adapter.deliveries.append("summary chunk A")
    adapter.deliveries.append("summary chunk B")
```

Test flow:

1. Trigger `post_llm_call(...)`.
2. Assert no notice before post-delivery callback.
3. Simulate summary chunks by appending/sending chunk A and chunk B.
4. Execute the post-delivery callback with the right generation.
5. Assert `adapter.deliveries == ["summary chunk A", "summary chunk B", ":robot_face: Injected auto-continue prompt (1/3):\nProceed."]`.

This proves the plugin-level ordering contract without calling Slack APIs.

**Step 6: Add repeated continuation deferred-notice test**

For repeated continuations, execute the callback after each post-LLM call and assert notices are sent only after callback execution:

```python
assert adapter.sent == []
run_post_delivery_callback(adapter, generation=7)
assert adapter.sent[-1]["content"].startswith(":robot_face: Injected auto-continue prompt (1/3):\n")

plugin.post_llm_call(...)
assert len(adapter.sent) == 1
run_post_delivery_callback(adapter, generation=7)
assert adapter.sent[-1]["content"].startswith(":robot_face: Injected auto-continue prompt (2/3):\n")
```

**Step 7: Update no-enqueue/no-callback regression**

Update `test_does_not_send_visible_notice_when_enqueue_cannot_happen` to also assert no callback was registered:

```python
assert getattr(adapter, "_post_delivery_callbacks", {}) == {}
```

**Step 8: Run focused RED tests**

Run the new tests by name. Expected before implementation: they fail because the current plugin sends immediately and does not register a callback.

---

## Task 3: Implement awaitable post-delivery notice send

**Objective:** Register a safe post-delivery callback that awaits the notice send before the queued follow-up turn can proceed.

**Files:**
- Modify: `hermes_auto_continue.py`

**Step 1: Add generation lookup helper**

```python
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
```

**Step 2: Split immediate scheduling from awaitable send**

Keep `_schedule_visible_notice(...)` as a fallback, but add an awaitable send helper:

```python
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
```

Then have `_schedule_visible_notice(...)` call this helper via `asyncio.run_coroutine_threadsafe(...)` for fallback-only paths.

**Step 3: Add callback unwrap helper**

Use local helpers similar to context-notifier but preserve awaitability:

```python
def _unwrap_post_delivery_entry(self, entry: Any) -> tuple[int | None, Any | None]:
    if isinstance(entry, tuple) and len(entry) == 2:
        generation, callback = entry
        try:
            return int(generation), callback
        except Exception:
            return None, callback
    return None, entry if callable(entry) else None
```

**Step 4: Add `_register_visible_notice(...)`**

Implementation shape:

```python
def _register_visible_notice(self, context: GatewayContext, notice: str) -> None:
    callbacks = getattr(context.adapter, "_post_delivery_callbacks", None)
    register = getattr(context.adapter, "register_post_delivery_callback", None)
    generation = self._adapter_generation(context)

    if generation is None and callbacks is not None and callable(getattr(context.adapter, "pop_post_delivery_callback", None)):
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
                # This run is newer than the existing callback. Match Base behavior:
                # replace the old callback without running it.
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
```

Add `import inspect` if using `inspect.isawaitable`.

Important constraints:

- Do not call `BasePlatformAdapter.register_post_delivery_callback(...)` with an existing callback when strict awaitability matters; its sync chaining wrapper can swallow awaitable inner results.
- Do not let callback registration failure escape `post_llm_call(...)`.
- Do not queue the notice as a `MessageEvent`.

**Step 5: Call `_register_visible_notice(...)` after enqueue/count update**

Replace:

```python
self._schedule_visible_notice(context, self._format_notice(current_count, context.platform))
```

with:

```python
self._register_visible_notice(context, self._format_notice(current_count, context.platform))
```

**Step 6: Run focused tests**

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: new post-delivery tests pass.

---

## Task 4: Cover fallback and failure behavior

**Objective:** Preserve current robustness for adapters without callback support and notice-send failures.

**Files:**
- Modify: `tests/test_auto_continue.py`
- Modify only if tests reveal a gap: `hermes_auto_continue.py`

**Step 1: Add adapter-without-callback fake**

Create a fake adapter class that has `send(...)` but no `register_post_delivery_callback(...)`, no `_post_delivery_callbacks`, and no `pop_post_delivery_callback(...)`.

**Step 2: Test immediate fallback for adapters without callback support**

Assert a successful enqueue still produces the existing notice text by the best-effort immediate path.

**Step 3: Update send-failure test**

`test_visible_notice_send_failure_does_not_block_continuation` should execute the post-delivery callback before asserting the send failure path completed:

```python
assert len(gateway.enqueued) == 1
run_post_delivery_callback(adapter, generation=7)
assert plugin._counts["session-1"] == 1
assert adapter.sent == []
```

**Step 4: Run full focused test file**

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: all tests pass.

---

## Task 5: Update docs and operational notes

**Objective:** Make the new ordering behavior clear for future maintenance and operations.

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/plans/2026-05-28-post-delivery-visible-notice.md`

**Step 1: Update `AGENTS.md` implementation notes**

Add a bullet near the visible-notice/private-internals notes:

```markdown
- Visible auto-continue notices are registered as post-delivery callbacks when adapters support a safe generation-aware callback path, so long assistant summaries finish before the notice appears. The normal path awaits the notice send before the queued follow-up turn starts. Do not queue the notice as a `MessageEvent`.
```

**Step 2: Update `README.md` behavior section**

Document that the visible notice is posted after the assistant summary delivery completes when the platform adapter supports post-delivery callbacks. Keep the text short.

**Step 3: Mark this plan implemented only after validation**

Do not change `Execution status` to implemented until tests and py_compile pass.

---

## Task 6: Validate plugin behavior

**Objective:** Verify the slice from unit tests through Hermes plugin discovery.

**Files:**
- No source edits unless validation reveals failures.

**Step 1: Run plugin tests**

From plugin root:

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: all tests pass.

**Step 2: Run py_compile**

```bash
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Expected: no output and exit code 0.

**Step 3: Run plugin discovery smoke**

From Hermes checkout:

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

**Step 4: Check git diff**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. Changed files should be limited to the plugin implementation, tests, docs, and this plan.

---

## Task 7: Commit and report

**Objective:** Preserve the reviewed implementation slice and tell the operator how to activate it.

**Files:**
- Commit changed files after validation.

**Step 1: Commit**

```bash
git add hermes_auto_continue.py tests/test_auto_continue.py AGENTS.md README.md docs/plans/2026-05-28-post-delivery-visible-notice.md
git commit -m "fix: auto-continue通知をpost-deliveryまで遅延"
```

**Step 2: Final report**

Report:

- Changed behavior: visible notice is now post-delivery when supported.
- Verification commands and pass/fail results.
- Any unsupported-adapter fallback behavior.
- Gateway restart required before Slack uses the new plugin code.
- Whether implementation was committed.

## Review Checklist

- [x] The plan does not edit Hermes core.
- [x] Notice ordering is fixed without changing continuation semantics.
- [x] Tests prove no immediate notice is sent when a safe post-delivery callback path exists.
- [x] Tests prove callback generation ownership is respected.
- [x] Tests include a split-order regression: summary chunk A, summary chunk B, notice.
- [x] Tests prove fallback still sends a notice when callback support is absent.
- [x] Callback registration is generation-aware where possible.
- [x] Callback body can be awaitable and awaits the send in the normal path.
- [x] Existing compression/session-split behavior remains covered.
- [x] Docs mention gateway restart.

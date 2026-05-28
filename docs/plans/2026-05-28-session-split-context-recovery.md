# hermes-auto-continue Session Split Context Recovery Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make `hermes-auto-continue` queue a continuation when a max-iteration summary completes under a new compressed session id, while preserving bounded gateway-only behavior and `/goal` non-competition.

**Architecture:** Keep the plugin standalone and gateway-only. The plugin already captures gateway context in `pre_gateway_dispatch` and reacts in `post_llm_call`; extend only the context lookup path so `post_llm_call(session_id=<new child>)` can recover the gateway context captured for a **compression ancestor** after Hermes session split. Do not change Hermes core.

**Tech Stack:** Python 3.11, pytest, Hermes standalone plugin hooks, Hermes profile-aware `state.db` session lineage, gateway FIFO `_enqueue_fifo`.

---

## Background / Evidence

Observed live failure on 2026-05-28:

- `hermes-auto-continue` is enabled and registers both hooks:
  - `pre_gateway_dispatch`
  - `post_llm_call`
- Live target turn reached the cap:
  - `Turn ended: reason=max_iterations_reached(90/90) ... session=20260528_094342_c7a406`
- The same turn also split sessions during compression:
  - `Session split detected: 20260528_081513_19f3fc → 20260528_094342_c7a406 (compression)`
- There was no `auto-continue: queued continuation...` log line.

Current plugin behavior stores gateway context by the session id returned during `pre_gateway_dispatch`. If Hermes later reports the final turn through `post_llm_call` using a compressed child session id, `self._contexts.get(sid)` misses and the plugin silently skips.

## External Review Incorporated

Codex reviewed the first draft in read-only mode. The plan now incorporates these review findings:

1. **Compression-only recovery:** do not treat every `parent_session_id` as a safe continuation lineage. Branch/fork sessions also have parents, so recovery must only walk through sessions whose parent link represents compression.
2. **Parent `/goal` guard:** `post_llm_call` runs before gateway goal migration. If a compressed child session has no active goal yet, the parent may still have one. Check both current child id and recovered ancestor id before enqueueing.
3. **Profile-aware DB path:** use `hermes_constants.get_hermes_home()` for `state.db`, with a narrow fallback only if Hermes internals are unavailable.
4. **DB lookup coverage:** add a temp sqlite test for session-link lookup instead of relying only on monkeypatched parent methods.
5. **Operational logging:** distinguish no context, no compression ancestor, and DB lookup failure at INFO for max-summary paths, without logging message bodies.

## Non-goals

- Do not modify Hermes core.
- Do not add CLI/ACP support.
- Do not make continuation unbounded.
- Do not change the continuation prompt semantics.
- Do not auto-continue when built-in `/goal` is active on either the current child session or recovered compression ancestor.
- Do not recover context across branch/fork/non-compression parent links.
- Do not infer continuation from fuzzy assistant text; continue detecting the built-in max-iteration summary request.

## Acceptance Criteria

1. A max-iteration summary for a compressed child session queues continuation using the gateway context captured for its compression parent/ancestor.
2. Recovery refuses non-compression parent links.
3. The continuation count is tracked against the active/current child session id after recovery, so repeated compressed continuations remain bounded.
4. Normal exact-session behavior still passes unchanged.
5. Parent-session `/goal` activity prevents auto-continue even if the child session has not received migrated goal state yet.
6. Missing context / no compression ancestor / parent lookup failure is visible in logs at `INFO` level without logging message bodies or prompts.
7. Focused plugin tests pass:
   - `python -m pytest tests/test_auto_continue.py -q`
   - `python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py`
8. Hermes discovery smoke still shows the plugin enabled and both hooks registered from the Hermes checkout.
9. Final report explicitly says gateway restart is required for live Slack to load the changed plugin code.

---

## Task 1: Add regression tests for compression session-split recovery

**Objective:** Prove the current bug before changing implementation: context recorded for an old compression session id should be usable when `post_llm_call` receives the compressed child session id.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Add a fake session-link resolver to the test file**

Add a tiny fake helper near `FakeSessionStore` or inline in the tests. The helper should model both parent id and why the link exists.

```python
class FakeSessionLinkLookup:
    def __init__(self, links: dict[str, tuple[str | None, str | None]]):
        self.links = links

    def link_for(self, session_id: str):
        parent_id, end_reason = self.links.get(session_id, (None, None))
        return parent_id, end_reason
```

If implementation uses a small dataclass such as `SessionLink(parent_id, parent_end_reason)`, adapt the helper to return that dataclass. Keep the test dependency injectable and deterministic.

**Step 2: Add the failing compressed-child test**

```python
def test_recovers_gateway_context_from_compression_parent_session():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", "compression")}).link_for

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
```

**Step 3: Add the non-compression guard test**

```python
def test_does_not_recover_gateway_context_from_non_compression_parent_session():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", "branch")}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
    assert "new-session" not in plugin._contexts
```

**Step 4: Run the focused tests and confirm RED**

Run:

```bash
python -m pytest \
  tests/test_auto_continue.py::test_recovers_gateway_context_from_compression_parent_session \
  tests/test_auto_continue.py::test_does_not_recover_gateway_context_from_non_compression_parent_session \
  -q
```

Expected before implementation: the compressed-child test FAILs because no continuation is enqueued. The non-compression test may pass or fail depending on interim implementation state; final behavior must pass.

---

## Task 2: Implement compression-only context recovery

**Objective:** Resolve a missing context by walking only compression session links, caching the recovered context under the current session id, and returning the recovered ancestor id for later guard checks.

**Files:**
- Modify: `hermes_auto_continue.py`

**Step 1: Add lightweight result types**

Near `GatewayContext`, add dataclasses or named tuples:

```python
@dataclass
class SessionLink:
    parent_id: str | None
    parent_end_reason: str | None


@dataclass
class ContextResolution:
    context: GatewayContext | None
    ancestor_session_id: str | None = None
    blocked_reason: str | None = None
```

**Step 2: Add helper methods to `AutoContinuePlugin`**

Add methods near `_active_goal_exists`:

```python
def _context_for_session(self, session_id: str) -> ContextResolution:
    context = self._contexts.get(session_id)
    if context is not None:
        return ContextResolution(context=context, ancestor_session_id=session_id)

    visited: set[str] = {session_id}
    current = session_id
    for _ in range(8):
        link = self._session_link(current)
        parent = link.parent_id
        if not parent:
            return ContextResolution(context=None, blocked_reason="no_parent")
        if parent in visited:
            return ContextResolution(context=None, blocked_reason="cycle")
        if link.parent_end_reason != "compression":
            return ContextResolution(context=None, blocked_reason="non_compression_parent")

        visited.add(parent)
        context = self._contexts.get(parent)
        if context is not None:
            self._contexts[session_id] = context
            parent_count = self._counts.pop(parent, 0)
            if parent_count and session_id not in self._counts:
                self._counts[session_id] = parent_count
            logger.info(
                "auto-continue: recovered gateway context for session %s from compression ancestor %s",
                session_id,
                parent,
            )
            return ContextResolution(context=context, ancestor_session_id=parent)
        current = parent

    return ContextResolution(context=None, blocked_reason="max_depth")
```

Notes:

- The depth bound prevents accidental cycles or a pathological DB chain from causing unbounded work.
- Recovery is intentionally compression-only; do not cross `branch`, `fork`, `manual`, `None`, or unknown end reasons.
- Moving `_counts[parent]` to the child keeps the bound attached to the active compressed lineage. This assumes compression session splits are linear; the compression-only guard makes that assumption explicit.
- Do not log prompt text, assistant response, Slack metadata, or message bodies.

**Step 3: Add a profile-aware session-link lookup helper**

```python
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
            logger.info("auto-continue: state db not found while resolving parent for session %s", session_id)
            return SessionLink(parent_id=None, parent_end_reason=None)
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "select parent_session_id, end_reason from sessions where id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return SessionLink(parent_id=None, parent_end_reason=None)
        parent, end_reason = row
        return SessionLink(
            parent_id=str(parent) if parent else None,
            parent_end_reason=str(end_reason) if end_reason else None,
        )
    except Exception:
        logger.info("auto-continue: parent lookup failed for session %s", session_id, exc_info=True)
        return SessionLink(parent_id=None, parent_end_reason=None)
```

Important implementation detail: verify against live session rows whether compression stores `end_reason='compression'` on the child or the parent. If the value is stored on the parent row instead, adjust `_session_link()` to fetch the parent's `end_reason` too and test that exact schema behavior. Do not guess here; inspect `state.db` rows for a known split before coding.

**Step 4: Use the helper in `post_llm_call`**

Replace:

```python
context = self._contexts.get(sid)
```

with:

```python
resolution = self._context_for_session(sid)
context = resolution.context
```

Change the missing-context log from debug to info and include the reason:

```python
logger.info(
    "auto-continue: no gateway context for session %s reason=%s",
    sid,
    resolution.blocked_reason or "missing_context",
)
```

Keep the platform check as-is, but apply it to `context.platform` after recovery.

**Step 5: Run the regression tests and confirm GREEN**

Run:

```bash
python -m pytest \
  tests/test_auto_continue.py::test_recovers_gateway_context_from_compression_parent_session \
  tests/test_auto_continue.py::test_does_not_recover_gateway_context_from_non_compression_parent_session \
  -q
```

Expected: both PASS.

---

## Task 3: Add `/goal` and bound-preservation guardrail tests

**Objective:** Ensure session split recovery neither competes with built-in `/goal` nor bypasses `max_auto_continues`.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Add a bound-preservation test**

```python
def test_compression_parent_recovery_preserves_auto_continue_bound():
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 1, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._counts["old-session"] = 1
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", "compression")}).link_for

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
    assert plugin._counts["new-session"] == 1
    assert "old-session" not in plugin._counts
```

**Step 2: Add a parent-goal guard test**

```python
def test_skips_when_compression_parent_has_active_goal(monkeypatch):
    plugin = AutoContinuePlugin({"enabled": True, "max_auto_continues": 2, "prompt": "Proceed."})
    gateway = FakeGateway()
    store = FakeSessionStore(FakeSessionEntry(session_id="old-session", session_key="slack:chat:thread"))
    event = make_event()

    plugin.pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
    plugin._session_link = FakeSessionLinkLookup({"new-session": ("old-session", "compression")}).link_for
    monkeypatch.setattr(plugin, "_active_goal_exists", lambda session_id: session_id == "old-session")

    plugin.post_llm_call(
        session_id="new-session",
        conversation_history=max_iteration_history(),
        assistant_response="summary",
        platform="slack",
    )

    assert gateway.enqueued == []
```

Implementation requirement for this test: after context recovery, `post_llm_call` must check both `sid` and `resolution.ancestor_session_id` if they differ:

```python
goal_session_ids = {sid}
if resolution.ancestor_session_id:
    goal_session_ids.add(resolution.ancestor_session_id)
if any(self._active_goal_exists(goal_sid) for goal_sid in goal_session_ids):
    logger.info("auto-continue: skipping session %s because /goal is active", sid)
    return
```

**Step 3: Run guardrail tests**

Run:

```bash
python -m pytest \
  tests/test_auto_continue.py::test_compression_parent_recovery_preserves_auto_continue_bound \
  tests/test_auto_continue.py::test_skips_when_compression_parent_has_active_goal \
  -q
```

Expected: both PASS.

---

## Task 4: Add a temp sqlite test for session-link lookup

**Objective:** Cover the actual DB lookup contract and profile-aware state DB path without touching real `~/.hermes/state.db`.

**Files:**
- Modify: `tests/test_auto_continue.py`

**Step 1: Add a temp sqlite fixture-style test**

```python
def test_session_link_reads_parent_and_end_reason_from_state_db(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as con:
        con.execute("create table sessions (id text primary key, parent_session_id text, end_reason text)")
        con.execute(
            "insert into sessions (id, parent_session_id, end_reason) values (?, ?, ?)",
            ("child-session", "parent-session", "compression"),
        )

    plugin = AutoContinuePlugin({"enabled": True})
    monkeypatch.setattr(plugin, "_state_db_path", lambda: db_path)

    link = plugin._session_link("child-session")

    assert link.parent_id == "parent-session"
    assert link.parent_end_reason == "compression"
```

If live schema inspection proves `end_reason='compression'` is stored on the parent row instead of the child row, update this test and `_session_link()` together to match the real schema.

**Step 2: Run the temp sqlite test**

```bash
python -m pytest tests/test_auto_continue.py::test_session_link_reads_parent_and_end_reason_from_state_db -q
```

Expected: PASS.

---

## Task 5: Run the full focused plugin suite and syntax check

**Objective:** Verify the standalone plugin remains healthy.

**Files:**
- No source changes expected.

**Step 1: Run focused tests**

```bash
python -m pytest tests/test_auto_continue.py -q
```

Expected: all tests PASS.

**Step 2: Run compile check**

```bash
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Expected: exit 0.

**Step 3: Run Hermes discovery smoke from the Hermes checkout**

```bash
cd ~/.hermes/hermes-agent
uv run python - <<'PY'
from hermes_cli.plugins import PluginManager
pm = PluginManager()
pm.discover_and_load(force=True)
loaded = pm._plugins.get('hermes-auto-continue')
print('found=', bool(loaded))
print('enabled=', getattr(loaded, 'enabled', None))
print('error=', getattr(loaded, 'error', None))
print('hooks=', sorted(getattr(loaded, 'hooks_registered', []) or []))
print('commands=', sorted(getattr(loaded, 'commands_registered', []) or []))
PY
```

Expected:

```text
found= True
enabled= True
error= None
hooks= ['post_llm_call', 'pre_gateway_dispatch']
commands= ['auto-continue']
```

---

## Task 6: Document the operational caveat

**Objective:** Make the failure mode and restart requirement visible for future debugging.

**Files:**
- Modify: `README.md`
- Optional modify: `AGENTS.md`

**Step 1: Update README Runtime behavior**

Add bullets under Runtime behavior:

```markdown
- Recovers gateway context across compression-created session splits by following profile-aware Hermes `state.db` session lineage.
- Refuses context recovery across non-compression parent links and skips when `/goal` is active on either side of a compression split.
```

**Step 2: Update README Development/Troubleshooting**

Add a short troubleshooting note:

```markdown
If a live turn logs `max_iterations_reached(...)` and `Session split detected: old → new (compression)` but no `auto-continue: queued continuation...`, check for `auto-continue: no gateway context...` / `auto-continue: recovered gateway context...` / `auto-continue: parent lookup failed...` in `~/.hermes/logs/gateway.log` or `agent.log`. After plugin code changes, restart the Hermes gateway.
```

**Step 3: Run no extra tests unless markdown tooling exists**

No markdown test is required unless the repo later adds one.

---

## Task 7: Commit and report

**Objective:** Leave a clean, reviewable change.

**Files:**
- `hermes_auto_continue.py`
- `tests/test_auto_continue.py`
- `README.md`
- Optional `AGENTS.md`
- This plan file

**Step 1: Inspect diff**

```bash
git diff -- hermes_auto_continue.py tests/test_auto_continue.py README.md AGENTS.md docs/plans/2026-05-28-session-split-context-recovery.md
```

Expected: only the planned changes.

**Step 2: Check status**

```bash
git status --short --branch
```

Expected: only planned modified/new files.

**Step 3: Commit**

```bash
git add hermes_auto_continue.py tests/test_auto_continue.py README.md docs/plans/2026-05-28-session-split-context-recovery.md
# Add AGENTS.md only if changed.
git commit -m "fix: compression後のauto-continue文脈を復元"
```

**Step 4: Final report**

Report:

- root cause fixed: context lookup missed after compression session split
- tests run and results
- commit hash
- live gateway requires restart before Slack behavior changes
- no Hermes core changes made

---

## Review Notes for Implementation

External review already found the main traps; implementation review should re-check:

1. Recovery is compression-only and does not cross branch/fork/non-compression parent links.
2. `/goal` is checked on both current child session id and recovered ancestor id.
3. `state.db` path is profile-aware via `get_hermes_home()`.
4. Count migration cannot bypass `max_auto_continues` on a compressed lineage.
5. Logging is useful for future live diagnosis and does not leak message bodies, prompts, Slack metadata, or assistant response text.

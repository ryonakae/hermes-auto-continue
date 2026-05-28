# AGENTS.md

This repository is a standalone Hermes Agent plugin. It should live at `~/.hermes/plugins/hermes-auto-continue`.

## Start here

- `plugin.yaml` — Hermes plugin manifest.
- `__init__.py` — thin loader that exposes `register(ctx)`.
- `hermes_auto_continue.py` — plugin implementation.
- `tests/test_auto_continue.py` — behavior tests for gateway continuation.
- `config.example.yaml` — tracked runtime config template. Copy it to ignored `config.yaml` for local use.

## Commands

Run from this repository root:

```bash
python -m pytest tests/test_auto_continue.py -q
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Optional discovery smoke from the Hermes checkout:

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

## Implementation notes

- Scope is gateway-only. Do not claim CLI or ACP support unless their queue/session objects are explicitly wired in later.
- The plugin intentionally uses private-ish gateway internals: `gateway._enqueue_fifo`, gateway `MessageEvent`, and adapter `_pending_messages` semantics. Keep that dependency documented.
- Visible auto-continue notices are registered as post-delivery callbacks when adapters support a safe generation-aware callback path, so long assistant summaries finish before the notice appears. The normal path awaits the notice send before the queued follow-up turn starts. Do not queue the notice as a `MessageEvent`.
- Detect max-iteration summary turns by the built-in summary request string in `conversation_history`, not by fuzzy assistant wording.
- Skip auto-continue when built-in `/goal` is active for the session; two continuation loops should not compete.
- Keep continuation bounded by `max_auto_continues`; do not add unbounded retry behavior.
- Runtime config comes from ignored `config.yaml` in this plugin directory. Keep `config.example.yaml` tracked as the template. Only plugin enablement lives in `~/.hermes/config.yaml` under `plugins.enabled`.

## Workflow

- Use TDD for behavior changes: add/update a focused test, verify RED, implement GREEN, then run the commands above.
- Do not edit Hermes core for this plugin unless the user explicitly approves a separate core change.
- After changing plugin code/config in a live setup, tell the user a gateway restart is required.
- Runtime files such as caches or state should be ignored; do not commit `__pycache__`, `.pytest_cache`, or local runtime artifacts.

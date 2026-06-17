# AGENTS.md

Standalone Hermes Agent plugin for gateway-only auto-continue after max-iteration summaries. Work from this repository root; installed runtime copies usually live at `~/.hermes/plugins/hermes-auto-continue`.

## Commands

```bash
python - <<'PY'
import pytest
raise SystemExit(pytest.main(['-p', 'no:rtk', 'tests/test_auto_continue.py', '-q']))
PY
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Optional plugin discovery smoke from the Hermes checkout:

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

If the smoke test reports `enabled=False` with `error='not enabled in config'`, enable `hermes-auto-continue` under `plugins.enabled` and restart the gateway.

## Important files

- `plugin.yaml`: plugin manifest, hook list, command list, and release version.
- `__init__.py`: loader that exposes `register(ctx)`.
- `hermes_auto_continue.py`: plugin implementation.
- `tests/test_auto_continue.py`: behavior tests for gateway continuation, notices, bounds, and compression session recovery.
- `config.example.yaml`: tracked runtime config template. Copy it to ignored `config.yaml` for local runtime use.
- `README.md` and `README.ja.md`: public docs. Keep the English README as source of truth, then update the Japanese sibling with the same structure.

## Implementation constraints

- Scope is gateway-only. Do not claim CLI or ACP support unless their session/queue objects are explicitly wired later.
- Keep auto-continue bounded by `max_auto_continues`; do not add unbounded retry behavior.
- Detect max-iteration summary turns by the built-in summary request string in `conversation_history`, not by fuzzy assistant wording.
- Skip sessions with an active built-in `/goal`; two continuation loops should not compete.
- Count state must survive compression descendants and same-thread sibling sessions so notices do not regress from `(2/3)` to `(1/3)` after a session split.
- Visible auto-continue notices should use post-delivery callbacks when adapter support exists. Do not queue the notice as a `MessageEvent`.
- Runtime config belongs in ignored `config.yaml` in this plugin directory. Only plugin enablement belongs in `~/.hermes/config.yaml` under `plugins.enabled`.

## Workflow

- Use TDD for behavior changes: add or update a focused test, verify it fails for the intended reason, implement, then run the commands above.
- Do not edit Hermes core for this plugin unless the user explicitly approves a separate core change.
- After changing plugin code/config in a live setup, tell the user a gateway restart is required.
- Do not commit runtime artifacts: `config.yaml`, `__pycache__`, `.pytest_cache`, or local state files.

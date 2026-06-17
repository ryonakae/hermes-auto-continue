# hermes-auto-continue

Keep Hermes Agent gateway conversations moving after a max-iteration summary by injecting one bounded follow-up turn into the same thread.

<!-- README-I18N:START -->

**English** | [日本語](./README.ja.md)

<!-- README-I18N:END -->

## Contents

- [Install](#install)
- [Configuration](#configuration)
- [Usage](#usage)
- [Behavior](#behavior)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

- **Gateway-native continuation:** reuses Hermes gateway FIFO so Slack, Telegram, Discord, and other gateway sessions continue in place.
- **Bounded by default:** stops after `max_auto_continues` so one stuck task cannot loop forever.
- **Visible notices:** posts the injected prompt back to the conversation after the assistant summary finishes.
- **Session-split aware:** carries counts across compression descendants and sibling sessions that share the same gateway thread.
- **Goal-safe:** skips sessions where Hermes built-in `/goal` is active.

## Install

Clone the plugin into the Hermes plugin directory:

```bash
git clone git@github.com:ryonakae/hermes-auto-continue.git ~/.hermes/plugins/hermes-auto-continue
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-auto-continue
```

Copy the runtime config template:

```bash
cp ~/.hermes/plugins/hermes-auto-continue/config.example.yaml \
  ~/.hermes/plugins/hermes-auto-continue/config.yaml
```

Requires Hermes Agent with standalone plugin support and PyYAML available in the Hermes runtime.
Restart the Hermes gateway after changing plugin code or config.

## Configuration

Edit `~/.hermes/plugins/hermes-auto-continue/config.yaml`:

```yaml
enabled: true
max_auto_continues: 3
prompt: >-
  Continue autonomously from the current state. Do not repeat completed work.
  Stop and summarize if blocked, if approval is required, or before destructive
  or externally visible actions.
platforms:
  - telegram
  - discord
  - slack
  - webhook
  - api_server
```

`platforms` is an allowlist. Omit it to allow every gateway platform. Keep `prompt` conservative because it becomes a synthetic user turn in the same gateway session.

## Usage

Start a long task from a gateway surface such as Slack. When Hermes hits its max tool-calling iteration limit, the plugin detects the built-in summary request and queues the configured prompt through `gateway._enqueue_fifo`.

```text
:robot_face: Injected auto-continue prompt (1/3):
Continue autonomously from the current state. Do not repeat completed work.
Stop and summarize if blocked, if approval is required, or before destructive or externally visible actions.
```

Check or reset in-session state from a gateway conversation:

```text
/auto-continue status
/auto-continue reset
```

The queued prompt is not sent as a normal chat message. The visible notice is informational; the actual continuation runs through the gateway FIFO.

## Behavior

- Detects max-iteration summary turns by matching Hermes' built-in summary request in `conversation_history`.
- Queues one configured prompt into the same gateway session after the summary turn.
- Registers visible notices as post-delivery callbacks when the adapter supports them, so split summary chunks arrive before the notice.
- Tracks continuation counts by session and recovers counts across compression-related sessions.
- Resets the count after a normal non-max-iteration turn.

## Limitations

- Gateway platforms only. CLI and ACP sessions are outside this plugin's v0 scope.
- The implementation uses Hermes gateway internals: `gateway._enqueue_fifo`, `MessageEvent`, adapter post-delivery callbacks, and session metadata.
- The plugin continues after Hermes produces the max-iteration summary. It does not extend the current tool loop.
- Auto-continue should not bypass approval, destructive-action, or externally visible action boundaries.

## Development

Run from the repository root:

```bash
python - <<'PY'
import pytest
raise SystemExit(pytest.main(['-p', 'no:rtk', 'tests/test_auto_continue.py', '-q']))
PY
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

If discovery reports `enabled=False` with `error='not enabled in config'`, add `hermes-auto-continue` to `plugins.enabled` and restart the gateway.

## License

[MIT](LICENSE)

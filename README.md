# hermes-auto-continue

Gateway-only auto-continue plugin for Hermes Agent.

When a gateway conversation hits Hermes' max tool-calling iteration limit, Hermes normally asks the model for a tool-less summary and then stops. This plugin detects that summary turn and queues a configured follow-up prompt into the same gateway session, similar to how built-in `/goal` continuations use the gateway FIFO.

Current scope: **gateway platforms only** (Slack, Telegram, Discord, etc.). CLI and ACP are intentionally out of scope for v0 because their continuation queues are not exposed to standalone plugins.

## How it works

```text
gateway message
  -> Hermes agent turn
  -> max iterations reached
  -> Hermes adds the built-in summary request
  -> post_llm_call hook sees that summary request in conversation_history
  -> plugin queues configured prompt via gateway._enqueue_fifo(...)
  -> next gateway turn runs automatically
```

The plugin does not extend the current tool loop before the summary. It continues **after** the summary, as a new synthetic user turn.

## Install

Clone this repository under the Hermes plugin directory:

```bash
git clone git@github.com:ryonakae/hermes-auto-continue.git ~/.hermes/plugins/hermes-auto-continue
```

Enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-auto-continue
```

Configure runtime behavior by copying the example config:

```bash
cp ~/.hermes/plugins/hermes-auto-continue/config.example.yaml \
  ~/.hermes/plugins/hermes-auto-continue/config.yaml
```

Then edit `~/.hermes/plugins/hermes-auto-continue/config.yaml`:

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
  - whatsapp
  - signal
  - matrix
  - mattermost
  - email
  - sms
  - dingtalk
  - wecom
  - weixin
  - feishu
  - qqbot
  - bluebubbles
  - yuanbao
  - webhook
  - api_server
  - homeassistant
```

`platforms` is an allowlist. Remove a platform name to disable auto-continue for that platform. If `platforms` is omitted, all gateway platforms are allowed.

Restart the Hermes gateway after changing plugin code or config.

## Runtime behavior

- Detects max-iteration summary turns by looking for Hermes' built-in summary request in `conversation_history`.
- Queues the configured prompt into the same gateway session using private gateway FIFO API (`gateway._enqueue_fifo`).
- Posts a visible side notice in the same gateway conversation after a successful injection. Slack uses its emoji shortcode so the message renders as a Slack emoji; other platforms use Unicode:

  ```text
  :robot_face: Injected auto-continue prompt (1/3):
  <configured prompt>
  ```

  The notice is not queued as user input; the actual continuation is still delivered internally through the gateway FIFO.
- Bounds continuation per session with `max_auto_continues`.
- Resets the per-session count after a normal non-max-iteration turn.
- Skips sessions with an active built-in `/goal`, to avoid competing continuation loops.
- Exposes `/auto-continue status` and `/auto-continue reset` as a lightweight in-session command when the plugin is loaded.

## Limitations

- Gateway only. CLI/ACP are not supported by this standalone plugin.
- Uses Hermes private-ish gateway internals (`_enqueue_fifo`, `MessageEvent`). This is acceptable for this plugin, but may need updates if Hermes changes those internals.
- The plugin queues a follow-up turn after Hermes' summary; it does not modify the core max-iteration loop.
- The configured prompt should stay conservative. Do not use it to bypass approval, destructive-action, or externally-visible-action boundaries.

## Development

```bash
python -m pytest tests/test_auto_continue.py -q
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Plugin discovery smoke from the Hermes checkout:

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

If `enabled=False` with `error='not enabled in config'`, add `hermes-auto-continue` to `plugins.enabled` and restart the gateway.

## License

MIT. See [LICENSE](LICENSE).

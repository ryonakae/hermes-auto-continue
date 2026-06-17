# hermes-auto-continue

Hermes Agent のゲートウェイ会話が最大イテレーション到達時の要約で止まったとき、同じスレッドへ上限付きの後続ターンを注入して処理を続けます。

<!-- README-I18N:START -->

[English](./README.md) | **日本語**

<!-- README-I18N:END -->

## 目次

- [インストール](#インストール)
- [設定](#設定)
- [使い方](#使い方)
- [挙動](#挙動)
- [制限](#制限)
- [開発](#開発)
- [ライセンス](#ライセンス)

- **ゲートウェイ上でそのまま続行:** Hermes のゲートウェイ FIFO を使い、Slack、Telegram、Discord などの会話を同じ場所で続けます。
- **標準で回数制限:** `max_auto_continues` で止まるため、詰まったタスクが無限ループしません。
- **注入内容を通知:** assistant の要約が届いた後、注入したプロンプトを会話へ表示します。
- **セッション分割に対応:** 圧縮で分岐した子セッションや、同じゲートウェイスレッドを共有する兄弟セッションの間で回数を引き継ぎます。
- **`/goal` と競合しない:** Hermes 組み込みの `/goal` が有効なセッションはスキップします。

## インストール

Hermes のプラグインディレクトリへクローンします:

```bash
git clone git@github.com:ryonakae/hermes-auto-continue.git ~/.hermes/plugins/hermes-auto-continue
```

`~/.hermes/config.yaml` でプラグインを有効にします:

```yaml
plugins:
  enabled:
    - hermes-auto-continue
```

実行時設定のテンプレートをコピーします:

```bash
cp ~/.hermes/plugins/hermes-auto-continue/config.example.yaml \
  ~/.hermes/plugins/hermes-auto-continue/config.yaml
```

Hermes Agent のスタンドアロンプラグイン対応と、Hermes の実行環境で使える PyYAML が必要です。
プラグイン本体や設定を変更したら、Hermes ゲートウェイを再起動してください。

## 設定

`~/.hermes/plugins/hermes-auto-continue/config.yaml` を編集します:

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

`platforms` は許可リストです。省略すると、すべてのゲートウェイプラットフォームで有効になります。`prompt` は同じゲートウェイセッション内の合成ユーザー発言として扱われるため、承認や破壊的操作の境界を越えない保守的な内容にしてください。

## 使い方

Slack などのゲートウェイ画面から長いタスクを開始します。Hermes がツール呼び出しの最大イテレーション数に達すると、このプラグインは組み込みの要約要求を検出し、設定済みプロンプトを `gateway._enqueue_fifo` 経由でキューに入れます。

```text
:robot_face: Injected auto-continue prompt (1/3):
Continue autonomously from the current state. Do not repeat completed work.
Stop and summarize if blocked, if approval is required, or before destructive or externally visible actions.
```

ゲートウェイ会話内で状態を確認・リセットできます:

```text
/auto-continue status
/auto-continue reset
```

キューに入ったプロンプトは、通常のチャットメッセージとしては送信されません。会話に表示される通知は情報表示だけで、実際の続行処理はゲートウェイ FIFO を通って実行されます。

## 挙動

- `conversation_history` 内の Hermes 組み込み要約要求を照合し、最大イテレーション到達時の要約ターンを検出します。
- 要約ターンの後、同じゲートウェイセッションに設定済みプロンプトを 1 回キューへ入れます。
- アダプターが対応している場合、表示通知を配信後コールバックとして登録します。分割された要約チャンクが先に届き、その後に通知が届きます。
- セッションごとに続行回数を持ち、圧縮で関連づいたセッション間で回数を復元します。
- 通常のターン、つまり最大イテレーション到達ではないターンの後に回数をリセットします。

## 制限

- ゲートウェイプラットフォーム専用です。CLI と ACP セッションは v0 の対象外です。
- 実装は Hermes ゲートウェイの内部 API に依存します: `gateway._enqueue_fifo`、`MessageEvent`、アダプターの配信後コールバック、セッションメタデータ。
- このプラグインは、Hermes が最大イテレーション到達時の要約を返した後に続行します。現在実行中のツールループ自体は延長しません。
- auto-continue を、承認、破壊的操作、外部に見える操作の境界回避に使わないでください。

## 開発

リポジトリのルートから実行します:

```bash
python - <<'PY'
import pytest
raise SystemExit(pytest.main(['-p', 'no:rtk', 'tests/test_auto_continue.py', '-q']))
PY
python -m py_compile __init__.py hermes_auto_continue.py tests/test_auto_continue.py
```

Hermes のチェックアウトから、必要に応じてプラグイン検出の簡易確認を実行できます:

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

検出結果が `enabled=False` と `error='not enabled in config'` を返す場合は、`plugins.enabled` に `hermes-auto-continue` を追加してゲートウェイを再起動してください。

## ライセンス

[MIT](LICENSE)

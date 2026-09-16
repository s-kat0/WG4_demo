# 検証報告

更新日: 2026-09-16

## 固定環境

- Python 3.12.10
- uv 0.11.19
- Streamlit 1.64.0
- openai 3.14.1
- openai-agents 0.22.2
- Pydantic 2.13.5
- NetworkX 3.6.1
- prompt `wg4-prompts-v3` / fixture `wg4-demo-v2`

## 非課金検証

2026-09-16にロック済み環境で次を実行した。

| コマンド | 結果 |
|---|---|
| `uv sync --locked` | 成功（116 packages resolved、110 checked） |
| `uv run --locked pytest` | 26 passed、1 skipped |
| `uv run --locked ruff check .` | 成功 |
| `uv run --locked ruff format --check .` | 成功（65 files already formatted） |
| `uv run --locked mypy wg4_demo` | 成功（32 source files） |
| `uv run --locked pip-audit --cache-dir /private/tmp/wg4-demo-pip-audit-cache` | 既知脆弱性0件 |
| `uv run --locked python scripts/check_repository_safety.py` | 成功（81 tracked files） |
| `uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05` | 30完了、0失敗、最大同時実行3、外部API呼び出し0 |

skipは`RUN_LIVE_TESTS=1`が明示されていない実API試験1件。Git追跡対象にはplaceholderだけの`.env.example`と`.streamlit/secrets.example.toml`を含み、実`.env`、実Secrets、runtime DB、log、export、recording、展開用ディレクトリは含まない。

ローカルStreamlitを実際に起動し、health endpoint、参加者ログイン、4画面への遷移、待機中表示、LLM無効時の`configuration_error`と代替回答なし、終端状態後のフォーム再有効化をブラウザで確認した。実APIを使う抽出・質問・回答・更新のend-to-endは未実施。

## 実API／Cloud

実キー、検証済みモデル、管理者による有限枠、GitHub remote、Cloud権限、公開URLが未提供のため未実施。mockの成功を実API成功として扱わない。

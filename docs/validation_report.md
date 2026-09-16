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
- prompt `wg4-prompts-v9` / fixture `wg4-demo-v2`

## 非課金検証

2026-09-16にロック済み環境で次を実行した。

| コマンド | 結果 |
|---|---|
| `uv sync --locked` | 成功（116 packages resolved、110 checked） |
| `uv run --locked pytest` | 32 passed、1 skipped |
| `uv run --locked ruff check .` | 成功 |
| `uv run --locked ruff format --check .` | 成功（66 files already formatted） |
| `uv run --locked mypy wg4_demo` | 成功（32 source files） |
| `uv run --locked pip-audit --cache-dir /private/tmp/wg4-demo-pip-audit-cache` | 既知脆弱性0件 |
| `uv run --locked python scripts/check_repository_safety.py` | 成功（81 tracked files） |
| `uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05` | 30完了、0失敗、最大同時実行3、median 1.3704秒、p95 1.6709秒、外部API呼び出し0 |

skipは`RUN_LIVE_TESTS=1`が明示されていない実API試験1件。Git追跡対象にはplaceholderだけの`.env.example`と`.streamlit/secrets.example.toml`を含み、実`.env`、実Secrets、runtime DB、log、export、recording、展開用ディレクトリは含まない。

ローカルStreamlitを実際に起動し、health endpoint、参加者ログイン、4画面への遷移、待機中表示、LLM無効時の`configuration_error`と代替回答なし、終端状態後のフォーム再有効化をブラウザで確認した。

## 実API／Cloud

2026-09-16、ユーザーが専用OpenAIプロジェクトの$50 Spend limitを設定したことを確認後、`.env`の実キーをサーバー側だけで読み、次の設定で実行した。

- model: `gpt-5.6-luna`
- reasoning effort: `low`
- Responses API、`store=false`、parallel tool calls無効、SDK/API自動retry無効
- 一時runtime DB、同時LLM 1、最大24 call、`GLOBAL_RPM=20`、`GLOBAL_TPM=200000`
- prompt `wg4-prompts-v9`

`scripts/run_live_validation.py --mode full`の結果:

| 項目 | 結果 |
|---|---|
| 状態 | passed |
| 全体時間 | 47.586秒 |
| 工程 | extract 6.364秒、interview 1.566秒、reflect 7.617秒、qa_v1 10.032秒、update 9.156秒、qa_v2 12.687秒 |
| API利用 | 18 calls、input 66,362 tokens、output 2,892 tokens |
| v1/v2回答 | `search_knowledge`、`get_context`、`read_evidence`を実行 |
| 更新 | 上記3ツールと`propose_update`を実行 |
| 承認 | pending中はv1維持、承認後v2 |
| 新しい会話 | 履歴なし、v2と追加根拠を取得 |

公開単価（input $0.20/M、output $1.20/M）による成功runの概算は約$0.0167。診断中の失敗runを含むOpenAI側の累積実額はこの一時台帳では集計していないため、Dashboardで確認する。

Streamlit Community Cloudへのdeploy、公開URL、Cloud上の実Secrets・実API・30実セッション負荷は未実施。mock／ローカル成功をCloud成功として扱わない。

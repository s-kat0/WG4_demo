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
- prompt `wg4-prompts-v10` / fixture `wg4-demo-v2`

## 非課金検証

2026-09-16にロック済み環境で次を実行した。

| コマンド | 結果 |
|---|---|
| `uv sync --locked` | 成功（116 packages resolved、110 checked） |
| `uv run --locked pytest` | 33 passed、1 skipped |
| `uv run --locked ruff check .` | 成功 |
| `uv run --locked ruff format --check .` | 成功（66 files already formatted） |
| `uv run --locked mypy wg4_demo` | 成功（32 source files） |
| `uv run --locked pip-audit --cache-dir /private/tmp/wg4-demo-pip-audit-cache` | 既知脆弱性0件 |
| `uv run --locked python scripts/check_repository_safety.py` | 成功（82 tracked files） |
| `uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05` | 30完了、0失敗、最大同時実行3、median 1.3704秒、p95 1.6709秒、外部API呼び出し0 |

skipは`RUN_LIVE_TESTS=1`が明示されていない実API試験1件。Git追跡対象にはplaceholderだけの`.env.example`と`.streamlit/secrets.example.toml`を含み、実`.env`、実Secrets、runtime DB、log、export、recording、展開用ディレクトリは含まない。

ローカルStreamlitを実際に起動し、health endpoint、参加者ログイン、4画面への遷移、待機中表示、LLM無効時の`configuration_error`と代替回答なし、終端状態後のフォーム再有効化をブラウザで確認した。

## 実API／Cloud

2026-09-16、ユーザーが専用OpenAIプロジェクトの$50 Spend limitを設定したことを確認後、`.env`の実キーをサーバー側だけで読み、次の設定で実行した。

- model: `gpt-5.6-luna`
- reasoning effort: `low`
- Responses API、`store=false`、parallel tool calls無効、SDK/API自動retry無効
- 一時runtime DB、同時LLM 1、最大24 call、`GLOBAL_RPM=20`、`GLOBAL_TPM=200000`
- prompt `wg4-prompts-v10`

Streamlitをローカルブラウザで操作し、抽出、追加質問、回答反映、pending承認、v1回答、更新案、承認前のv1維持、承認、新しい会話、v2・追加原文・グラフ・実行ツールの表示まで確認した。一時24 call枠の上限で`call_budget_exhausted`となり、代替回答や自動再送がないことも確認した。検証用台帳の最終は35 calls、input 139,596 tokens、output 4,286 tokens、active 0。

ブラウザ確認中、同じ主質問に対する決定的検索順は「知識項目3 v2 > 知識項目1 v1 > 知識項目2 v1」だったが、LLMが知識項目1だけを選ぶ事例を検出した。検索1位が適用可能な場合は最新版を第1候補にするprompt制約とサーバー側検証を追加し、逸脱時は`validation_top_candidate_missing`で操作全体を失敗させるようにした。固定回答への置換は行わない。

この選択修正後の`scripts/run_live_validation.py --mode full`は、仕様書の同じ主質問で次のとおり完走した。実行時のprompt内容はv10相当、識別ラベルはv9だった。

| 項目 | 結果 |
|---|---|
| 状態 | passed |
| 全体時間 | 47.619秒 |
| 工程 | extract 4.465秒、interview 1.761秒、reflect 5.277秒、qa_v1 12.233秒、update 9.058秒、qa_v2 14.665秒 |
| API利用 | 20 calls、input 79,512 tokens、output 2,907 tokens |
| v1/v2回答 | `search_knowledge`、`get_context`、`read_evidence`を実行 |
| 更新 | 上記3ツールと`propose_update`を実行 |
| 承認 | pending中はv1維持、承認後v2 |
| 新しい会話 | 履歴なし、v2と追加根拠を取得 |

公開単価（input $0.20/M、output $1.20/M）による上記成功runの概算は約$0.0194、ブラウザ検証35 callsの概算は約$0.0331（いずれもcached input割引を無視した上限寄り）。診断中の他の成功・失敗runを含むOpenAI側の累積実額はこの一時台帳では集計していないため、Dashboardで確認する。

prompt識別ラベルを`wg4-prompts-v10`へ更新した最終リビジョンの追加full runは、update工程のOpenAI `provider_error`で停止した。自動再送や代替結果は返していない。そのため、最終リビジョンのフル完走は未確認とする。また、Streamlit Community Cloudへのdeploy、公開URL、Cloud上の実Secrets・実API・30実セッション負荷も未実施。mock／ローカル成功をCloud成功として扱わない。

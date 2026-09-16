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
- prompt `wg4-prompts-v12` / fixture `wg4-demo-v3`

## 非課金検証

2026-09-16にロック済み環境で次を実行した。

| コマンド | 結果 |
|---|---|
| `uv sync --locked` | 成功（116 packages resolved、110 checked） |
| `uv run --locked pytest` | 35 passed、1 skipped |
| `uv run --locked ruff check .` | 成功 |
| `uv run --locked ruff format --check .` | 成功（67 files already formatted） |
| `uv run --locked mypy wg4_demo scripts/run_live_validation.py` | 成功（33 source files） |
| `uv run --locked pip-audit --cache-dir /private/tmp/wg4-demo-pip-audit-cache` | 既知脆弱性0件 |
| `uv run --locked python scripts/check_repository_safety.py` | 成功（83 tracked files） |
| `uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05` | 30完了、0失敗、最大同時実行3、median 1.3704秒、p95 1.6709秒、外部API呼び出し0 |

skipは`RUN_LIVE_TESTS=1`が明示されていない実API試験1件。Git追跡対象にはplaceholderだけの`.env.example`と`.streamlit/secrets.example.toml`を含み、実`.env`、実Secrets、runtime DB、log、export、recording、展開用ディレクトリは含まない。

ローカルStreamlitを実際に起動し、health endpoint、参加者ログイン、4画面への遷移、待機中表示、LLM無効時の`configuration_error`と代替回答なし、終端状態後のフォーム再有効化をブラウザで確認した。

## 実API／Cloud

2026-09-16、ユーザーが専用OpenAIプロジェクトの$50 Spend limitを設定したことを確認後、`.env`の実キーをサーバー側だけで読み、次の設定で実行した。

- model: `gpt-5.6-luna`
- reasoning effort: `low`
- Responses API、`store=false`、parallel tool calls無効、SDK/API自動retry無効
- 一時runtime DB、同時LLM 1、最大24 call、`GLOBAL_RPM=20`、`GLOBAL_TPM=200000`
- prompt `wg4-prompts-v12`

prompt v10時点でStreamlitをローカルブラウザ操作し、抽出、追加質問、回答反映、pending承認、v1回答、更新案、承認前のv1維持、承認、新しい会話、v2・追加原文・グラフ・実行ツールの表示まで確認した。一時24 call枠の上限で`call_budget_exhausted`となり、代替回答や自動再送がないことも確認した。検証用台帳の最終は35 calls、input 139,596 tokens、output 4,286 tokens、active 0。prompt v12では後述のCLIによる同等業務フローを確認し、ブラウザ操作自体は再実施していない。

ブラウザ確認中、同じ主質問に対する決定的検索順は「知識項目3 v2 > 知識項目1 v1 > 知識項目2 v1」だったが、LLMが知識項目1だけを選ぶ事例を検出した。検索1位が適用可能な場合は最新版を第1候補にするprompt制約とサーバー側検証を追加し、逸脱時は`validation_top_candidate_missing`で操作全体を失敗させるようにした。固定回答への置換は行わない。

自然文の保全記録1と簡潔化指示を導入した最初のfull run（prompt v11）は、抽出工程の`structured_output_invalid`で停止した。代替結果や自動再送は行っていない。自然文から複数種類のfactを得る際の`condition_scope`規則をpromptへ明記してv12とし、関連mockテストを通した後に別の検証runを開始した。

prompt v12の`scripts/run_live_validation.py --mode full`は、仕様書の主質問で次のとおり完走した。

| 項目 | 結果 |
|---|---|
| 状態 | passed |
| 全体時間 | 47.528秒 |
| 工程 | extract 4.994秒、interview 3.037秒、reflect 5.729秒、qa_v1 10.992秒、update 9.089秒、qa_v2 13.563秒 |
| API利用 | 20 calls、input 79,531 tokens、output 2,790 tokens |
| v1/v2回答 | `search_knowledge`、`get_context`、`read_evidence`を実行 |
| 更新 | 上記3ツールと`propose_update`を実行 |
| 承認 | pending中はv1維持、承認後v2 |
| 新しい会話 | 履歴なし、v2と追加根拠を取得 |

さらに、抽出結果を直接検査する`--mode extract`を1 callで実行し、5.749秒、input 844 tokens、output 455 tokensで完了した。未承認カードのfact.textは次のとおりで、すべて30文字以内、原因は`unresolved`、引用は登録原文の部分文字列だった。

- condition: `温度計交換後`
- observation: `出口温度が高めに表示`
- check_action: `別の計器と突き合わせ確認`
- cause_status: `表示差の原因は未特定`

公開単価（input $0.20/M、output $1.20/M）によるprompt v12の成功full run概算は約$0.0193、抽出専用runは約$0.0007（いずれもcached input割引を無視した上限寄り）。prompt v11の失敗runや診断中の他runを含むOpenAI側の累積実額は一時台帳では集計していないため、Dashboardで確認する。

Streamlit Community Cloudへのdeploy、公開URL、Cloud上の実Secrets・実API・30実セッション負荷は未実施。mock／ローカル成功をCloud成功として扱わない。

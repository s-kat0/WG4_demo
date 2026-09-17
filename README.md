# 現場知識をつなぐミニエージェント

2026年9月17日のWG4講演「AIエージェントによる暗黙知と形式知の構造化・活用の第一歩」で使用するStreamlitデモアプリです。

架空の製造設備事例を使い、文書・Q&Aから知識を探し、AIエージェントと原文を確認しながら相談し、人が承認した補足だけを次の回答へ反映する流れを体験できます。

**公開アプリ：<https://wg4-demo-kato.streamlit.app/>**

> すべて架空の教材です。実設備の診断や操作判断には使用できません。入力はOpenAI APIへ送信されるため、機密情報、個人情報、実際の現場記録を入力しないでください。

## まず読む資料

| 対象 | 最初に読む資料 | 用途 |
|---|---|---|
| 参加者 | [参加者向け操作ガイド](docs/participant_guide.md) | ログインからA/B/C比較までの手順と期待結果 |
| 講演者 | [講演デモ簡易手順](docs/demo_quick_guide.md) | 登壇中に見る短い進行表 |
| 運営者 | [運営手順](docs/operator_runbook.md) | 開始前、講演中、終了後、事故時の確認 |
| デプロイ担当 | [Cloudデプロイ手順](docs/deployment.md) | GitHub・Secrets・公開後確認 |
| 開発者 | [実装ハンドオフ](CODING_AGENT_HANDOFF.md) | コード構成、安全境界、変更時の確認 |

資料全体の位置付けは[資料索引](docs/README.md)にまとめています。実装要件のsingle source of truthは[SPEC.md](SPEC.md)です。

## 参加者が体験する流れ

1. **知識を探す** — 初期収録された架空の知識12件を、設備・由来で絞り込み、原文と版を確認します。通常検索はOpenAI APIを使用しません。
2. **エージェントに相談する** — 検索、知識グラフ、原文取得を使った根拠付き候補を確認し、「なぜ」「どの記録」「別条件なら」と追質問します。
3. **知識を追加・補足する** — 保全記録から未承認カードを抽出し、本人役への追加質問で判断理由と適用範囲を補います。
4. **更新案・実回答比較** — 人が原文と差分を承認し、文書版A、対話補足版B、追加条件版Cの実回答と出典を比較します。

承認前の情報は通常検索へ入りません。新しい会話では会話履歴だけが消え、承認済み知識は残ります。

## 講演後に学ぶための見どころ

このリポジトリで見ていただきたいのは、AIがそれらしい回答を生成する部分だけではありません。文書や経験談を、出典・適用条件・版と結び付け、人の承認を境界として安全に利用する一連の設計が中心です。

### まず10分で全体像をつかむ

1. [参加者向け操作ガイド](docs/participant_guide.md)で、検索、相談、知識の補足、承認後の再利用という流れを確認する
2. [初期知識カタログ](docs/seed_catalog_v5.md)で、文書由来とQ&A由来の知識がどのように収録されているかを見る
3. [デモ進行ガイド](docs/demo_quick_guide.md)で、文書のみの状態A、聞き取りで補足した状態B、回答へのフィードバックを承認した状態Cの違いを確認する

### 関心別の読みどころ

| 関心 | 最初に見る場所 | 学べること |
|---|---|---|
| 業務での使い方・知識継承 | [参加者向け操作ガイド](docs/participant_guide.md)、[初期知識カタログ](docs/seed_catalog_v5.md) | 文書検索だけで終わらず、理由や条件を本人への聞き取りで補い、人が確認してから再利用する流れ |
| 知識・根拠・版の持ち方 | [`schemas.py`](wg4_demo/schemas.py)、[`repository.py`](wg4_demo/repository.py)、[`knowledge_seed_v5.json`](data/knowledge_seed_v5.json) | KnowledgeItem、fact、原文根拠、版、Proposal、Approvalを分ける理由 |
| 検索と根拠提示 | [`retrieval.py`](wg4_demo/retrieval.py)、[`graph.py`](wg4_demo/graph.py)、[`evidence.py`](wg4_demo/evidence.py)、[`tools.py`](wg4_demo/tools.py) | 決定的な検索、関連知識のグラフ探索、取得済み根拠だけを回答へ渡す構造 |
| LLMの使い方と安全境界 | [`llm_gateway.py`](wg4_demo/llm_gateway.py)、[`agent_runtime.py`](wg4_demo/agent_runtime.py)、[`result_validation.py`](wg4_demo/result_validation.py)、[`prompts/`](prompts/)、[障害時の挙動](docs/failure_matrix.md) | モデル呼び出しの一元化、structured outputの検証、検索0件と処理失敗の区別、意図しないfallbackを返さない設計 |
| 人による確認と承認 | [`approvals.py`](wg4_demo/approvals.py)、[`review.py`](wg4_demo/ui/review.py)、[`test_repository.py`](tests/test_repository.py)、[`test_v5_workflows.py`](tests/test_v5_workflows.py) | 未承認情報を通常検索から遮断し、承認時だけ新版を発行する仕組み |
| 複数参加者での運用 | [`jobs.py`](wg4_demo/jobs.py)、[`scheduler.py`](wg4_demo/scheduler.py)、[`usage_ledger.py`](wg4_demo/usage_ledger.py)、[`auth.py`](wg4_demo/auth.py)、[負荷試験レポート](docs/load_test_report.md) | workspace分離、共有キュー、同時実行制限、利用量管理、認証の役割 |

### コードを読むときの問い

- 「原因」「確認行動」「適用条件」を分けて保存することで、どの誤解を防いでいるか
- 検索や原文取得をLLM任せにせず、決定的な処理として分離しているのはなぜか
- 未承認候補が正式知識へ変わる境界はどこにあり、二重承認をどう防いでいるか
- 正常な検索0件と、検索処理そのものの失敗をUIとデータ型でどう区別しているか
- API失敗時に自動再送や代替回答を行わない判断には、どのような利点と運用上の負担があるか
- 実設備へ適用するなら、権限管理、監査、評価データ、責任分界をどこまで追加する必要があるか

このアプリは架空の設備事例を使った教材です。実設備の診断、安全判断、作業指示には使用せず、公開デモへ機密情報や個人情報を入力しないでください。

## 現在の確認状況

2026年9月17日時点の現行ブランチで、次を確認しています。

| 区分 | 結果 |
|---|---|
| ローカル実ブラウザ・実API | `gpt-5.6-luna`、reasoning `medium`、prompt `wg4-prompts-v15`で、検索、複数ターン相談、文書抽出、v1/v2/v3承認、A/B/C比較、新しい会話からv3参照まで成功 |
| 非課金テスト | 81 passed、1 skipped。skipは明示的なlive gate |
| 30セッション模擬キュー | 30完了、0失敗、最大同時実行3、workspace分離を確認。外部API呼出し0 |
| Repository安全検査 | `.env`、実Secrets、runtime DB、ログをGit管理しないことを確認 |
| Streamlit Cloud | 公開済み。現行ブランチの変更はpush・再デプロイ後に公開環境で再確認が必要 |

詳細と、未確認事項を含む検証範囲は[検証報告](docs/validation_report.md)を参照してください。

## ローカル起動

### 必要なもの

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- 使用を許可されたOpenAI project APIキー
- 参加者用と管理者用の異なるパスワード

### セットアップ

```bash
uv sync --locked
cp .env.example .env
uv run --locked python scripts/create_password_hash.py
```

`.env`へAPIキー、モデル、生成した2種類のArgon2idハッシュ、利用期限、確認済みRPM/TPMを設定します。平文パスワードは`.env`へ保存しません。

主な推奨設定は次のとおりです。全項目と説明は[`.env.example`](.env.example)を参照してください。

```dotenv
APP_ENV=local
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=medium
APP_LLM_ENABLED=true
CALL_BUDGET_MODE=provider_hard_limit
MAX_CONCURRENT_LLM=3
MAX_CONCURRENT_JOBS=3
MAX_PENDING_JOBS=30
MAX_MODEL_CALLS_PER_ACTION=12
MAX_TOOL_CALLS_PER_ACTION=8
MAX_PROMPT_BYTES=262144
MAX_ESTIMATED_INPUT_TOKENS=65536
```

`provider_hard_limit`では、費用の強制停止をOpenAI project側のhard limitに委ねます。アプリ側の共有キュー、同時実行数、操作別上限、RPM/TPM、監査記録、管理者停止は引き続き有効です。

### 起動前確認と実行

```bash
uv run --locked --env-file .env python scripts/preflight.py
uv run --locked --env-file .env streamlit run app.py
```

ローカルでは`.env`を自動探索しません。必ず`--env-file .env`で明示的に渡します。

## パスワードハッシュの作成

```bash
uv run --locked python scripts/create_password_hash.py
```

同じパスワードを2回入力するとArgon2idハッシュだけが表示されます。参加者用と管理者用は異なる十分長い値にし、平文や生成メモをGitへ追加しないでください。

## Streamlit Community Cloudへのデプロイ

詳細は[Cloudデプロイ手順](docs/deployment.md)を参照してください。概要は次のとおりです。

1. 検証済みcommitをGitHubへpushする
2. entrypointを`app.py`、Pythonを3.12に設定する
3. [`.streamlit/secrets.example.toml`](.streamlit/secrets.example.toml)と同じキーをCloud Secretsへ登録する
4. `APP_ENV="cloud"`、`APP_LLM_ENABLED=true`、`CALL_BUDGET_MODE="provider_hard_limit"`を設定する
5. OpenAI projectのモデル利用可否、hard limit、RPM/TPMを確認する
6. 再起動後、未認証画面、参加者ログイン、workspace分離、主シナリオを実画面で確認する

APIキーとハッシュはサーバー側だけで利用し、ブラウザ、ログ、JSON exportへ含めません。

## テスト

通常のテストとCIはOpenAI APIを呼びません。

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy wg4_demo
uv run --locked python scripts/check_repository_safety.py
uv run --locked python scripts/run_load_test.py --sessions 30
```

実APIテストは、APIキー、live testの明示的有効化、利用期限、OpenAI側hard limitがそろう場合だけ実行します。通常テストのskipを実API成功として扱いません。

## 障害時の挙動

- 正常に検索して0件だった場合は、成功した0件として表示
- API、検索、グラフ、原文取得、出力検証、保存に失敗した場合は、安全なエラーコードと操作IDを表示して停止
- timeoutや通信断は状態不明として記録し、同じ操作を自動再送しない
- 固定回答、過去回答、別モデル、推測値、自動JSON修復へ切り替えない
- A/B/Cが失敗した場合は失敗記録を残し、利用者が明示的に開始した新しい操作だけを再実行として扱う

詳細な契約は[障害時の結果契約](docs/failure_matrix.md)を参照してください。

## Repository構成

```text
app.py                       Streamlit entrypoint
wg4_demo/                    認証、DB、検索、会話、LLM、Agent、Queue、UI
data/knowledge_seed_v5.json  初期12件の架空教材
data/demo_inputs_v5.json     講演中に送信する架空の入力例
prompts/                     抽出、聞き取り、回答、更新の制約
tests/                       非課金unit・integration・AppTest・live gate
scripts/                     preflight、安全検査、模擬負荷、live検証
docs/                        参加者、講演者、運営者、開発者向け資料
runtime/                     実行時DB。Git管理外
```

## 限界

- 共有パスワードは個人認証、組織監査、機密データ管理の代替ではありません。
- SQLiteと単一Cloudプロセスを前提とし、再起動をまたぐ永続性や複数replicaを保証しません。
- 決定的検索は文字列bigramと小規模語彙規則で、一般的な意味検索や診断精度を保証しません。
- 会話条件の分類は明示語に基づくため、曖昧な発言では追加確認または安全な検証エラーになる場合があります。
- Structured Outputsは形式を制約しますが、内容の正しさは人が原文と照合する必要があります。
- 初期知識と追加事例は架空教材であり、実務上の安全性・工学的妥当性を示すものではありません。
- 30セッション試験はmock APIによるキュー・分離試験であり、Cloud上の30人実API性能保証ではありません。

セキュリティ上の連絡方法は[SECURITY.md](SECURITY.md)を参照してください。

# 現場知識をつなぐミニエージェント

2026年9月17日のWG4講演「AIエージェントによる暗黙知と形式知の構造化・活用の第一歩」用Streamlitデモ。架空の設備記録とQ&Aを、原文、条件、判断理由、版とともに検索し、人が承認した補足だけを以後の相談に利用する。

このアプリは実設備の診断・操作指示には使用できない。入力はOpenAI APIへ送信されるため、機密情報、個人情報、実際の現場記録を入力しないこと。詳細仕様のsingle source of truthは [SPEC.md](SPEC.md)、実装入口は [CODING_AGENT_HANDOFF.md](CODING_AGENT_HANDOFF.md)。

## v5の主な機能

- 新規の実務デモworkspaceへ、検証済みの初期知識12件（文書6、Q&A6）をLLMなしで登録
- 承認済み知識だけを対象とする非課金検索、設備・由来フィルター、原文・版の表示
- Agents SDKによる検索、NetworkXグラフ探索、原文取得と根拠付き複数ターン相談
- 現状、仮定の別条件、訂正、設備切替、直前候補を分けた会話状態
- 文書だけの版A、本人役の理由・適用範囲を補った版B、校正条件を補った版Cの実回答記録
- workspaceごとに分離したSQLiteの原文、知識版、Proposal、Approval、会話、比較snapshot
- 参加者／管理者のArgon2id認証、API利用監査台帳、共有FIFOキュー、同時実行3、待機30、1セッション1件
- API、検索、検証、保存の失敗を固定回答、別モデル、前回回答、正常0件へ置換しないエラー契約
- Cloud参加者画面は初期12件の練習領域と四つの主画面に限定。管理機能は別パスワード付きの折りたたみ入口へ分離

## ローカル起動

Python 3.12とuvが必要。

```bash
uv sync --locked
cp .env.example .env
uv run --locked python scripts/create_password_hash.py
# .envへ専用APIキー、モデル、異なる2つのハッシュ、期限、確認済みTPMを設定
uv run --locked --env-file .env python scripts/preflight.py
uv run --locked --env-file .env streamlit run app.py
```

アプリは`.env`を自動探索しない。ローカルではuvの`--env-file`で明示的に渡す。実`.env`、APIキー、平文パスワード、runtime DBはGit管理しない。

### `.env`の設定

`.env.example`をコピーし、少なくとも次を設定する。

- `OPENAI_API_KEY`: サーバー側だけで読む専用project key
- `OPENAI_MODEL`: 検証済みの正確なモデルID。別モデルへ自動切替しない
- `OPENAI_REASONING_EFFORT`: 講演用は`medium`を推奨。`low`は短時間・低コストだが、v5実測で必須fact種別を落とす場合があった
- `DEMO_PASSWORD_HASH` / `ADMIN_PASSWORD_HASH`: 別々のArgon2idハッシュ
- `DEMO_EXPIRES_AT`: timezone付きISO 8601
- `APP_LLM_ENABLED`: 実送信を許すときだけ`true`
- `CALL_BUDGET_MODE`: 通常は`provider_hard_limit`。OpenAI projectのhard limitを費用上限にする
- `GLOBAL_RPM` / `GLOBAL_TPM`: OpenAI projectで確認した上限以下
- `APP_MAX_LLM_CALLS` / `SESSION_MAX_LLM_CALLS`: `CALL_BUDGET_MODE=finite`の場合だけ使う互換設定

### パスワードハッシュ

```bash
uv run --locked python scripts/create_password_hash.py
```

スクリプトは`getpass`で同じ値を2回読み、Argon2idハッシュだけを表示する。参加者用と管理者用は異なる十分長い値にする。平文や生成メモをリポジトリへ置かない。

## 実LLMの有効化と利用上限

標準の`CALL_BUDGET_MODE=provider_hard_limit`で実APIを送信するには、次の条件が必要。

1. `APP_LLM_ENABLED=true`
2. APIキー、モデル、reasoning、期限、確認済みTPMが設定済み
3. 専用OpenAI projectで強制停止型のhard limitが設定済み

推奨するアプリ側初期値は`GLOBAL_RPM=60`、`GLOBAL_TPM=200000`、`MAX_CONCURRENT_LLM=3`、`MAX_CONCURRENT_JOBS=3`、`MAX_PENDING_JOBS=30`。実際のproject Dashboard上限を当日確認し、それ以下へ設定する。

`provider_hard_limit`では初回起動から有効で、全体・session別のcall数では停止しない。台帳は利用回数とtoken、失敗、unknownを監査用に記録する。管理者停止とprovider上限エラー時の停止、操作別上限、共有キュー、同時実行、RPM/TPMは維持する。timeout・接続断は課金状態不明として記録し、自動再送しない。従来の有限枠を使う場合だけ`CALL_BUDGET_MODE=finite`へ変更し、管理画面で枠を追加する。

## 講演用操作

詳細は [docs/demo_script_v5.md](docs/demo_script_v5.md)。主な流れは次のとおり。

1. 共通パスワードでログインする。初期12件を収録した自分専用の練習領域が作られる
2. 「知識を探す」で`冷却器1 流量低下`を非課金検索し、文書またはQ&Aの原文を見る
3. 「知識を追加・補足する」で保全記録1を抽出し、文書だけのpending案を作る
4. 「更新案・実回答比較」で内容と原文を確認し、対象事例v1を承認する
5. 文書版の比較回答Aを空履歴で実行する
6. 本人役の開始発言、判断理由、適用範囲を回答し、補足pending案を作って承認する
7. 同じ質問を空履歴で実行してBを保存し、A/Bの版、fact、出典を比べる
8. Bの実候補へ校正確認条件を補足し、承認後にCを実行してB/Cを比べる
9. 通常相談では「なぜ」「どの記録」「流量が低い場合」を追質問する

聞き取り画面の開始発言と本人役の回答欄に入る例文は、アプリに固定収録した架空教材であり、LLM生成ではない。LLMが生成するのは「追加質問」と「追加質問が不要という完了判定」で、画面上でも区別して表示する。固定回答例は理由用・適用範囲用を各一度だけ提示し、以後は空欄とする。

API障害時は処理を停止する。録画や静止画を使う場合、講演者がアプリ外で「記録の再生」と明示し、アプリの成功結果にはしない。

## Streamlit Community Cloud

詳細は [docs/deployment.md](docs/deployment.md)。運営者が行う作業は次のとおり。

1. 最終検査済みbranchを指定GitHub repositoryへpushする
2. Community Cloudでentrypointを`app.py`、Pythonを3.12に設定する
3. `.streamlit/secrets.example.toml`と同じキーをCloud Secretsへ実値で登録する
4. `APP_ENV="cloud"`を設定する
5. 専用OpenAI projectのモデル利用可否、レート、強制停止型支出上限を確認する
6. サイドバー下部の「運営者用」から管理画面を開き、`budget_mode=provider_hard_limit`と利用回数を確認する
7. 未認証、2ブラウザ分離、主シナリオ、1→5→10→30 sessionの段階試験を行う

Cloud URL、Cloud Secrets、公開操作はこの実装作業では設定しない。仮URLを稼働URLとして記載しない。

参加者向けCloud UIには「旧デモ互換」「空の領域から開始」「領域を初期状態へ戻す」を表示しない。旧workspaceの読取りと回帰試験に必要なbackend互換処理は残す。新しい空の練習領域が必要な場合は、ログアウト後に再ログインして別workspaceを作る。

## 障害時の挙動

- 通常検索0件: 成功した0件として明示
- 検索、グラフ、原文取得失敗: `failed`とし、知識なしとは表示しない
- timeout、接続断: `indeterminate`。同一操作を自動再送しない
- structured output、根拠ID、版の不整合: 検証失敗。部分採用・自動JSON修復なし
- 待機列満杯、期限切れ: 受付拒否または`expired`。未送信ならAPI枠を消費しない
- 取消後の遅着: 回答やpending案を公開しない
- DB消失: 知識を成功扱いで復元しない。監査台帳消失時は過去回数を復元できないと扱い、費用停止はOpenAI側hard limitに委ねる
- A/B/C記録なし・失敗: 「比較用の実行記録なし」または失敗コードを表示し、模範文で埋めない

## 非課金テスト

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy wg4_demo
uv run --locked python scripts/check_repository_safety.py
uv run --locked python scripts/run_load_test.py --sessions 30
```

通常CIは実APIを呼ばない。liveテストは`RUN_LIVE_TESTS=1`、実設定、参加者パスワード、有限台帳がすべてそろう場合だけ実行する。v3のlive結果をv5の実績として流用しない。

## 構成

```text
app.py                       Streamlit entrypoint
wg4_demo/                    認証、DB、seed、検索、会話、LLM、Agent、Queue、UI
data/knowledge_seed_v5.json  初期12件の架空教材manifest
data/demo_inputs_v5.json     講演中に初めて送る入力例
prompts/                     抽出、聞き取り、補足、回答、更新の制約
tests/                       非課金unit・integration・AppTest・queue・live gate
scripts/                     preflight、秘密検査、30-session模擬負荷、live検証
docs/                        v5受入条件、操作、seedカタログ、deploy、検証報告
runtime/                     実行時DB（Git管理外）
```

## 限界・既知のリスク

- 共有パスワードは個人認証、企業監査、機密データ管理の代替ではない
- SQLiteと単一Cloudプロセス前提で、再起動をまたぐ完遂、複数replica、永続保存を保証しない
- 決定的検索は文字列bigramと小規模語彙規則で、一般的な日本語理解や診断精度を保証しない
- 会話状態の分類は明示語を使う決定的規則で、曖昧な表現は対象確認が必要
- Structured Outputsは形式を制約するが、内容の正しさは人の原文レビューが必要
- 初期12件と追加事例は架空教材で、実務上の安全性・工学的妥当性の証拠ではない
- 現行コードのv5 fullローカル実APIは`gpt-5.6-luna`・reasoning `medium`で1回成功（67.889秒、21 calls、入力78,677／出力4,048 tokens）。同一revisionでの複数回再現性、Cloud実ブラウザ、Cloud 30-session実API負荷は未確認

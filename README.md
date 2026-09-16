# 現場知識をつなぐミニエージェント

2026年9月17日のWG4講演「AIエージェントによる暗黙知と形式知の構造化・活用の第一歩」用Streamlitデモ。架空の冷却器記録を、原文根拠・適用条件・版とともに整理し、人の承認後だけ正式知識へ反映する。

このアプリは実設備の診断・操作指示には使用できない。入力はOpenAI APIへ送信されるため、機密情報・個人情報・実際の現場記録を入力しないこと。

## 実装範囲

- 文書抽出、追加質問、回答反映、更新案作成のStructured Outputs
- OpenAI Agents SDKによる `search_knowledge`、`get_context`、`read_evidence`、`propose_update`
- workspaceごとに分離したSQLiteの原文・知識版・Proposal・Approval
- 現行承認版だけを対象にする決定的検索とNetworkXグラフ探索
- 参加者／管理者のArgon2id認証、有限利用台帳、RPM/TPM／call上限
- SQLite FIFO待機列、全体3ジョブ、待機30件、1セッション1件、取消・期限・重複防止
- API／検索／検証／保存失敗を成功結果へ置き換えないエラー契約

## ローカル起動

Python 3.12とuvが必要。

```bash
uv sync --locked
cp .env.example .env
uv run --locked python scripts/create_password_hash.py
# .envへ専用APIキー、検証済みモデル、別々の2ハッシュ、期限、確認済みTPMを設定
uv run --locked --env-file .env python scripts/preflight.py
uv run --locked --env-file .env streamlit run app.py
```

`.env`はGit管理外。アプリは`.env`を自動探索しないため、ローカルでは必ずuvの`--env-file`で明示的に渡す。APIキーや平文パスワードをCLI引数へ置かない。

### パスワードハッシュ

`scripts/create_password_hash.py`は`getpass`で同じ値を2回読み、Argon2idハッシュだけを出力する。参加者用と管理者用は異なる十分長いランダム値にする。ハッシュを含む実`.env`やCloud Secretsもリポジトリへ置かない。

## 実LLMの有効化と利用上限

実API送信には次の全条件が必要。

1. `APP_LLM_ENABLED=true`
2. `OPENAI_API_KEY`、正確な`OPENAI_MODEL`、`OPENAI_REASONING_EFFORT`、timezone付き`DEMO_EXPIRES_AT`、確認済み`GLOBAL_TPM`が設定済み
3. 管理者が専用OpenAIプロジェクトの強制停止型支出上限を確認
4. 管理画面で有限call枠を追加し、利用台帳を明示的に有効化

2026-09-16の実API検証では`OPENAI_MODEL=gpt-5.6-luna`、`OPENAI_REASONING_EFFORT=low`を採用した。主シナリオは47.6秒、20モデル呼出しで完走した。約30人の講演用の推奨初期値は`GLOBAL_RPM=60`、`GLOBAL_TPM=200000`、`MAX_CONCURRENT_LLM=3`。いずれもLunaの公開Tier 1上限（500 RPM、500,000 TPM）より低いが、実際のOpenAIプロジェクトDashboardに表示される上限を当日確認し、それ以下に設定すること。

台帳は初回・消失・認証世代変更時に停止状態、割当0から始まる。会話・知識・cacheを初期化しても使用済みcallは戻らない。送信後のtimeout／接続断は課金状態不明として枠を戻さず、自動再送しない。

## Streamlit Community Cloud

詳細は [docs/deployment.md](docs/deployment.md)。概略は次のとおり。

1. 秘密検査とCIを通した指定branchをGitHubへpush
2. Community Cloudでentrypointを`app.py`、Pythonを3.12に設定
3. `.streamlit/secrets.example.toml`と同じキーをCloud Secretsへ実値で登録
4. 専用OpenAIプロジェクトで強制停止型支出上限と利用可能モデルを確認
5. 管理画面で有限call枠を有効化
6. 未認証、2つの独立ブラウザ、実API主シナリオ、段階的な1→5→10→30セッションを確認

GitHub owner、remote、Cloud権限、実Secrets、公開URLはこのリポジトリでは未設定。仮URLを稼働URLとして記載しない。

## 講演用操作

約6分の手順は [docs/demo_script.md](docs/demo_script.md)。

1. 「最初から体験」で保全記録1を抽出
2. 経験談から追加質問を作り、回答を反映
3. pending案を人が知識項目3 v1として承認
4. 主質問で検索・グラフ・原文ツールと根拠を表示
5. 聞き取り記録2から校正確認の更新案を作成
6. 承認前に検索へ入らないことを確認し、v2として承認
7. 「新しい会話」で履歴だけを消し、同じ質問からv2と新出典を確認

API障害時はアプリ外の録画を「記録の再生」と明示して使用する。アプリが固定回答や録画へ自動切替する機能はない。

## 障害時の挙動

- 正常検索0件: `insufficient_evidence`。検索済みであることを示す
- 検索／グラフ／原文取得失敗: jobを`failed`にし、根拠の有無を断定しない
- timeout／接続断: `indeterminate`。同一callを自動再送しない
- structured output／根拠ID／版の不整合: `validation_failed`。部分結果・自動修復なし
- 待機列満杯／期限切れ: 受付拒否または`expired`。API枠を消費しない
- 取消後の遅着結果: 利用台帳だけ確定し、回答やpending案へ公開しない
- DB／利用台帳消失: seedや満額へ自動復旧せず停止
- Graphviz表示だけの失敗: 確定済み回答・保存結果は維持し、図の表示障害として分離

一覧は [docs/failure_matrix.md](docs/failure_matrix.md)。

## テスト

通常のテストは実APIを呼ばない。

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy wg4_demo
uv run --locked python scripts/check_repository_safety.py
uv run --locked python scripts/run_load_test.py --sessions 30
```

liveテストは`RUN_LIVE_TESTS=1`、実設定、参加者パスワード、管理者が有効化した有限台帳がそろう場合だけ実行する。

```bash
RUN_LIVE_TESTS=1 LIVE_TEST_PARTICIPANT_PASSWORD='...' \
  uv run --locked --env-file .env pytest -m live
```

ユーザーが外部Spend limitを確認済みの場合だけ、全主シナリオを一時DB・有限call枠で検証できる。実行結果にAPIキーや本文は出力しない。

```bash
RUN_LIVE_TESTS=1 uv run --locked --env-file .env python \
  scripts/run_live_validation.py --mode full --reasoning-effort low \
  --call-budget 24 --confirmed-external-limit
```

## 主な構成

```text
app.py                     Streamlit entrypoint
wg4_demo/                  認証、DB、検索、グラフ、LLM、Agent、Queue、UI
data/                      架空seed、ontology、vocabulary、デモ入力
prompts/                   抽出・質問・回答・更新の制約
tests/                     非課金unit/AppTest/障害/queueと明示的liveテスト
scripts/                   ハッシュ、preflight、秘密検査、30-session模擬負荷
docs/                      deploy、運用、デモ、検証報告
runtime/                   実行時DB（Git管理外）
```

## 限界

- 共有パスワードは個人認証・企業監査・機密データ管理の代替ではない
- SQLiteと単一Cloudプロセスを前提とし、再起動をまたぐ完遂や複数replicaを保証しない
- Cloud上の知識と台帳の永続性を保証しない。台帳消失後は管理者確認が必要
- 文字列検索と小さな語彙規則であり、一般的な日本語理解・全矛盾検出・原因診断を保証しない
- Structured Outputsは形式を制御するだけで、内容の正しさを保証しない
- 30人対応は公開予定環境の実API／Cloud負荷試験を終えるまで保証しない
- 実設備の安全性・工学的妥当性を検証したアプリではない

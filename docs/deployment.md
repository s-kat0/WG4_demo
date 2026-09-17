# Streamlit Community Cloud デプロイ

## 運営者の事前作業

1. 専用OpenAIプロジェクトと、Responses呼出しに限定したキーを作る。
2. 使用する正確なモデルID、関数ツール、Structured Outputs、日本語、tokenizer対応を少数のliveテストで確認する。
3. Spend limitで強制停止を有効化する。通知だけで代用しない。
4. 参加者用と管理者用に異なるパスワードを作り、`scripts/create_password_hash.py`でArgon2idハッシュへ変換する。
5. timezone付き利用期限と確認済みRPM/TPMを決める。

## GitHubとCloud

```bash
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy wg4_demo
uv run --locked python scripts/check_repository_safety.py
git ls-files
```

指定されたGitHub repository／branchへpushし、CI通過後にCommunity Cloudで次を設定する。

- entrypoint: `app.py`
- Python: 3.12
- Secrets: `.streamlit/secrets.example.toml`と同じキー。プレースホルダーではなく実値
- `APP_ENV="cloud"`
- `APP_LLM_ENABLED=true`はモデル・支出上限確認後だけ
- `CALL_BUDGET_MODE="provider_hard_limit"`

`provider_hard_limit`では初回起動時からcall数の割当なしで有効になる。管理画面でモード、利用回数、active数を確認する。費用の強制停止はOpenAI project側hard limitを正本とし、アプリ側は共有キュー、同時実行、操作別call上限、RPM/TPM、監査記録、管理者停止を維持する。

## 公開確認

- 未認証では説明とパスワード欄以外のデータ・有料操作がない
- 参加者はStreamlitアカウントやOpenAIキーなしで共通パスワードだけを使える
- 2つの独立ブラウザでworkspace・会話・proposal・exportが混ざらない
- 1→5→10→30 browser contextsで段階試験し、最大同時3、FIFO、取消、timeout、DB lockを記録
- v5主シナリオ（初期検索、文書版A、本人役補足版B、校正条件版C）を3回以上確認
- 実在するURLとdeploy commitだけを記録

Cloud再起動後も有限call枠の追加操作は不要。ただし公開前と障害復旧時にはOpenAI側の利用量とhard limitを確認する。管理者またはprovider上限エラーによって停止した場合は、hard limit確認後に管理画面から明示的に再開する。

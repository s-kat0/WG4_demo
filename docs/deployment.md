# Streamlit Community Cloud デプロイ

参加者へ案内する資料は[participant_guide.md](participant_guide.md)、講演者が登壇中に使う資料は[demo_quick_guide.md](demo_quick_guide.md)。この文書はデプロイ担当者向けであり、Secretsの実値を参加者資料やIssueへ転記しない。

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
- ログイン画面に旧デモ互換・空の領域の選択肢がなく、ログイン後は初期12件の専用workspaceになる
- 参加者の四画面ナビゲーションに管理・領域初期化が混ざらず、JSON保存は利用できる
- サイドバー下部の「運営者用」からだけ管理画面へ進み、別の管理者パスワードを要求する
- 2つの独立ブラウザでworkspace・会話・proposal・exportが混ざらない
- 1→5→10→30 browser contextsで段階試験し、最大同時3、FIFO、取消、timeout、DB lockを記録
- v5主シナリオ（初期検索、文書版A、本人役補足版B、校正条件版C）を3回以上確認
- 実在するURLとdeploy commitだけを記録

Cloud再起動後も有限call枠の追加操作は不要。ただし公開前と障害復旧時にはOpenAI側の利用量とhard limitを確認する。管理者またはprovider上限エラーによって停止した場合は、hard limit確認後に管理画面から明示的に再開する。

## 公開直後の短縮確認

1. 未認証の独立ブラウザで、説明、注意書き、共通パスワード欄だけが見えることを確認する。
2. 参加者としてログインし、知識12件、文書6件、Q&A 6件、設備3件を確認する。
3. `冷却器1 流量低下`を通常検索し、文書とQ&Aの原文を開く。この操作ではAPIを呼ばない。
4. 既定の出口温度質問を1回実行し、根拠とツール履歴が表示されることを確認する。
5. 管理画面で利用回数、active数、失敗状態を確認し、Secretsが画面やログへ出ていないことを確認する。
6. 主シナリオ全体は[participant_guide.md](participant_guide.md)の期待結果と照合する。

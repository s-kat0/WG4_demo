# 運営手順

## 開始前

- deploy commit、`uv.lock`、prompt=`wg4-prompts-v15`、seed=`wg4-practical-seed-v5`を記録
- 独立ブラウザで参加者／管理者認証を確認
- 専用OpenAIプロジェクトのモデル権限、強制停止型支出上限、残利用量を確認
- `OPENAI_MODEL=gpt-5.6-luna`、`OPENAI_REASONING_EFFORT=medium`を固定し、Dashboardの実上限以下で`GLOBAL_RPM=60`、`GLOBAL_TPM=200000`を初期候補として確認
- 管理画面で`provider_hard_limit`モード、利用回数、active数を確認
- 30-session試験結果と当日の人数分割方針を確認
- アプリ外の録画を「記録の再生」と分かる形で用意

## 講演中

- 管理画面でenabled、budget_mode、used、activeを監視
- 待機中の参加者へ再送を促さない
- provider hard-limit、台帳障害、秘密漏えい疑いでは新規LLM要求を停止
- 失敗を固定回答・別モデル・過去結果へ置換しない

## 終了後

- 利用期限または管理画面で新規要求を停止
- OpenAI側の最終利用量を確認
- 必要な架空データだけを参加者自身がJSON export
- runtime DBを永続保存と説明しない

## 事故時

キー漏えい疑いでは、アプリ停止→キー失効・ローテーション→provider利用量確認の順。パスワード漏えいでは両パスワードと`AUTH_VERSION`を変更する。DB消失時は知識を自動seed復旧の成功として扱わず、監査台帳の過去回数も復元できないと表示・記録する。費用停止はOpenAI側hard limitが継続していることを確認する。

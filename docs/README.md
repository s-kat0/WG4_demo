# 資料索引

このフォルダには、参加者、講演者、運営者、デプロイ担当、開発者向けの資料があります。目的に合う資料だけを開いてください。

## 参加者

| 資料 | 内容 |
|---|---|
| [participant_guide.md](participant_guide.md) | 配布用の操作ガイド。手順、期待される結果、エラー時の対応 |

参加者は原則としてこの1ファイルだけで操作できます。実装仕様、Secrets、運営画面の説明は参加者向け資料には含めません。

## 講演者

| 資料 | 内容 |
|---|---|
| [demo_quick_guide.md](demo_quick_guide.md) | 登壇中に参照する短い進行表 |
| [demo_script_v5.md](demo_script_v5.md) | 時間配分、説明意図、リハーサル用の詳細手順 |

`demo_script.md`は旧リンクを壊さないための案内ファイルです。現行の進行には使用しません。

## 運営者・デプロイ担当

| 資料 | 内容 |
|---|---|
| [operator_runbook.md](operator_runbook.md) | 開始前、講演中、終了後、事故時の運営確認 |
| [deployment.md](deployment.md) | GitHub、Streamlit Community Cloud、Secrets、公開確認 |
| [failure_matrix.md](failure_matrix.md) | 障害ごとの状態、表示、自動代替・再送の禁止 |
| [validation_report.md](validation_report.md) | 現行版で実施した検査、実API確認、未確認事項 |
| [load_test_report.md](load_test_report.md) | 30セッションmockキュー試験の条件と結果 |

秘密値、平文パスワード、runtime DB、利用者データは資料へ記載しません。

## 開発者・レビュー担当

| 資料 | 内容 |
|---|---|
| [../SPEC.md](../SPEC.md) | 実装要件のsingle source of truth |
| [../CODING_AGENT_HANDOFF.md](../CODING_AGENT_HANDOFF.md) | 現行実装の入口、安全境界、変更時チェックリスト |
| [acceptance_tests_v5.md](acceptance_tests_v5.md) | v5の受入条件 |
| [seed_catalog_v5.md](seed_catalog_v5.md) | 初期知識12件の構成 |
| [sources.md](sources.md) | 実装時に参照した一次資料 |

仕様と説明資料が衝突する場合は`SPEC.md`を優先します。検証済みかどうかは`validation_report.md`で確認し、仕様書に書かれているだけの項目を実施済みとして扱わないでください。

## 文書の更新ルール

- 参加者の操作や期待結果が変わった場合：`participant_guide.md`とREADMEを更新
- 講演の順番や時間配分が変わった場合：`demo_quick_guide.md`と`demo_script_v5.md`を更新
- Secrets、上限、Cloud手順が変わった場合：`deployment.md`と`operator_runbook.md`を更新
- 実装要件が変わった場合：最初に`SPEC.md`を更新
- 検査を再実行した場合：実行日、commit、mock／実API／Cloudの区別を`validation_report.md`へ記録

古い結果を現行版の成功例として流用せず、未実施項目は未実施のまま明記します。

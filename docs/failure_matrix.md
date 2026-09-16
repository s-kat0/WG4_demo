# 障害時の結果契約

| 状況 | 結果 | 自動代替／再送 |
|---|---|---|
| API timeout・接続断 | `indeterminate`、送信済み枠を保持 | なし |
| モデル利用不可・APIエラー | `failed` | 別モデル／固定回答なし |
| 正常検索0件 | `insufficient_evidence` | 一般知識の補完なし |
| 検索・グラフ・原文取得例外 | `failed` | 空結果化・別検索なし |
| refusal・未完了・schema違反 | 専用codeまたは`validation_failed` | JSON修復・部分採用なし |
| 根拠ID・版・workspace不整合 | `validation_failed` | 似たIDへの置換なし |
| staged後のRunner失敗／取消 | `aborted`、pending非公開 | なし |
| 保存commit前失敗 | rollback、現行版維持 | UIだけ新版にしない |
| 保存状態不明 | `indeterminate`、ActionOutcome読取り照合のみ | 更新／課金の再実行なし |
| Graphviz描画だけ失敗 | 確定済み回答は維持、図だけエラー | 代替図なし |
| queue満杯・待機期限切れ | 受付拒否／`expired` | queue迂回なし |
| running取消後の遅着 | 台帳だけ確定、成果非公開 | なし |
| control DB欠損・不明schema | LLM停止、枠0 | 満額再開なし |

# 検証報告

更新日：2026年9月17日

## 対象

- code commit：496b35d
- branch：fix/participant-guide-verification-issues
- fixture：wg4-practical-seed-v5
- model：gpt-5.6-luna
- reasoning：medium
- QA・抽出・補足・更新prompt：wg4-prompts-v15
- interview prompt：wg4-interview-v2
- domain schema：2

この報告は、仕様、mock／非課金検査、ローカル実API、Streamlit Cloudの確認を区別する。古いpromptや旧フローの成功結果を現行版の成功として流用しない。

## 結論

現行コードでは、参加者向けガイドの主経路をローカルの実ブラウザと実OpenAI APIで完了できた。

- 初期12件の通常検索
- 根拠付き相談と理由・別条件の追質問
- 文書抽出と文書版v1の承認
- 空履歴の回答A
- 本人役への異なる追加質問と対話補足版v2の承認
- 空履歴の回答BとA/B比較
- 校正条件の更新案とv3の承認
- 空履歴の回答CとB/C比較
- 新しい会話で現行v3と追加原文を取得

A/B/Cはそれぞれ対象v1/v2/v3を保持し、別モデル、固定回答、過去回答、自動再送は使用しなかった。

## 固定環境

| 項目 | バージョン |
|---|---|
| Python | 3.12.10 |
| uv | 0.11.19 |
| Streamlit | 1.64.0 |
| openai | 3.14.1 |
| openai-agents | 0.22.2 |
| Pydantic | 2.13.5 |
| NetworkX | 3.6.1 |

## 非課金検証

| コマンド | 結果 |
|---|---|
| uv sync --locked | 成功。116 packages resolved、110 checked |
| uv run --locked pytest | 81 passed、1 skipped |
| uv run --locked ruff check . | 成功 |
| uv run --locked ruff format --check . | 成功。82 files already formatted |
| uv run --locked mypy wg4_demo | 成功。34 source files |
| uv run --locked python scripts/check_repository_safety.py | 成功 |
| uv run --locked python scripts/run_load_test.py --sessions 30 | 30完了、0失敗、外部API 0 |

pytestのskip 1件は、RUN_LIVE_TESTS=1を明示していないlive gateである。通常テストは外部APIを呼んでいない。

### 30セッション模擬キュー

| 指標 | 結果 |
|---|---:|
| 独立session／workspace／conversation | 30 |
| 完了 | 30 |
| 失敗 | 0 |
| 観測最大同時実行数 | 3 |
| median | 1.4874秒 |
| p95 | 1.9403秒 |
| max | 2.0611秒 |
| workspaceごとの初期知識 | 12件 |
| knowledge IDのworkspace間重複 | なし |
| 外部API呼び出し | 0 |

これはキュー、同時実行制限、所有権、workspace分離のmock試験であり、Cloud上の30人実API性能試験ではない。

## ローカル実ブラウザ・実API

.envの参加者設定をサーバー側で読み、平文パスワードやAPIキーを画面・出力へ表示せず、新規の練習領域で確認した。

### 初期検索と相談

| 操作 | 確認結果 |
|---|---|
| ログイン | 12 KnowledgeItem、文書6、Q&A 6、設備3、KB改訂1 |
| 冷却器1 流量低下 | 8件。文書とQ&Aの両方の原文を確認 |
| 出口温度表示の初回相談 | 知識項目1 v1と原文、ツール実行履歴を表示 |
| なぜこの確認が候補になるのですか | reason_explanation。本人の判断理由が未記録であることを明示 |
| 流量が下がっていた場合も... | condition_comparison。通常条件と流量低下時の知識を分けて表示 |

### 文書・対話・A/B/C

| 段階 | 確認結果 |
|---|---|
| 文書抽出 | 条件、観察、確認行動、原因未特定を分離。原因・判断理由・適用範囲・確認結果は未確認 |
| v1／回答A | KB改訂2。回答Aはsucceeded／対象v1、由来document、空履歴True |
| 本人役への聞き取り | 判断理由と適用範囲について異なる質問を生成。固定回答例はLLM生成でないと表示し、2回答後に自動挿入を停止 |
| v2／回答B | KB改訂3。回答Bはsucceeded／対象v2、由来mixed、本人役の理由・条件・原文を参照 |
| v3／回答C | KB改訂4。校正確認をaction_prerequisiteとして追加。回答Cはsucceeded／対象v3、聞き取り記録2を参照 |
| 履歴表示 | A=v1、B=v2、C=v3を保持。すべてprompt wg4-prompts-v15、空履歴True |
| 新しい会話 | 会話IDと履歴だけが変わり、KB改訂4と承認済みv3を保持。比較と同じ条件を最初に入力してv3を取得 |

各根拠付き回答でsearch_knowledge、get_context、read_evidenceが表示された。回答Bで以前確認されたvalidation_unread_evidenceは、prompt v15の通し確認では再発しなかった。

### 安全停止を確認したケース

新しい会話で一般的な質問を行った後、同じ会話の次ターンで比較用の条件へ変更したケースでは、1回validation_top_candidate_missingとなり安全停止した。代替回答、自動修復、自動再送は行われなかった。

参加者ガイドの承認後確認は、新しい会話の最初から比較と同じ条件を入力する手順とし、その経路では知識項目13 v3と追加原文の取得に成功した。曖昧な条件追加やLLM出力の揺れにより検証エラーとなる可能性は、既知の制約として残る。

## Streamlit Community Cloud

公開URL：<https://wg4-demo-kato.streamlit.app/>

過去の公開環境では、Secrets設定後の参加者ログインと、上限調整後の基本相談成功を運営者が確認している。現行branchのprompt v15、理由不足表示、比較失敗後の明示的再実行UI、参加者資料はまだpush・再デプロイしていない。

したがって、現行branchについて次は未確認である。

- Cloud上の文書抽出からA/B/Cまでの全経路
- Cloud上の複数ブラウザworkspace分離
- Cloud上の30セッション実API同時利用
- 現行commitの公開URLへの反映

## 既知の制約

- 決定的検索はbigramと小規模語彙規則で、意味検索ではない。
- 会話の仮定、訂正、設備切替は明示語に基づく。曖昧な発言は確認または安全な検証エラーになる場合がある。
- SQLite単一プロセス前提で、Cloud再起動をまたぐ永続性や複数replicaを保証しない。
- LLMの文言は実行ごとに変わり得るため、文章一致ではなく版、fact、原文、ツール実行を確認する。
- AppTestやmock成功を、Cloud公開成功、実API品質、実務上の安全性として扱わない。
- 初期知識と追加事例は架空教材であり、工学的妥当性や実設備での安全性を証明しない。

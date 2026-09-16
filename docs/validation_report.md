# 検証報告

更新日: 2026-09-17

## 対象

- fixture: `wg4-practical-seed-v5`
- prompt: `wg4-prompts-v13`
- domain schema: `2` / 公開schema表記: `wg4-schema-v2`
- 主シナリオ: 初期12件 → 文書版A → 本人役補足版B → 校正条件版C

## 固定環境

- Python 3.12.10
- uv 0.11.19
- Streamlit 1.64.0
- openai 3.14.1
- openai-agents 0.22.2
- Pydantic 2.13.5
- NetworkX 3.6.1

## v5非課金検証

2026-09-17にロック済み環境で再実行した。

| コマンド | 結果 |
|---|---|
| `uv sync --locked` | 成功（116 packages resolved、110 checked） |
| `uv run --locked pytest` | 61 passed、1 skipped |
| `uv run --locked ruff check .` | 成功 |
| `uv run --locked ruff format --check .` | 成功（77 files already formatted） |
| `uv run --locked mypy wg4_demo scripts` | 成功（39 source files） |
| `uv run --locked pip-audit` | 既知脆弱性0件 |
| `uv run --locked python scripts/check_repository_safety.py` | 成功（95 tracked or addable files） |
| `uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05` | 30完了、0失敗、最大同時3、median 1.2512秒、p95 1.6621秒、外部API 0 |
| `uv run --locked pytest --cov=wg4_demo --cov-report=term-missing:skip-covered` | 61 passed、1 skipped、総合72% |

pytestのskip 1件は、`RUN_LIVE_TESTS=1`が明示されていない既存live gate。通常テストは外部APIを呼んでいない。

### 確認したv5項目

- 初期KnowledgeItem 12件、文書6/Q&A6、12 source、35 segment、41 fact
- 全factのquoteが原文の部分文字列で、assistant質問をfact根拠にしないこと
- 破損quoteのseedをtransaction全体で拒否すること
- 旧workspaceへv5 seedを追加せず、schema v1 workspaceを追加型migrationで保持すること
- 代表検索5件、設備・由来filter、正常0件と検索例外の型分離
- 画面閲覧・通常検索でKB revisionが変わらないこと
- 文書版を動的な新規itemとして承認し、本人役補足を同一itemの新版にすること
- pending中は新segmentとdecision reasonが通常検索・原文取得へ入らないこと
- 仮定と実状の分離、明示訂正、別設備への話題変更、曖昧な`それ`の事前確認
- 理由追質問では検索1位を一律強制せず、focus、取得版、fact、原文は引き続き検証すること
- A/B/C snapshotが実payloadまたは失敗コードを保持し、空欄を模範回答で埋めないこと
- mock structured gatewayを通した聞き取り補足Proposalがstagedのままで、承認前の知識を変えないこと
- AppTestで未認証面、v5ログイン、非課金検索、画面遷移、pendingレビューを確認
- 30のv5 workspaceが各12件を持ち、knowledge IDが相互に重ならず、最大worker 3を超えないこと
- API timeoutの自動再送なし、structured output不正の自動修復なし、重複jobの二重受付なし
- `APP_LLM_ENABLED=false`では、台帳に枠があってもAPI予約を作らない実行時gate
- 待機中にKB改訂が変わったjobを`stale_context`で終了し、schedulerが後続jobを継続すること
- 同じ文書抽出操作から同一Proposalを再利用し、二重pendingを作らないこと
- 競合する旧版Proposalを承認せず、`stale`状態をrollbackせず保持すること
- `from_scratch`の新規作成・初期化が実際に空で、旧seedを混入させないこと
- APIキー、パスワード、Secrets、runtime DBがexportや安全な例外に含まれないこと

## mockでのみ確認した項目

- 文書抽出、追加質問、本人役補足、Agent回答、更新案のLLM境界
- API例外、timeout、不正structured output、SDK tool例外の伝播
- 30 session共有キュー。fake handlerの遅延を使用し、外部APIは0 call
- v5 A/B/C保存と比較。実回答本文の品質・所要時間・token量は未測定

## 実API

2026-09-17に`gpt-5.6-luna`、reasoning `low`、最大30 call、同時実行1、SDK retry 0の条件で、v5 full検証を1回実行した。最初の文書抽出APIは完了したが、結果をDBのJSON payloadから`KnowledgeDraft`へ戻す箇所がPydanticのPython strict modeで正当なenum文字列を拒否し、`ValidationError`で停止した。固定結果や別モデルへ切り替えず、A/B/Cの後続処理と自動再送は行っていない。

原因箇所は、保存JSONをJSON modeで厳格検証するよう修正した。同じ復元処理を使うStreamlit文書登録画面も修正し、JSON round-trip回帰テストを追加した。修正後の非課金テスト・静的検査・30 session負荷試験は成功している。

同日に修正後のv5 full検証を別の新規実行として1回実施した。文書抽出と対象事例v1の承認までは完了したが、比較Aで`validation_top_candidate_missing`となり停止した。比較処理が知識項目13を明示選択していた一方、検証器が通常候補検索の検索1位を常に要求していたためである。固定候補への置換や後続B/C、自動再送は行っていない。

明示的な「この知識について相談する」から開始した現在の1ターンだけ、取得済みの選択項目を第1候補として検証するよう修正した。通常の候補検索では検索1位規則を維持し、版・fact・原文・workspaceの検証も変更していない。2回目の修正後のv5 full実API検証はまだ行っていないため、A/B/Cの実API成功、所要時間、call数、token量は未確認。今後の失敗時には一時台帳を削除する前に、安全なcall数とtoken数を出力するようlive検証スクリプトを更新した。

同日に上記修正後のv5 fullを新規実行したが、最初の抽出が原文にある必須fact種別を一つ省略したため停止した。使用量は1 call、入力803 tokens、出力263 tokens、4.776秒。再送は行っていない。`gpt-5.6-luna`は維持し、講演用の推奨推論強度を`medium`へ変更した。抽出promptには、原文に明示された異なるkindを統合せず、観察・事例条件・確認行動を種類別に漏れなく対応付ける一般則を追加した。`medium`でのv5 full実API検証は未実施であり、A/B/Cの実API成功はまだ確認していない。

`medium`での次の新規実行は、文書抽出・v1承認・比較Aまでは進み、最初の追加質問が「理由」「なぜ」「考え」の語を含まなかったためliveスクリプトの固定語判定で停止した。使用量は6 calls、入力14,965 tokens、出力1,145 tokens、21.532秒。質問内容のschema違反や根拠違反ではなかった。この表層語判定は、二往復程度を目安とし質問順序・文言を固定しないv5仕様と矛盾するため削除した。代わりに、補足承認後のv2が本人回答を根拠とする`decision_reason`と`applicability`または`exception`を実際に保持することを検証する。併せて、A/B/Cを実画面から起動する経路にも明示選択マーカーを追加した。修正後の実API A/B/C通過は未確認。

過去にprompt v12・旧v3フローで`gpt-5.6-luna`、reasoning `low`のローカル実API検証が成功しているが、その47.5秒・20 calls等をv5の実績へ流用しない。

## Cloud・実画面

Streamlit Community Cloudへのpush、deploy、Cloud Secrets、公開URL、Cloud実API、Cloud 30 sessionは未実施。

スライド用のv5成功画面は、次の実行をまだ終えていないため成功例として作成していない。

- 文書抽出結果
- 本人役との対話差分
- 根拠付き複数ターン相談
- A/B比較
- 承認後のC

初期検索画面はAppTestで機能確認済み。ローカルPlaywrightでの撮影は、対応するChromium実体が未導入だったため完了せず、成功画像は作成していない。liveリハーサル時に秘密値を画面・URL・ログへ出さず、各実行IDと設定を確認して撮影する。

## 既知の制約

- 決定的検索はbigramと小規模語彙規則であり、意味検索ではない
- 会話の仮定・訂正・設備切替は明示語に基づく。曖昧な発言は確認が必要
- SQLite単一プロセス前提で、Cloud再起動をまたぐ完遂・永続性・複数replicaを保証しない
- v5実APIで、LLMが追加理由・適用範囲・校正条件を毎回正しく取得して候補へ含めるかは未確認
- AppTest・mock成功をCloud公開成功、実API品質、実務上の安全性として扱わない

# 負荷試験報告

## 非課金の模擬試験

`scripts/run_load_test.py`は通常アプリから選択できない遅延Fake handlerを依存注入し、30の独立認証session／workspace／conversationをFIFOへ投入する。最大同時数、完了数、median／p95／maxをJSONで出力する。

2026-09-16に次を実行した。

```bash
uv run --locked python scripts/run_load_test.py --sessions 30 --delay 0.05
```

| 指標 | 結果 |
|---|---:|
| 独立session／workspace／conversation | 30 |
| 完了 | 30 |
| 失敗 | 0 |
| 観測最大同時実行数 | 3 |
| median | 1.1634秒 |
| p95 | 1.4412秒 |
| max | 1.5174秒 |
| 外部API呼び出し | 0 |

これはqueue・所有権・重複防止のサービス層試験であり、OpenAI APIまたはStreamlit Community Cloudの30人ブラウザ性能試験ではない。

## 実API／Cloud

未実施。専用キー、有限台帳、Cloud権限、公開URLがそろった後、1→5→10→30 contextsで段階実施する。未測定の応答時間や「30人保証」を記載しない。

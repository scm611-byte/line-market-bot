# LINE 台股 / 美股盤後推播 Bot

## 架構
- **Render 免費 Web Service** 跑 Flask（`app.py`），處理 LINE webhook 與推播
- **GitHub Actions**（`.github/workflows/push.yml`）定時呼叫 `/tasks/tw`、`/tasks/us`
  - 台股：台北週一～五 16:00
  - 美股：台北週二～六 06:30
  - GitHub 排程可能延遲 5～30 分鐘；非交易日自動跳過
- 推播用 LINE **broadcast**，對象是 Bot 的所有好友，不需儲存訂閱名單

## 設定值
| 名稱 | 放在 | 說明 |
|---|---|---|
| `LINE_CHANNEL_SECRET` | Render 環境變數 | LINE Developers → Basic settings |
| `LINE_CHANNEL_ACCESS_TOKEN` | Render 環境變數 | LINE Developers → Messaging API |
| `CRON_SECRET` | Render 環境變數 + GitHub Secrets | 保護 `/tasks/*` 的隨機字串，兩邊要一樣 |
| `BOT_URL` | GitHub Secrets | Render 服務網址，例如 `https://line-market-bot.onrender.com` |
| `TW_WATCHLIST`、`US_WATCHLIST` | Render 環境變數（`render.yaml` 有預設） | 自選股，逗號分隔 |

LINE Webhook URL 設為 `<BOT_URL>/callback`。

## 手動測試推播
GitHub repo → Actions → 盤後推播 → Run workflow，輸入 `tw` 或 `us`。

## 本機開發
```bash
cp .env.example .env   # 填入金鑰
.venv/bin/python app.py
ngrok http 5000
```

## 注意
- Render 免費方案閒置 15 分鐘會休眠，喚醒約 30～60 秒；休眠中傳的第一則訊息可能沒有回應，再傳一次即可。
- LINE 官方帳號免費方案每月推播則數有限，broadcast 給 N 位好友算 N 則。

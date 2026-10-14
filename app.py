"""LINE 台股 / 美股盤後推播 Bot（Render 版）

- /callback：接收 LINE webhook
- /tasks/tw、/tasks/us：由 GitHub Actions 定時呼叫，broadcast 推播給所有好友
  （需帶 X-Cron-Secret header）；非交易日回 204 不推播
- 指令：台股 / 美股 / 說明

不存任何檔案：推播對象就是 Bot 的所有好友，不想收可以封鎖或把聊天室設為靜音。
"""
import hmac
import os

from dotenv import load_dotenv
from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    BroadcastRequest,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

from market import build_tw_summary, build_us_summary

load_dotenv()

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CRON_SECRET = os.environ["CRON_SECRET"]
TW_WATCHLIST = [c.strip() for c in os.getenv("TW_WATCHLIST", "").split(",") if c.strip()]
US_WATCHLIST = [c.strip().upper() for c in os.getenv("US_WATCHLIST", "").split(",") if c.strip()]

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


def broadcast(text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).broadcast(BroadcastRequest(messages=[TextMessage(text=text)]))


def reply(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


# ---------- 排程觸發 ----------
def _check_cron_secret() -> None:
    if not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), CRON_SECRET):
        abort(403)


@app.route("/tasks/tw", methods=["POST"])
def task_tw():
    _check_cron_secret()
    text = build_tw_summary(TW_WATCHLIST)
    if text is None:
        return "TW market closed or data not ready", 204
    broadcast(text)
    return "TW pushed"


@app.route("/tasks/us", methods=["POST"])
def task_us():
    _check_cron_secret()
    text = build_us_summary(US_WATCHLIST)
    if text is None:
        return "US market closed", 204
    broadcast(text)
    return "US pushed"


# ---------- Webhook ----------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "LINE bot running."


HELP_TEXT = (
    "📮 盤後推播\n"
    "・台股：交易日 16:00 左右\n"
    "・美股：交易日隔天 06:30 左右\n\n"
    "指令：\n"
    "・台股／美股（立即查詢最新摘要）\n\n"
    "不想收推播可以把聊天室設為靜音或封鎖。"
)


@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    reply(event.reply_token, "感謝加入！之後會自動收到盤後摘要 ✅\n\n" + HELP_TEXT)


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    text = event.message.text.strip()
    if text == "台股":
        summary = build_tw_summary(TW_WATCHLIST)
        reply(event.reply_token, summary or "今天台股休市，或盤後資料尚未公布（約 15:00 後）。")
    elif text == "美股":
        reply(event.reply_token, build_us_summary(US_WATCHLIST, require_today=False) or "暫時抓不到美股資料。")
    else:
        reply(event.reply_token, HELP_TEXT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

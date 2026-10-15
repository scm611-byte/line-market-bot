"""LINE 台股 / 美股盤後推播 Bot（Render 版）

- /callback：接收 LINE webhook
- /tasks/tw、/tasks/us：由 GitHub Actions 定時呼叫，broadcast 推播給所有好友
  （需帶 X-Cron-Secret header）；非交易日回 204 不推播
- /img/<名稱>.png：推播用的圖片（摘要卡、近 1 個月日 K），LINE 會來這裡抓圖
- 指令：台股 / 美股 / 說明

不需資料庫：推播對象就是 Bot 的所有好友，不想收可以封鎖或把聊天室設為靜音。
"""
import hmac
import io
import os
import re
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, request, send_file
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    BroadcastRequest,
    Configuration,
    ImageMessage,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent
from PIL import Image

from charts import render_all
from market import Report, build_tw_report, build_us_report, format_text

load_dotenv()

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CRON_SECRET = os.environ["CRON_SECRET"]
TW_WATCHLIST = [c.strip() for c in os.getenv("TW_WATCHLIST", "").split(",") if c.strip()]
US_WATCHLIST = [c.strip().upper() for c in os.getenv("US_WATCHLIST", "").split(",") if c.strip()]
# 對外網址：Render 會自動提供 RENDER_EXTERNAL_URL；本機用 ngrok 時設 PUBLIC_URL
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
IMG_DIR = Path(tempfile.gettempdir()) / "line-bot-img"
IMG_DIR.mkdir(exist_ok=True)
IMG_NAME = re.compile(r"^(tw|us)-(\d{8})-(\d)\.png$")

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


def broadcast(messages: list) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).broadcast(BroadcastRequest(messages=messages))


def reply(reply_token: str, messages: list | str) -> None:
    if isinstance(messages, str):
        messages = [TextMessage(text=messages)]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))


# ---------- 圖片 ----------
def _build_report(market: str, latest: bool = False) -> Report | None:
    if market == "tw":
        return build_tw_report(TW_WATCHLIST, latest=latest)
    return build_us_report(US_WATCHLIST, require_today=not latest)


def _save_images(r: Report) -> list[str]:
    names = []
    for i, png in enumerate(render_all(r)):
        name = f"{r.market}-{r.trade_date:%Y%m%d}-{i}.png"
        (IMG_DIR / name).write_bytes(png)
        names.append(name)
    return names


def report_messages(r: Report) -> list:
    """把報告轉成 LINE 訊息：有對外網址就送圖片，否則（或畫圖失敗）送文字"""
    if PUBLIC_URL:
        try:
            v = int(time.time())  # 讓 LINE 不要用到舊快取
            return [
                ImageMessage(
                    original_content_url=f"{PUBLIC_URL}/img/{n}?v={v}",
                    preview_image_url=f"{PUBLIC_URL}/img/{n}?preview=1&v={v}",
                )
                for n in _save_images(r)
            ]
        except Exception:
            app.logger.exception("render images failed, fallback to text")
    return [TextMessage(text=format_text(r))]


@app.route("/img/<name>", methods=["GET"])
def image(name: str):
    m = IMG_NAME.match(name)
    if not m:
        abort(404)
    path = IMG_DIR / name
    if not path.exists():
        # 免費方案重啟後 /tmp 會清空：重新產生（只有同一個交易日的才補）
        r = _build_report(m.group(1), latest=True)
        if r is None or f"{r.trade_date:%Y%m%d}" != m.group(2):
            abort(404)
        _save_images(r)
        if not path.exists():
            abort(404)
    if request.args.get("preview"):
        img = Image.open(path).convert("RGB")
        img.thumbnail((540, 1200))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg", max_age=86400)
    return send_file(path, mimetype="image/png", max_age=86400)


# ---------- 排程觸發 ----------
def _check_cron_secret() -> None:
    if not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), CRON_SECRET):
        abort(403)


@app.route("/tasks/tw", methods=["POST"])
def task_tw():
    _check_cron_secret()
    r = build_tw_report(TW_WATCHLIST)
    if r is None:
        return "TW market closed or data not ready", 204
    broadcast(report_messages(r))
    return "TW pushed"


@app.route("/tasks/us", methods=["POST"])
def task_us():
    _check_cron_secret()
    r = build_us_report(US_WATCHLIST)
    if r is None:
        return "US market closed", 204
    broadcast(report_messages(r))
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
        r = build_tw_report(TW_WATCHLIST, latest=True)
        reply(event.reply_token, report_messages(r) if r else "暫時抓不到台股資料。")
    elif text == "美股":
        r = build_us_report(US_WATCHLIST, require_today=False)
        reply(event.reply_token, report_messages(r) if r else "暫時抓不到美股資料。")
    else:
        reply(event.reply_token, HELP_TEXT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

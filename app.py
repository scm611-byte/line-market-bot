"""LINE 台股盤後推播 Bot（Render 版，僅限擁有者使用）

- /callback：接收 LINE webhook（只回應 OWNER_USER_ID）
- /tasks/tw：由 GitHub Actions 定時呼叫（需帶 X-Cron-Secret header），
  推播三張圖給擁有者：盤後摘要、近 1 個月日 K、我的庫存；非交易日回 204 不推播
- /img/<名稱>.png：推播用的圖片，網址帶簽章，猜不到別天的圖
- 自選股與庫存存在 GitHub repo 的 data/portfolio.json（見 portfolio.py）
"""
import hashlib
import hmac
import io
import os
import re
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, abort, request, send_file  # noqa: E402
from linebot.v3 import WebhookHandler  # noqa: E402
from linebot.v3.exceptions import InvalidSignatureError  # noqa: E402
from linebot.v3.messaging import (  # noqa: E402
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent  # noqa: E402
from PIL import Image  # noqa: E402

import portfolio  # noqa: E402
from charts import LOCK, render_holdings, render_kline, render_summary  # noqa: E402
from market import TPE, build_tw_report, format_text, latest_trade_date  # noqa: E402

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CRON_SECRET = os.environ["CRON_SECRET"]
OWNER_USER_ID = os.environ["OWNER_USER_ID"]
# 對外網址：Render 會自動提供 RENDER_EXTERNAL_URL；本機用 ngrok 時設 PUBLIC_URL
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
IMG_DIR = Path(tempfile.gettempdir()) / "line-bot-img"
IMG_DIR.mkdir(exist_ok=True)
IMG_NAME = re.compile(r"^tw-(\d{8})-([012])-([0-9a-f]{16})\.png$")
SUMMARY, KLINE, HOLDINGS = 0, 1, 2
FORCE_PUSH_AT = "16:00"  # 到這個時間資料還沒到齊也照推（缺的會用前一個交易日的價格）

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


def push(messages: list) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(PushMessageRequest(to=OWNER_USER_ID, messages=messages))


def reply(reply_token: str, messages: list | str) -> None:
    if isinstance(messages, str):
        messages = [TextMessage(text=messages)]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))


# ---------- 圖片 ----------
def _img_name(d: date, kind: int) -> str:
    base = f"tw-{d:%Y%m%d}-{kind}"
    sig = hmac.new(CRON_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{base}-{sig}.png"


def _render(d: date, kind: int) -> bytes | None:
    """重新產生某一天的某張圖（給 /img 在檔案不見時用）"""
    data, _ = portfolio.load()
    if kind == HOLDINGS:
        h = portfolio.build_holdings(data, d)
        with LOCK:
            return render_holdings(h)
    r = build_tw_report(data["watchlist"], today=d)
    if r is None:
        return None
    with LOCK:
        return render_summary(r) if kind == SUMMARY else render_kline(r)


def image_messages(d: date, images: list[tuple[int, bytes | None]]) -> list:
    v = int(time.time())  # 讓 LINE 不要用到舊快取
    msgs = []
    for kind, png in images:
        if png is None:
            continue
        name = _img_name(d, kind)
        (IMG_DIR / name).write_bytes(png)
        msgs.append(ImageMessage(
            original_content_url=f"{PUBLIC_URL}/img/{name}?v={v}",
            preview_image_url=f"{PUBLIC_URL}/img/{name}?preview=1&v={v}",
        ))
    return msgs


def _render_messages(r, h) -> list:
    if PUBLIC_URL:
        try:
            with LOCK:
                images = [(SUMMARY, render_summary(r)), (KLINE, render_kline(r))]
                if h:
                    images.append((HOLDINGS, render_holdings(h)))
            return image_messages(r.trade_date, images)
        except Exception:
            app.logger.exception("render images failed, fallback to text")
    texts = [format_text(r)] + ([portfolio.format_holdings_text(h)] if h else [])
    return [TextMessage(text=t) for t in texts]


def _build(data: dict, latest: bool = False):
    r = build_tw_report(data["watchlist"], latest=latest)
    if r is None:
        return None, None
    h = portfolio.build_holdings(data, r.trade_date) if (data["stocks"] or data["futures"]) else None
    return r, h


def stale_items(r, h) -> list[str]:
    """還沒更新到當天的資料（自選股日 K、庫存股票收盤、期貨結算價）"""
    items = [q.label for q in r.kline if q.ohlc is None or q.ohlc.index[-1].date() != r.trade_date]
    if h:
        items += [p.label for p in h.all if p.data_date != r.trade_date]
    return items


def market_messages(latest: bool = False) -> list | None:
    """盤後摘要 + 日 K（+ 庫存）。今天沒開盤且 latest=False 時回傳 None。"""
    data, _ = portfolio.load()
    r, h = _build(data, latest)
    return None if r is None else _render_messages(r, h)


def holdings_messages() -> list:
    data, _ = portfolio.load()
    if not (data["stocks"] or data["futures"]):
        return [TextMessage(text="目前沒有庫存。傳「說明」看如何新增。")]
    d = latest_trade_date()
    h = portfolio.build_holdings(data, d)
    if PUBLIC_URL:
        try:
            with LOCK:
                png = render_holdings(h)
            return image_messages(d, [(HOLDINGS, png)])
        except Exception:
            app.logger.exception("render holdings failed, fallback to text")
    return [TextMessage(text=portfolio.format_holdings_text(h))]


@app.route("/img/<name>", methods=["GET"])
def image(name: str):
    m = IMG_NAME.match(name)
    if not m or not hmac.compare_digest(name, _img_name(datetime.strptime(m.group(1), "%Y%m%d").date(), int(m.group(2)))):
        abort(404)
    path = IMG_DIR / name
    if not path.exists():
        # 免費方案重啟後 /tmp 會清空：重新產生
        png = _render(datetime.strptime(m.group(1), "%Y%m%d").date(), int(m.group(2)))
        if png is None:
            abort(404)
        path.write_bytes(png)
    if request.args.get("preview"):
        img = Image.open(path).convert("RGB")
        img.thumbnail((540, 1600))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg", max_age=86400)
    return send_file(path, mimetype="image/png", max_age=86400)


# ---------- 排程觸發 ----------
@app.route("/tasks/tw", methods=["POST"])
def task_tw():
    if not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), CRON_SECRET):
        abort(403)
    # 排程從 14:00 起每 15 分鐘觸發一次（cron-job.org，另有 GitHub Actions 備援）：
    # 資料全部更新到當天才推；同一個交易日只推一次；過了 FORCE_PUSH_AT 就算有缺也照推
    now = datetime.now(TPE)
    today = now.date().isoformat()
    data, _ = portfolio.load()
    if data.get("last_push") == today:
        return "already pushed today"
    r, h = _build(data)
    if r is None:
        return "TW market closed or data not ready", 204
    stale = stale_items(r, h)
    if stale and now.strftime("%H:%M") < FORCE_PUSH_AT:
        return "waiting for: " + "、".join(stale), 202
    push(_render_messages(r, h))
    data, sha = portfolio.load()
    data["last_push"] = today
    # 留最近 60 筆推播時間，方便觀察資料大約幾點到齊
    data["push_log"] = (data.get("push_log", []) + [f"{now:%Y-%m-%d %H:%M}" + (" 有缺：" + "、".join(stale) if stale else "")])[-60:]
    portfolio.save(data, sha, f"已推播 {now:%Y-%m-%d %H:%M}")
    return "TW pushed" + (" (stale: " + "、".join(stale) + ")" if stale else "")


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


HELP_TEXT = """📮 每個交易日 16:00 左右推播：盤後摘要、日 K、我的庫存

📈 查詢
台股｜立即看最新盤後（三張圖）
庫存｜只看庫存損益

⭐ 自選股
自選｜列出自選股
自選 加 2330 2454｜新增
自選 刪 2317｜移除

💼 股票庫存（數量預設單位是「張」）
買 2330 2 1050｜買 2 張、每股 1050（自動算均價）
買 2330 300股 1050｜零股
賣 2330 1｜賣 1 張
設定 2330 3 980｜直接改成 3 張、成本 980

📉 期貨庫存
多 MTX 2 23000｜小台多單 2 口、成本 23000
空 TMF 1 23100｜微台空單
多 2330期 1 1050｜台積電期貨（2330小期 = 小型）
平 MTX 1｜平倉 1 口
刪除 2330／刪除 MTX｜整筆刪除"""

NOT_OWNER = "這是私人 Bot，僅限擁有者使用。"


@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    reply(event.reply_token, "歡迎！\n\n" + HELP_TEXT if event.source.user_id == OWNER_USER_ID else NOT_OWNER)


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    if event.source.user_id != OWNER_USER_ID:
        reply(event.reply_token, NOT_OWNER)
        return
    text = event.message.text.strip()
    try:
        if text == "台股":
            reply(event.reply_token, market_messages(latest=True) or "暫時抓不到台股資料。")
        elif text == "庫存":
            reply(event.reply_token, holdings_messages())
        elif (result := portfolio.handle_command(text)) is not None:
            reply(event.reply_token, result)
        else:
            reply(event.reply_token, HELP_TEXT)
    except Exception as e:
        app.logger.exception("command failed: %s", text)
        reply(event.reply_token, f"⚠️ 處理失敗：{e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

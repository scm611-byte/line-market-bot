"""把資料畫成圖：盤後摘要卡、近 1 個月日 K 線、我的庫存

顏色依台灣習慣：紅漲綠跌。
"""
import io
import math
import threading
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from market import Quote, Report, date_title  # noqa: E402
from portfolio import Holdings, Position  # noqa: E402

FONT_DIR = Path(__file__).with_name("fonts")
for f in FONT_DIR.glob("*.ttf"):
    font_manager.fontManager.addfont(str(f))
plt.rcParams["font.family"] = "Noto Sans TC"
plt.rcParams["axes.unicode_minus"] = False

UP, DOWN, FLAT = "#E0393E", "#14A35B", "#7A7F87"
INK, MUTED, LINE, BG, CARD, ACCENT = "#1F2328", "#6B7280", "#E5E7EB", "#F3F4F6", "#FFFFFF", "#1E3A8A"
MA20_COLOR = "#8B5CF6"

WIDTH_PX, DPI = 1080, 150
LOCK = threading.Lock()  # matplotlib 不是 thread-safe，呼叫端畫圖時要拿這把鎖


def _color(v: float) -> str:
    return UP if v > 0 else DOWN if v < 0 else FLAT


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


class _Canvas:
    """以「由上往下的像素」定位文字與色塊的直式卡片"""

    def __init__(self, height: int):
        self.h = height
        self.fig = plt.figure(figsize=(WIDTH_PX / DPI, height / DPI), dpi=DPI, facecolor=BG)

    def y(self, px: float) -> float:
        return 1 - px / self.h

    @staticmethod
    def x(px: float) -> float:
        return px / WIDTH_PX

    def text(self, x, y, s, **kw):
        kw.setdefault("va", "center")
        self.fig.text(self.x(x), self.y(y), s, **kw)

    def rect(self, x, y, w, h, color):
        self.fig.patches.append(plt.Rectangle((self.x(x), self.y(y + h)), self.x(w), h / self.h,
                                              transform=self.fig.transFigure, color=color, zorder=0))

    def hline(self, y, x0=52, x1=1028):
        self.fig.add_artist(plt.Line2D([self.x(x0), self.x(x1)], [self.y(y)] * 2,
                                       transform=self.fig.transFigure, color=LINE, lw=0.8))

    def header(self, title: str, subtitle: str, height: int = 130):
        self.rect(0, 0, WIDTH_PX, height, ACCENT)
        self.text(48, 52, title, fontsize=26, weight="bold", color="white")
        self.text(48, 98, subtitle, fontsize=15, color="#C7D2FE")

    def png(self) -> bytes:
        return _png(self.fig)


def _change_text(q: Quote) -> tuple[str, str]:
    if q.change == 0:
        return "平盤", FLAT
    arrow = "▲" if q.change > 0 else "▼"
    return f"{arrow}{abs(q.change):,.{q.digits}f}  {q.pct:+.2f}%", _color(q.change)


# ---------- 摘要卡 ----------
def render_summary(r: Report) -> bytes:
    chg, chg_color = _change_text(r.index)
    sections = [("大盤", [(r.index.label, f"{r.index.close:,.2f}", chg, chg_color),
                          ("成交金額", f"{r.turnover:,.0f} 億", "", INK)])]
    if r.watchlist:
        sections.append(("自選股", [(q.label, f"{q.close:,.2f}", *_change_text(q)) for q in r.watchlist]))

    header, sec_h, row_h = 150, 70, 62
    c = _Canvas(header + sum(sec_h + row_h * len(rs) + 20 for _, rs in sections) + 30)
    c.header("台股盤後", date_title(r.trade_date))

    cur = header
    for heading, rows in sections:
        c.rect(28, cur, WIDTH_PX - 56, sec_h + row_h * len(rows) - 10, CARD)
        c.text(52, cur + 38, heading, fontsize=15, weight="bold", color=ACCENT)
        cur += sec_h
        for i, (left, mid, right, rc) in enumerate(rows):
            cy = cur + row_h / 2 - 8
            c.text(52, cy, left, fontsize=15, color=INK)
            c.text(640, cy, mid, fontsize=15, color=INK, ha="right", weight="bold")
            c.text(1028, cy, right, fontsize=15, color=rc, ha="right", weight="bold")
            if i < len(rows) - 1:
                c.hline(cur + row_h - 8)
            cur += row_h
        cur += 20
    return c.png()


# ---------- 日 K 線 ----------
def _draw_kline(ax, q: Quote) -> None:
    h = q.ohlc.copy()
    h["MA20"] = h["Close"].rolling(20).mean()
    h = h[h.index > h.index[-1] - pd.DateOffset(months=1)]

    xs = range(len(h))
    colors = [UP if u else DOWN for u in h["Close"] >= h["Open"]]
    ax.vlines(xs, h["Low"], h["High"], colors=colors, lw=1)
    body_low = h[["Open", "Close"]].min(axis=1)
    body_h = (h["Close"] - h["Open"]).abs().clip(lower=(h["High"] - h["Low"]).max() * 0.004)
    ax.bar(xs, body_h, bottom=body_low, width=0.65, color=colors, edgecolor=colors, lw=0.6)
    ax.plot(xs, h["MA20"], color=MA20_COLOR, lw=1.2)

    step = max(1, len(h) // 4)
    ticks = list(range(0, len(h), step))
    ax.set_xticks(ticks, [h.index[i].strftime("%m/%d") for i in ticks])
    ax.set_xlim(-0.8, len(h) - 0.2)
    ax.tick_params(labelsize=8, colors=MUTED, length=0)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}" if q.close >= 100 else "{x:,.2f}"))
    ax.grid(axis="y", color=LINE, lw=0.6)
    for s in ax.spines.values():
        s.set_visible(False)

    ax.set_title(q.label, loc="left", fontsize=12, weight="bold", color=INK, pad=18)
    ax.text(0, 1.02, f"{q.close:,.{q.digits}f}  {q.pct:+.2f}%", transform=ax.transAxes,
            fontsize=10, color=_color(q.change), weight="bold", va="bottom")


def render_kline(r: Report) -> bytes | None:
    items = r.kline
    if not items:
        return None
    cols = 2
    rows = math.ceil(len(items) / cols)
    header = 130
    height = header + rows * 440 + 20
    fig, axes = plt.subplots(rows, cols, figsize=(WIDTH_PX / DPI, height / DPI), dpi=DPI, facecolor=BG, squeeze=False)
    fig.subplots_adjust(left=0.04, right=0.9, top=1 - (header + 90) / height, bottom=50 / height,
                        wspace=0.28, hspace=360 / 440 * 0.55 / 0.62)
    fig.text(0.04, 1 - 48 / height, "台股・近 1 個月日 K", fontsize=18, weight="bold", color=INK, va="center")
    fig.text(0.04, 1 - 95 / height, date_title(r.trade_date), fontsize=11, color=MUTED, va="center")
    fig.text(0.76, 1 - 95 / height, "━ MA20", fontsize=10, color=MA20_COLOR, va="center")

    for ax, q in zip(axes.flat, items):
        ax.set_facecolor(CARD)
        _draw_kline(ax, q)
    for ax in list(axes.flat)[len(items):]:
        ax.set_visible(False)
    return _png(fig)


# ---------- 我的庫存 ----------
def _money(v: float) -> str:
    return f"{v:+,.0f}" if v else "0"


def render_holdings(h: Holdings) -> bytes:
    sections = [(name, ps) for name, ps in (("股票", h.stocks), ("期貨", h.futures)) if ps]
    header, total_h, sec_h, row_h = 150, 190, 66, 104
    height = header + total_h + sum(sec_h + row_h * len(ps) + 20 for _, ps in sections) + 70
    c = _Canvas(height)
    sub = f"資料日 {date_title(h.trade_date)}"
    if h.futures and h.futures_date and h.futures_date != h.trade_date:
        sub += f"｜期貨 {h.futures_date:%m/%d}"
    c.header("我的庫存", sub)

    # 合計
    cur = header
    c.rect(28, cur, WIDTH_PX - 56, total_h - 20, CARD)
    for i, (label, v) in enumerate([("當日損益", h.day_pnl), ("未實現損益", h.unrealized)]):
        x = 52 + i * 500
        c.text(x, cur + 45, label, fontsize=13, color=MUTED)
        c.text(x, cur + 105, _money(v), fontsize=26, weight="bold", color=_color(v))
    if h.stocks:
        c.text(52, cur + 150, f"股票市值 {h.stock_value:,.0f}", fontsize=12, color=MUTED)
    cur += total_h

    if not sections:
        c.text(52, cur + 20, "目前沒有庫存。傳「說明」看如何新增。", fontsize=15, color=MUTED)

    for heading, ps in sections:
        c.rect(28, cur, WIDTH_PX - 56, sec_h + row_h * len(ps) - 10, CARD)
        c.text(52, cur + 36, heading, fontsize=15, weight="bold", color=ACCENT)
        c.text(640, cur + 36, "收盤" if heading == "股票" else "結算價", fontsize=11, color=MUTED, ha="right")
        c.text(1028, cur + 36, "當日／未實現", fontsize=11, color=MUTED, ha="right")
        cur += sec_h
        for i, p in enumerate(ps):
            _holding_row(c, cur, p)
            if i < len(ps) - 1:
                c.hline(cur + row_h - 10)
            cur += row_h
        cur += 20
    c.text(52, height - 35, "損益未扣手續費與稅；期貨以結算價計算（資料來源 FinMind）", fontsize=10, color=MUTED)
    return c.png()


def _holding_row(c: _Canvas, top: float, p: Position) -> None:
    l1, l2 = top + 28, top + 66
    c.text(52, l1, p.label, fontsize=14, weight="bold", color=INK)
    c.text(52, l2, f"{p.qty_text}・成本 {p.cost:,.2f}", fontsize=12, color=MUTED)
    if p.price is None:
        c.text(1028, l1, p.note, fontsize=13, color=MUTED, ha="right")
        return
    c.text(640, l1, f"{p.price:,.2f}", fontsize=14, weight="bold", color=INK, ha="right")
    c.text(640, l2, f"{p.pct:+.2f}%", fontsize=12, color=_color(p.price - p.prev), ha="right")
    c.text(1028, l1, _money(p.day_pnl), fontsize=14, weight="bold", color=_color(p.day_pnl), ha="right")
    c.text(1028, l2, _money(p.unrealized), fontsize=12, color=_color(p.unrealized), ha="right")

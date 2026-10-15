"""把 Report 畫成兩張圖：摘要卡、近 1 個月日 K 線

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

FONT_DIR = Path(__file__).with_name("fonts")
for f in FONT_DIR.glob("*.ttf"):
    font_manager.fontManager.addfont(str(f))
plt.rcParams["font.family"] = "Noto Sans TC"
plt.rcParams["axes.unicode_minus"] = False

UP, DOWN, FLAT = "#E0393E", "#14A35B", "#7A7F87"
INK, MUTED, LINE, BG, CARD, ACCENT = "#1F2328", "#6B7280", "#E5E7EB", "#F3F4F6", "#FFFFFF", "#1E3A8A"
MA_COLORS = {5: "#F59E0B", 20: "#8B5CF6"}

WIDTH_PX, DPI = 1080, 150
_lock = threading.Lock()  # matplotlib 不是 thread-safe


def _color(v: float) -> str:
    return UP if v > 0 else DOWN if v < 0 else FLAT


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def _title(r: Report) -> str:
    return "台股盤後" if r.market == "tw" else "美股收盤"


# ---------- 摘要卡 ----------
def _quote_cells(q: Quote) -> tuple[str, str, str, str]:
    if q.change == 0:
        return q.label, f"{q.close:,.{q.digits}f}", "平盤", FLAT
    arrow = "▲" if q.change > 0 else "▼"
    return (
        q.label,
        f"{q.close:,.{q.digits}f}",
        f"{arrow}{abs(q.change):,.{q.digits}f}  {q.pct:+.2f}%",
        _color(q.change),
    )


def render_summary(r: Report) -> bytes:
    # 每個 section：(標題, [(左, 中, 右, 右邊顏色)])
    sections: list[tuple[str, list[tuple[str, str, str, str]]]] = []
    rows = [_quote_cells(q) for q in r.indices]
    if r.turnover is not None:
        rows.append(("成交金額", f"{r.turnover:,.0f} 億", "", INK))
    sections.append(("大盤" if r.market == "tw" else "指數 / 匯率", rows))
    if r.institutional:
        sections.append((
            "三大法人買賣超（億）",
            [(k, "", f"{v:+,.1f}", _color(v)) for k, v in r.institutional.items()],
        ))
    if r.watchlist:
        sections.append(("自選股", [_quote_cells(q) for q in r.watchlist]))

    header, sec_h, row_h, pad = 150, 70, 62, 40
    height = header + sum(sec_h + row_h * len(rs) + 20 for _, rs in sections) + pad
    fig = plt.figure(figsize=(WIDTH_PX / DPI, height / DPI), dpi=DPI, facecolor=BG)

    def y(px: float) -> float:  # 由上往下的像素 → figure 座標
        return 1 - px / height

    def x(px: float) -> float:
        return px / WIDTH_PX

    # 標題列
    fig.patches.append(plt.Rectangle((0, y(header - 20)), 1, (header - 20) / height,
                                     transform=fig.transFigure, color=ACCENT, zorder=0))
    fig.text(x(48), y(62), _title(r), fontsize=26, weight="bold", color="white", va="center")
    fig.text(x(48), y(104), date_title(r.trade_date), fontsize=15, color="#C7D2FE", va="center")

    cur = header
    for heading, rs in sections:
        card_h = sec_h + row_h * len(rs) - 10
        fig.patches.append(plt.Rectangle((x(28), y(cur + card_h)), x(WIDTH_PX - 56), card_h / height,
                                         transform=fig.transFigure, color=CARD, zorder=0))
        fig.text(x(52), y(cur + 38), heading, fontsize=15, weight="bold", color=ACCENT, va="center")
        cur += sec_h
        for i, (left, mid, right, rc) in enumerate(rs):
            cy = cur + row_h / 2 - 8
            fig.text(x(52), y(cy), left, fontsize=15, color=INK, va="center")
            fig.text(x(640), y(cy), mid, fontsize=15, color=INK, va="center", ha="right", weight="bold")
            fig.text(x(1028), y(cy), right, fontsize=15, color=rc, va="center", ha="right", weight="bold")
            if i < len(rs) - 1:
                fig.add_artist(plt.Line2D([x(52), x(1028)], [y(cur + row_h - 8)] * 2,
                                          transform=fig.transFigure, color=LINE, lw=0.8))
            cur += row_h
        cur += 20
    return _png(fig)


# ---------- 日 K 線 ----------
def _draw_kline(ax, q: Quote) -> None:
    h = q.ohlc.copy()
    for n in MA_COLORS:
        h[f"MA{n}"] = h["Close"].rolling(n).mean()
    start = h.index[-1] - pd.DateOffset(months=1)
    h = h[h.index > start]

    xs = range(len(h))
    up = h["Close"] >= h["Open"]
    colors = [UP if u else DOWN for u in up]
    ax.vlines(xs, h["Low"], h["High"], colors=colors, lw=1)
    body_low = h[["Open", "Close"]].min(axis=1)
    body_h = (h["Close"] - h["Open"]).abs().clip(lower=(h["High"] - h["Low"]).max() * 0.004)
    ax.bar(xs, body_h, bottom=body_low, width=0.65, color=colors, edgecolor=colors, lw=0.6)
    for n, c in MA_COLORS.items():
        ax.plot(xs, h[f"MA{n}"], color=c, lw=1.1, label=f"MA{n}")

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
    fig.text(0.04, 1 - 48 / height, f"{_title(r)}・近 1 個月日 K", fontsize=18, weight="bold", color=INK, va="center")
    fig.text(0.04, 1 - 95 / height, date_title(r.trade_date), fontsize=11, color=MUTED, va="center")
    fig.text(0.62, 1 - 95 / height, "━ MA5", fontsize=10, color=MA_COLORS[5], va="center")
    fig.text(0.76, 1 - 95 / height, "━ MA20", fontsize=10, color=MA_COLORS[20], va="center")

    for ax, q in zip(axes.flat, items):
        ax.set_facecolor(CARD)
        _draw_kline(ax, q)
    for ax in list(axes.flat)[len(items):]:
        ax.set_visible(False)
    return _png(fig)


def render_all(r: Report) -> list[bytes]:
    with _lock:
        imgs = [render_summary(r)]
        if k := render_kline(r):
            imgs.append(k)
    return imgs

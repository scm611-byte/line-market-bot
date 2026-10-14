"""台股 / 美股盤後摘要

資料來源：
- 台股大盤、成交金額：證交所 FMTQIK
- 三大法人買賣超：證交所 BFI82U
- 個股中文名稱：證交所 / 櫃買中心 OpenAPI
- 自選股與美股收盤價：Yahoo Finance（yfinance）
"""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
WEEKDAYS = "一二三四五六日"

US_INDICES = [
    ("^DJI", "道瓊"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "那斯達克"),
    ("^SOX", "費城半導體"),
    ("^VIX", "VIX"),
    ("TSM", "台積電 ADR"),
]


def _get_json(url: str, **params) -> dict | list:
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("+", ""))


def _fmt_change(close: float, prev: float, digits: int = 2) -> str:
    chg = close - prev
    pct = chg / prev * 100 if prev else 0
    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "－"
    if chg == 0:
        return f"{close:,.{digits}f} 平盤"
    return f"{close:,.{digits}f} {arrow}{abs(chg):,.{digits}f} ({pct:+.2f}%)"


def _date_title(d) -> str:
    return f"{d:%Y/%m/%d}（{WEEKDAYS[d.weekday()]}）"


# ---------- Yahoo Finance ----------
def _last_two_closes(tickers: list[str]) -> dict[str, tuple]:
    """回傳 {ticker: (最後交易日, 收盤, 前一日收盤)}"""
    if not tickers:
        return {}
    df = yf.download(tickers, period="10d", progress=False, auto_adjust=False, threads=False)["Close"]
    if isinstance(df, pd.Series):
        df = df.to_frame(tickers[0])
    out = {}
    for t in tickers:
        if t not in df:
            continue
        s = df[t].dropna()
        if len(s) >= 2:
            out[t] = (s.index[-1].date(), float(s.iloc[-1]), float(s.iloc[-2]))
    return out


# ---------- 台股 ----------
@lru_cache(maxsize=1)
def _tw_names() -> tuple[dict[str, str], set[str]]:
    """回傳 (代號→中文名稱, 上櫃代號集合)。名稱不常變，快取即可。"""
    names, otc = {}, set()
    try:
        for x in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            names[x["Code"]] = x["Name"]
    except Exception:
        pass
    try:
        for x in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"):
            names[x["SecuritiesCompanyCode"]] = x["CompanyName"]
            otc.add(x["SecuritiesCompanyCode"])
    except Exception:
        pass
    return names, otc


def _watchlist_lines(symbols: list[str], *, tw: bool) -> list[str]:
    if tw:
        names, otc = _tw_names()
        tickers = {f"{c}{'.TWO' if c in otc else '.TW'}": f"{names.get(c, '')} {c}".strip() for c in symbols}
    else:
        tickers = {s: s for s in symbols}
    quotes = _last_two_closes(list(tickers))
    lines = []
    for t, label in tickers.items():
        if t in quotes:
            _, close, prev = quotes[t]
            lines.append(f"{label}  {_fmt_change(close, prev)}")
        else:
            lines.append(f"{label}  查無資料")
    return lines


def build_tw_summary(watchlist: list[str], today=None) -> str | None:
    """今天不是台股交易日（或資料還沒出來）時回傳 None。"""
    today = today or datetime.now(TPE).date()
    roc = f"{today.year - 1911}/{today:%m/%d}"

    market = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK", date=f"{today:%Y%m%d}", response="json")
    row = next((r for r in market.get("data", []) if r[0] == roc), None)
    if row is None:
        return None
    close, chg = _num(row[4]), _num(row[5])
    turnover = _num(row[2]) / 1e8

    lines = [
        f"📊 台股盤後 {_date_title(today)}",
        f"加權指數 {_fmt_change(close, close - chg)}",
        f"成交金額 {turnover:,.0f} 億",
    ]

    inst = _get_json("https://www.twse.com.tw/rwd/zh/fund/BFI82U", type="day", dayDate=f"{today:%Y%m%d}", response="json")
    if inst.get("stat") == "OK":
        net = {r[0]: _num(r[3]) / 1e8 for r in inst["data"]}
        foreign = net.get("外資及陸資(不含外資自營商)", 0) + net.get("外資自營商", 0)
        dealer = net.get("自營商(自行買賣)", 0) + net.get("自營商(避險)", 0)
        lines += [
            "",
            "💰 三大法人買賣超（億）",
            f"外資 {foreign:+,.1f}｜投信 {net.get('投信', 0):+,.1f}",
            f"自營商 {dealer:+,.1f}｜合計 {net.get('合計', 0):+,.1f}",
        ]

    if watchlist:
        lines += ["", "⭐ 自選股", *_watchlist_lines(watchlist, tw=True)]
    return "\n".join(lines)


# ---------- 美股 ----------
def build_us_summary(watchlist: list[str], require_today: bool = True) -> str | None:
    """美股收盤後（台北清晨）執行。require_today=True 時，若紐約今天沒開盤回傳 None。"""
    quotes = _last_two_closes([t for t, _ in US_INDICES])
    if "^GSPC" not in quotes:
        return None
    trade_date = quotes["^GSPC"][0]
    if require_today and trade_date != datetime.now(NY).date():
        return None

    lines = [f"🇺🇸 美股收盤 {_date_title(trade_date)}"]
    for t, name in US_INDICES:
        if t in quotes:
            _, close, prev = quotes[t]
            lines.append(f"{name} {_fmt_change(close, prev)}")

    fx = _last_two_closes(["TWD=X"])
    if "TWD=X" in fx:
        _, close, prev = fx["TWD=X"]
        lines.append(f"美元/台幣 {_fmt_change(close, prev, digits=3)}")

    if watchlist:
        lines += ["", "⭐ 自選股", *_watchlist_lines(watchlist, tw=False)]
    return "\n".join(lines)

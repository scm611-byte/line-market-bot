"""台股盤後資料

資料來源：
- 加權指數、成交金額：證交所 FMTQIK
- 個股中文名稱：證交所 / 櫃買中心 OpenAPI
- 自選股收盤、日 K 線：Yahoo Finance（yfinance）
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TPE = ZoneInfo("Asia/Taipei")
WEEKDAYS = "一二三四五六日"


@dataclass
class Quote:
    label: str
    close: float
    prev: float
    digits: int = 2
    ohlc: pd.DataFrame | None = None  # 近 3 個月日 K（Open/High/Low/Close），畫圖用

    @property
    def change(self) -> float:
        return self.close - self.prev

    @property
    def pct(self) -> float:
        return self.change / self.prev * 100 if self.prev else 0.0


@dataclass
class Report:
    trade_date: date
    index: Quote
    turnover: float  # 億
    watchlist: list[Quote]
    kline: list[Quote] = field(default_factory=list)  # 要畫 K 線的標的（依序）


def get_json(url: str, **params) -> dict | list:
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("+", ""))


def date_title(d) -> str:
    return f"{d:%Y/%m/%d}（{WEEKDAYS[d.weekday()]}）"


# ---------- Yahoo Finance ----------
def history(tickers: list[str], until: date | None = None) -> dict[str, pd.DataFrame]:
    """回傳 {ticker: 近 3 個月日 K DataFrame}；until 之後的資料（例如盤中）會被去掉"""
    if not tickers:
        return {}
    df = yf.download(
        tickers, period="3mo", interval="1d", group_by="ticker",
        progress=False, auto_adjust=False, threads=False,
    )
    out = {}
    for t in tickers:
        if t not in df.columns.get_level_values(0):
            continue
        h = df[t][["Open", "High", "Low", "Close"]].dropna()
        if until is not None:
            h = h[h.index.date <= until]
        if len(h) >= 2:
            out[t] = h
    return out


def quote(label: str, h: pd.DataFrame | None, digits: int = 2) -> Quote | None:
    if h is None:
        return None
    return Quote(label, float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2]), digits, h)


# ---------- 股票代號 ----------
@lru_cache(maxsize=1)
def tw_names() -> tuple[dict[str, str], set[str]]:
    """回傳 (代號→中文名稱, 上櫃代號集合)。名稱不常變，快取即可。"""
    names, otc = {}, set()
    try:
        for x in get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            names[x["Code"]] = x["Name"]
    except Exception:
        pass
    try:
        for x in get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"):
            names[x["SecuritiesCompanyCode"]] = x["CompanyName"]
            otc.add(x["SecuritiesCompanyCode"])
    except Exception:
        pass
    return names, otc


def yahoo_ticker(code: str) -> str:
    _, otc = tw_names()
    return f"{code}{'.TWO' if code in otc else '.TW'}"


def stock_label(code: str) -> str:
    names, _ = tw_names()
    return f"{names.get(code, '')} {code}".strip()


# ---------- 台股 ----------
def _tw_market_row(today: date, latest: bool) -> tuple[date, list] | None:
    """證交所每日市場成交資訊。latest=True 時找不到今天就用最近一個交易日。"""
    for month_start in (today, (today.replace(day=1) - pd.Timedelta(days=1))):
        data = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
                        date=f"{month_start:%Y%m}01", response="json").get("data", [])
        for r in reversed(data):
            y, m, d = map(int, r[0].split("/"))
            row_date = date(y + 1911, m, d)
            if row_date == today or (latest and row_date < today):
                return row_date, r
        if not latest:
            break
    return None


def build_tw_report(watchlist: list[str], today=None, latest: bool = False) -> Report | None:
    """今天不是台股交易日（或資料還沒出來）時回傳 None；latest=True 則改用最近一個交易日。"""
    found = _tw_market_row(today or datetime.now(TPE).date(), latest)
    if found is None:
        return None
    today, row = found
    close, chg = _num(row[4]), _num(row[5])

    tickers = {yahoo_ticker(c): stock_label(c) for c in watchlist}
    hist = history(["^TWII", *tickers], until=today)

    # 大盤收盤用證交所官方數字，K 線用 Yahoo
    index = Quote("加權指數", close, close - chg, ohlc=hist.get("^TWII"))
    stocks = [q for t, label in tickers.items() if (q := quote(label, hist.get(t)))]
    report = Report(today, index, _num(row[2]) / 1e8, stocks)
    report.kline = [q for q in [index, *stocks] if q.ohlc is not None]
    return report


# ---------- 文字版（圖片失敗時的備援） ----------
def fmt_quote(q: Quote) -> str:
    if q.change == 0:
        return f"{q.label} {q.close:,.{q.digits}f} 平盤"
    arrow = "▲" if q.change > 0 else "▼"
    return f"{q.label} {q.close:,.{q.digits}f} {arrow}{abs(q.change):,.{q.digits}f} ({q.pct:+.2f}%)"


def format_text(r: Report) -> str:
    lines = [f"📊 台股盤後 {date_title(r.trade_date)}", fmt_quote(r.index), f"成交金額 {r.turnover:,.0f} 億"]
    if r.watchlist:
        lines += ["", "⭐ 自選股", *map(fmt_quote, r.watchlist)]
    return "\n".join(lines)


def latest_trade_date() -> date:
    found = _tw_market_row(datetime.now(TPE).date(), latest=True)
    return found[0] if found else datetime.now(TPE).date()

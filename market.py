"""台股 / 美股盤後資料

資料來源：
- 台股大盤、成交金額：證交所 FMTQIK
- 三大法人買賣超：證交所 BFI82U
- 個股中文名稱：證交所 / 櫃買中心 OpenAPI
- 自選股、美股、日 K 線：Yahoo Finance（yfinance）
"""
from dataclasses import dataclass, field
from datetime import date, datetime
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
# 美股 K 線圖要畫的指數
US_KLINE_INDICES = ["^GSPC", "^IXIC", "^SOX"]


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
    market: str  # "tw" / "us"
    trade_date: date
    indices: list[Quote]
    watchlist: list[Quote]
    turnover: float | None = None  # 億
    institutional: dict[str, float] = field(default_factory=dict)  # 億
    kline: list[Quote] = field(default_factory=list)  # 要畫 K 線的標的（依序）


def _get_json(url: str, **params) -> dict | list:
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("+", ""))


def date_title(d) -> str:
    return f"{d:%Y/%m/%d}（{WEEKDAYS[d.weekday()]}）"


# ---------- Yahoo Finance ----------
def _history(tickers: list[str], until: date | None = None) -> dict[str, pd.DataFrame]:
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


def _quote(label: str, h: pd.DataFrame | None, digits: int = 2) -> Quote | None:
    if h is None:
        return None
    return Quote(label, float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2]), digits, h)


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


def _tw_market_row(today: date, latest: bool) -> tuple[date, list] | None:
    """證交所每日市場成交資訊。latest=True 時找不到今天就用最近一個交易日。"""
    for month_start in (today, (today.replace(day=1) - pd.Timedelta(days=1))):
        data = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
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

    names, otc = _tw_names()
    tickers = {f"{c}{'.TWO' if c in otc else '.TW'}": f"{names.get(c, '')} {c}".strip() for c in watchlist}
    hist = _history(["^TWII", *tickers], until=today)

    # 大盤收盤用證交所官方數字，K 線用 Yahoo
    index = Quote("加權指數", close, close - chg, ohlc=hist.get("^TWII"))
    stocks = [q for t, label in tickers.items() if (q := _quote(label, hist.get(t)))]

    report = Report("tw", today, [index], stocks, turnover=_num(row[2]) / 1e8)

    inst = _get_json("https://www.twse.com.tw/rwd/zh/fund/BFI82U", type="day", dayDate=f"{today:%Y%m%d}", response="json")
    if inst.get("stat") == "OK":
        net = {r[0]: _num(r[3]) / 1e8 for r in inst["data"]}
        report.institutional = {
            "外資": net.get("外資及陸資(不含外資自營商)", 0) + net.get("外資自營商", 0),
            "投信": net.get("投信", 0),
            "自營商": net.get("自營商(自行買賣)", 0) + net.get("自營商(避險)", 0),
            "合計": net.get("合計", 0),
        }

    report.kline = [q for q in [index, *stocks] if q.ohlc is not None]
    return report


# ---------- 美股 ----------
def build_us_report(watchlist: list[str], require_today: bool = True) -> Report | None:
    """美股收盤後（台北清晨）執行。require_today=True 時，若紐約今天沒開盤回傳 None。"""
    index_tickers = [t for t, _ in US_INDICES]
    hist = _history([*index_tickers, "TWD=X", *watchlist])
    if "^GSPC" not in hist:
        return None
    trade_date = hist["^GSPC"].index[-1].date()
    if require_today and trade_date != datetime.now(NY).date():
        return None

    by_ticker = {t: q for t, name in US_INDICES if (q := _quote(name, hist.get(t)))}
    indices = list(by_ticker.values())
    if fx := _quote("美元/台幣", hist.get("TWD=X"), digits=3):
        indices.append(fx)
    stocks = [q for s in watchlist if (q := _quote(s, hist.get(s)))]

    kline = [by_ticker[t] for t in US_KLINE_INDICES if t in by_ticker] + stocks
    return Report("us", trade_date, indices, stocks, kline=kline)


# ---------- 文字版（圖片失敗時的備援） ----------
def _fmt(q: Quote) -> str:
    if q.change == 0:
        return f"{q.label} {q.close:,.{q.digits}f} 平盤"
    arrow = "▲" if q.change > 0 else "▼"
    return f"{q.label} {q.close:,.{q.digits}f} {arrow}{abs(q.change):,.{q.digits}f} ({q.pct:+.2f}%)"


def format_text(r: Report) -> str:
    title = "📊 台股盤後" if r.market == "tw" else "🇺🇸 美股收盤"
    lines = [f"{title} {date_title(r.trade_date)}", *map(_fmt, r.indices)]
    if r.turnover is not None:
        lines.append(f"成交金額 {r.turnover:,.0f} 億")
    if r.institutional:
        lines += ["", "💰 三大法人買賣超（億）", *(f"{k} {v:+,.1f}" for k, v in r.institutional.items())]
    if r.watchlist:
        lines += ["", "⭐ 自選股", *map(_fmt, r.watchlist)]
    return "\n".join(lines)

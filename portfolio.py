"""自選股與庫存：存取、LINE 指令、損益計算

資料存在 GitHub repo 的 data/portfolio.json（透過 GitHub API 讀寫，Render 免費方案沒有永久硬碟）。
沒設定 GITHUB_TOKEN 時改讀寫本機檔案，方便開發。

portfolio.json 格式：
{
  "watchlist": ["2330", "2317"],
  "stocks":  {"2330": {"shares": 2000, "cost": 1050.0}},
  "futures": {"MTX": {"side": "多", "qty": 2, "cost": 23000, "month": "202611"}}
}
"""
import base64
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import requests

from market import get_json, history, stock_label, tw_names, yahoo_ticker

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "scm611-byte/line-market-bot")
DATA_PATH = "data/portfolio.json"
LOCAL_FILE = Path(__file__).with_name("data") / "portfolio.json"

INDEX_FUTURES = {"MTX": ("小台", 50), "TMF": ("微台", 10)}
FUTURES_ALIASES = {"小台": "MTX", "微台": "TMF"}


# ---------- 存取 ----------
def _gh(method: str, **kw) -> requests.Response:
    r = requests.request(
        method, f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DATA_PATH}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        timeout=15, **kw,
    )
    r.raise_for_status()
    return r


def _normalize(data: dict) -> dict:
    data.setdefault("watchlist", [])
    data.setdefault("stocks", {})
    data.setdefault("futures", {})
    return data


def load() -> tuple[dict, str | None]:
    """回傳 (資料, GitHub 檔案 sha)"""
    if GITHUB_TOKEN:
        r = _gh("GET", params={"ref": "main"}).json()
        return _normalize(json.loads(base64.b64decode(r["content"]))), r["sha"]
    if LOCAL_FILE.exists():
        return _normalize(json.loads(LOCAL_FILE.read_text())), None
    return _normalize({}), None


def save(data: dict, sha: str | None, message: str) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if GITHUB_TOKEN:
        _gh("PUT", json={
            "message": f"LINE：{message}", "branch": "main", "sha": sha,
            "content": base64.b64encode(text.encode()).decode(),
        })
    elif os.getenv("RENDER"):
        # Render 的硬碟重啟就清空，寫本機檔案等於沒存
        raise RuntimeError("Render 上沒有設定 GITHUB_TOKEN，無法儲存")
    else:
        LOCAL_FILE.parent.mkdir(exist_ok=True)
        LOCAL_FILE.write_text(text)


# ---------- 期貨資料 ----------
@lru_cache(maxsize=1)
def _stock_futures() -> dict[str, tuple[str, str, int]]:
    """{契約代號: (名稱, 標的股票代號, 每口股數)}；小型個股期貨 100 股，一般 2000 股"""
    out = {}
    for x in get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
        name = x["ContractName"].removesuffix("貨")  # 台積電期貨 → 台積電期
        out[x["Contract"]] = (name, x["UnderlyingSecurityCode"], 100 if name.startswith("小型") else 2000)
    return out


def futures_spec(symbol: str) -> tuple[str, str, int] | None:
    """使用者輸入 → (契約代號, 顯示名稱, 乘數)。可輸入 MTX、小台、CDF、2330期、2330小期"""
    s = symbol.strip().upper()
    s = FUTURES_ALIASES.get(s, s)
    if s in INDEX_FUTURES:
        return s, *INDEX_FUTURES[s]
    sf = _stock_futures()
    if m := re.fullmatch(r"(\w+?)(小)?期", s):
        code, mini = m.group(1), bool(m.group(2))
        for c, (name, under, mult) in sf.items():
            if under == code and (mult == 100) == mini:
                return c, name, mult
        return None
    if s in sf:
        name, _, mult = sf[s]
        return s, name, mult
    return None


def _futures_quotes() -> tuple[date | None, dict[tuple[str, str], tuple[float, float]]]:
    """期交所每日行情（一般交易時段）。回傳 (資料日期, {(契約, 月份): (結算價, 前日結算價)})"""
    out, d = {}, None
    for x in get_json("https://openapi.taifex.com.tw/v1/DailyMarketReportFut"):
        if x["TradingSession"] != "一般" or not re.fullmatch(r"\d{6}", x["ContractMonth(Week)"]):
            continue
        try:
            settle = float(x["SettlementPrice"])
        except ValueError:
            continue
        try:  # 漲跌 = 收盤 - 前日結算價
            prev = float(x["Last"]) - float(x["Change"])
        except ValueError:
            prev = settle
        out[(x["Contract"], x["ContractMonth(Week)"])] = (settle, prev)
        d = date(int(x["Date"][:4]), int(x["Date"][4:6]), int(x["Date"][6:]))
    return d, out


def near_month(contract: str) -> str | None:
    _, q = _futures_quotes()
    months = sorted(m for c, m in q if c == contract)
    return months[0] if months else None


# ---------- 損益 ----------
@dataclass
class Position:
    kind: str  # 股票 / 期貨
    label: str
    qty_text: str
    cost: float
    price: float | None  # 收盤價（股票）/ 結算價（期貨）
    prev: float | None
    day_pnl: float = 0.0
    unrealized: float = 0.0
    value: float = 0.0  # 股票市值
    note: str = ""

    @property
    def pct(self) -> float:
        return (self.price - self.prev) / self.prev * 100 if self.price and self.prev else 0.0


@dataclass
class Holdings:
    trade_date: date
    stocks: list[Position] = field(default_factory=list)
    futures: list[Position] = field(default_factory=list)
    futures_date: date | None = None

    @property
    def all(self) -> list[Position]:
        return self.stocks + self.futures

    @property
    def day_pnl(self) -> float:
        return sum(p.day_pnl for p in self.all)

    @property
    def unrealized(self) -> float:
        return sum(p.unrealized for p in self.all)

    @property
    def stock_value(self) -> float:
        return sum(p.value for p in self.stocks)


def qty_text(shares: int) -> str:
    lots, odd = divmod(shares, 1000)
    if lots and odd:
        return f"{lots} 張 {odd} 股"
    return f"{lots} 張" if lots else f"{odd} 股"


def build_holdings(data: dict, trade_date: date) -> Holdings:
    h = Holdings(trade_date)

    if data["stocks"]:
        tickers = {code: yahoo_ticker(code) for code in data["stocks"]}
        hist = history(list(tickers.values()), until=trade_date)
        for code, pos in data["stocks"].items():
            shares, cost = int(pos["shares"]), float(pos["cost"])
            p = Position("股票", stock_label(code), qty_text(shares), cost, None, None)
            if (df := hist.get(tickers[code])) is not None:
                p.price, p.prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
                p.day_pnl = (p.price - p.prev) * shares
                p.unrealized = (p.price - cost) * shares
                p.value = p.price * shares
            else:
                p.note = "查無報價"
            h.stocks.append(p)

    if data["futures"]:
        h.futures_date, quotes = _futures_quotes()
        for contract, pos in data["futures"].items():
            spec = futures_spec(contract)
            name, mult = (spec[1], spec[2]) if spec else (contract, 0)
            sign = 1 if pos["side"] == "多" else -1
            qty, cost, month = int(pos["qty"]), float(pos["cost"]), pos.get("month", "")
            p = Position("期貨", f"{name} {contract} {month}", f"{pos['side']} {qty} 口", cost, None, None)
            if (q := quotes.get((contract, month))) and mult:
                p.price, p.prev = q
                p.day_pnl = (p.price - p.prev) * qty * mult * sign
                p.unrealized = (p.price - cost) * qty * mult * sign
            else:
                p.note = "已到期或查無報價"
            h.futures.append(p)
    return h


def format_holdings_text(h: Holdings) -> str:
    if not h.all:
        return "目前沒有庫存。輸入「說明」看如何新增。"
    lines = [f"💼 我的庫存 {h.trade_date:%m/%d}"]
    for p in h.all:
        if p.price is None:
            lines.append(f"{p.label} {p.qty_text} 成本 {p.cost:,.2f}（{p.note}）")
        else:
            lines.append(f"{p.label} {p.qty_text} 成本 {p.cost:,.2f} 收 {p.price:,.2f}"
                         f"｜當日 {p.day_pnl:+,.0f}｜未實現 {p.unrealized:+,.0f}")
    lines.append(f"合計：當日 {h.day_pnl:+,.0f}｜未實現 {h.unrealized:+,.0f}")
    return "\n".join(lines)


# ---------- LINE 指令 ----------
NUM = r"(\d+(?:\.\d+)?)"
QTY = r"(\d+)\s*(股|張)?"


def _shares(n: str, unit: str | None) -> int:
    return int(n) if unit == "股" else int(n) * 1000


def _check_stock(code: str) -> str | None:
    names, _ = tw_names()
    return None if code in names else f"找不到股票代號 {code}（目前支援上市、上櫃股票）"


def handle_command(text: str) -> str | None:
    """處理庫存 / 自選股指令。不是這類指令就回傳 None。"""
    t = re.sub(r"\s+", " ", text.strip())

    # 自選股
    if t == "自選":
        data, _ = load()
        wl = data["watchlist"]
        return "⭐ 自選股：\n" + "\n".join(stock_label(c) for c in wl) if wl else "目前沒有自選股。輸入「自選 加 2330」新增。"
    if m := re.fullmatch(r"自選 (加|刪) ([\w ]+)", t):
        codes = m.group(2).split()
        data, sha = load()
        wl = data["watchlist"]
        if m.group(1) == "加":
            if err := next(filter(None, map(_check_stock, codes)), None):
                return err
            wl += [c for c in codes if c not in wl]
        else:
            data["watchlist"] = wl = [c for c in wl if c not in codes]
        save(data, sha, t)
        return "✅ 自選股已更新：\n" + ("\n".join(stock_label(c) for c in wl) or "（空）")

    # 股票
    if m := re.fullmatch(rf"(買|設定) (\w+) {QTY} {NUM}", t):
        action, code, shares, price = m.group(1), m.group(2), _shares(m.group(3), m.group(4)), float(m.group(5))
        if err := _check_stock(code):
            return err
        data, sha = load()
        pos = data["stocks"].get(code)
        if action == "買" and pos:
            total = pos["shares"] + shares
            cost = (pos["shares"] * pos["cost"] + shares * price) / total
            pos.update(shares=total, cost=round(cost, 4))
        else:
            data["stocks"][code] = pos = {"shares": shares, "cost": price}
        save(data, sha, t)
        return f"✅ {stock_label(code)}：{qty_text(pos['shares'])}，平均成本 {pos['cost']:,.2f}"
    if m := re.fullmatch(rf"賣 (\w+) {QTY}", t):
        code, shares = m.group(1), _shares(m.group(2), m.group(3))
        data, sha = load()
        pos = data["stocks"].get(code)
        if not pos:
            return f"庫存裡沒有 {code}"
        if shares > pos["shares"]:
            return f"{stock_label(code)} 只有 {qty_text(pos['shares'])}，不能賣 {qty_text(shares)}"
        pos["shares"] -= shares
        if pos["shares"] == 0:
            del data["stocks"][code]
        save(data, sha, t)
        left = qty_text(pos["shares"]) if pos["shares"] else "已全部賣出"
        return f"✅ {stock_label(code)}：{left}"

    # 期貨
    if m := re.fullmatch(rf"(多|空) (\S+) (\d+) {NUM}(?: (\d{{6}}))?", t):
        side, sym, qty, price, want_month = m.group(1), m.group(2), int(m.group(3)), float(m.group(4)), m.group(5)
        if not (spec := futures_spec(sym)):
            return f"不支援的期貨：{sym}（支援 MTX 小台、TMF 微台、個股期貨如 2330期、2330小期 或 CDF）"
        contract, name, _ = spec
        data, sha = load()
        pos = data["futures"].get(contract)
        if pos and pos["side"] == side:
            total = pos["qty"] + qty
            pos.update(qty=total, cost=round((pos["qty"] * pos["cost"] + qty * price) / total, 4))
        elif pos:  # 反向 = 平倉
            if qty > pos["qty"]:
                return f"{name} 目前是{pos['side']}單 {pos['qty']} 口，反向下單不能超過持有口數"
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del data["futures"][contract]
        else:
            month = want_month or near_month(contract)
            if not month:
                return f"查不到 {name} 的近月合約"
            data["futures"][contract] = pos = {"side": side, "qty": qty, "cost": price, "month": month}
        save(data, sha, t)
        if contract not in data["futures"]:
            return f"✅ {name} 已全部平倉"
        return f"✅ {name} {contract} {pos['month']}：{pos['side']} {pos['qty']} 口，平均成本 {pos['cost']:,.2f}"
    if m := re.fullmatch(r"平 (\S+) (\d+)", t):
        if not (spec := futures_spec(m.group(1))):
            return f"不支援的期貨：{m.group(1)}"
        contract, name, _ = spec
        data, sha = load()
        pos = data["futures"].get(contract)
        if not pos:
            return f"庫存裡沒有 {name}"
        qty = int(m.group(2))
        if qty > pos["qty"]:
            return f"{name} 只有 {pos['qty']} 口"
        pos["qty"] -= qty
        if pos["qty"] == 0:
            del data["futures"][contract]
        save(data, sha, t)
        return f"✅ {name}：" + (f"剩 {pos['qty']} 口" if pos["qty"] else "已全部平倉")

    # 刪除
    if m := re.fullmatch(r"刪除 (\S+)", t):
        key = m.group(1)
        data, sha = load()
        spec = futures_spec(key) if key not in data["stocks"] else None
        if key in data["stocks"]:
            del data["stocks"][key]
        elif spec and spec[0] in data["futures"]:
            del data["futures"][spec[0]]
        else:
            return f"庫存裡沒有 {key}"
        save(data, sha, t)
        return f"✅ 已刪除 {key}"
    return None

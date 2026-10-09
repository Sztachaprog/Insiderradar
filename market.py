"""Prices and company profiles from Yahoo Finance (free, no API key).

Chart endpoint needs only a browser User-Agent; the profile endpoint needs a
cookie + crumb, fetched lazily once per session.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
PROFILE_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
REQUEST_DELAY = 0.3


def yahoo_symbol(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B


class YahooClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        self._crumb: str | None = None
        self._last = 0.0

    def _get(self, url: str, **params) -> dict:
        wait = REQUEST_DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, params=params or None, timeout=20)
        self._last = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def chart(self, ticker: str, range_: str = "6mo") -> dict:
        """Daily closes plus the live price: {"price", "name", "closes": [(date, close)]}."""
        data = self._get(CHART_URL.format(symbol=yahoo_symbol(ticker)), range=range_, interval="1d")
        res = data["chart"]["result"][0]
        meta = res["meta"]
        closes_raw = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [(datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), round(c, 4))
                  for ts, c in zip(res.get("timestamp", []), closes_raw) if c is not None]
        return {"price": meta.get("regularMarketPrice"),
                "name": meta.get("longName") or meta.get("shortName") or "",
                "closes": closes}

    def profile(self, ticker: str) -> dict:
        if self._crumb is None:
            self.session.get("https://fc.yahoo.com", timeout=20)  # sets the consent cookie (404 is fine)
            self._crumb = self.session.get(CRUMB_URL, timeout=20).text.strip()
        data = self._get(PROFILE_URL.format(symbol=yahoo_symbol(ticker)),
                         modules="assetProfile,price", crumb=self._crumb)
        res = (data.get("quoteSummary", {}).get("result") or [{}])[0]
        prof, price = res.get("assetProfile", {}), res.get("price", {})
        return {
            "name": price.get("longName") or price.get("shortName") or "",
            "sector": prof.get("sector", ""),
            "industry": prof.get("industry", ""),
            "country": prof.get("country", ""),
            "website": prof.get("website", ""),
            "employees": prof.get("fullTimeEmployees"),
            "summary": prof.get("longBusinessSummary", ""),
            "market_cap": (price.get("marketCap") or {}).get("raw"),
            "exchange": price.get("exchangeName", ""),
        }


# ---------- Performance math (pure, testable) ----------

HORIZONS = [(1, "1 sesja"), (3, "3 sesje"), (5, "1 tydz."), (20, "1 mies.")]


def price_on(closes: list[tuple[str, float]], day: str) -> tuple[int, float] | None:
    """Index and close of the last session on or before `day`."""
    found = None
    for i, (d, c) in enumerate(closes):
        if d > day:
            break
        found = (i, c)
    return found


def performance(closes: list[tuple[str, float]], base_date: str, base_price: float,
                bench: list[tuple[str, float]] | None = None) -> dict:
    """Return since base and at fixed session horizons, optionally vs a benchmark."""
    out = {"now": None, "alpha_now": None, "h": {}, "path": []}
    if not closes or not base_price:
        return out
    start = price_on(closes, base_date)
    i0 = start[0] if start else -1
    after = closes[i0 + 1:]  # sessions strictly after the base session
    last = closes[-1][1]
    out["now"] = last / base_price - 1
    out["path"] = [(d, c / base_price - 1) for d, c in [(base_date, base_price), *after]]

    bench_base = price_on(bench, base_date) if bench else None

    def bench_ret(day: str) -> float | None:
        if not bench_base:
            return None
        p = price_on(bench, day)
        return p[1] / bench_base[1] - 1 if p else None

    b_now = bench_ret(closes[-1][0])
    if b_now is not None:
        out["alpha_now"] = out["now"] - b_now
    for n, _ in HORIZONS:
        if len(after) >= n:
            d, c = after[n - 1]
            r = c / base_price - 1
            b = bench_ret(d)
            out["h"][n] = {"ret": r, "alpha": None if b is None else r - b}
    return out


def summarize(perfs: list[dict]) -> list[dict]:
    """Average / win-rate stats per horizon over many tracked filings."""
    rows = []
    for key, label in [("now", "Od dodania"), *[(n, l) for n, l in HORIZONS]]:
        if key == "now":
            rets = [p["now"] for p in perfs if p["now"] is not None]
            alphas = [p["alpha_now"] for p in perfs if p["alpha_now"] is not None]
        else:
            hs = [p["h"][key] for p in perfs if key in p["h"]]
            rets = [h["ret"] for h in hs]
            alphas = [h["alpha"] for h in hs if h["alpha"] is not None]
        wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
        avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        rows.append({"label": label, "n": len(rets), "avg": avg(rets),
                     "median": sorted(rets)[len(rets) // 2] if rets else None,
                     "win_rate": len(wins) / len(rets) if rets else None,
                     "avg_win": avg(wins), "avg_loss": avg(losses), "alpha": avg(alphas)})
    return rows


def average_path(perfs: list[dict], max_sessions: int = 30) -> list[float | None]:
    """Mean cumulative return by session index since base (index 0 = 0%)."""
    out = []
    for i in range(max_sessions + 1):
        vals = [p["path"][i][1] for p in perfs if len(p["path"]) > i]
        out.append(sum(vals) / len(vals) if vals else None)
    while out and out[-1] is None:
        out.pop()
    return out


def price_context(closes: list[tuple[str, float]], day: str) -> dict | None:
    """Where the stock stood when the insider bought: vs 52-week high/low and the prior month."""
    at = price_on(closes, day)
    if not at or at[0] < 20:
        return None
    i, price = at
    window = [c for _, c in closes[max(0, i - 251):i + 1]]
    hi, lo = max(window), min(window)
    return {
        "price": price, "high": hi, "low": lo,
        "from_high": price / hi - 1,
        "from_low": price / lo - 1,
        "range_pos": (price - lo) / (hi - lo) if hi > lo else 0.5,  # 0 = at the low, 1 = at the high
        "month_before": price / closes[i - 21][1] - 1 if i >= 21 else None,
        "sessions": len(window),
    }

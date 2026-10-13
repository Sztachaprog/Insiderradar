"""Price candles (OHLC) around every crypto signal - the raw material for strategy tests.

For each signal (crypto_events row) we keep a fixed window measured from the moment
of detection, whatever any single trade does:
    - 1-minute candles for the first 48 hours,
    - 1-hour candles from 24 hours before detection to 7 days after.
Candles carry the high and low of every period, so a simulated take-profit or
stop-loss is known to have been touched even between our scans.

Sources (free, no key):
    1. Binance market-data mirror (data-api.binance.vision; works from US servers too)
       for coins listed against USDT - minute history goes back years.
    2. GeckoTerminal for small tokens that only trade on DEXes - minute history is
       short (days), so these candles must be collected while the window is open.
A source is accepted only if its price at the detection minute matches the price the
radar recorded (same ticker on Binance can be a different coin).
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

import crypto

BINANCE = "https://data-api.binance.vision/api/v3"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
MINUTE_HOURS = 48
HOURLY_BEFORE_H = 24
HOURLY_DAYS = 7
PRICE_TOLERANCE = 0.15        # source price at detection vs. our recorded price
MIN_POOL_LIQUIDITY = 20_000   # $ - ignore dust pools on GeckoTerminal
GT_CALLS_PER_RUN = int(os.environ.get("GT_CALLS_PER_RUN", "10"))  # GeckoTerminal rate-limits hard (429)
GT_SPACING_S = 6.5            # ...so calls are spaced out, and a 429 pauses it until the next run
RESOLVE_PER_RUN = 20          # new signals matched to a source per run
RETRY_NONE_H = 6              # "no source found" is retried while the signal's minute window is open
RES_MIN, RES_HOUR = 60, 3600


def epoch(iso: str) -> int:
    return int(crypto.parse_iso(iso).timestamp())


def window(detected_at: str) -> dict[int, tuple[int, int]]:
    t0 = epoch(detected_at) // 60 * 60
    return {RES_MIN: (t0, t0 + MINUTE_HOURS * 3600),
            RES_HOUR: (t0 // 3600 * 3600 - HOURLY_BEFORE_H * 3600, t0 + HOURLY_DAYS * 86400)}


# ---------- storage ----------

class CandleStore:
    def __init__(self, path):
        self.path = str(path)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS candles (
                event_id INTEGER NOT NULL, res INTEGER NOT NULL, ts INTEGER NOT NULL,
                o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL,
                PRIMARY KEY (event_id, res, ts)) WITHOUT ROWID""")
            c.execute("""CREATE TABLE IF NOT EXISTS candle_sources (
                event_id INTEGER PRIMARY KEY, source TEXT NOT NULL, ref TEXT, checked_at TEXT NOT NULL)""")

    def _conn(self):
        return sqlite3.connect(self.path)

    def sources(self) -> dict[int, dict]:
        with self._conn() as c:
            return {e: {"source": s, "ref": r, "checked_at": t}
                    for e, s, r, t in c.execute("SELECT event_id, source, ref, checked_at FROM candle_sources")}

    def set_source(self, event_id: int, source: str, ref: str | None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO candle_sources VALUES (?, ?, ?, ?)",
                      (event_id, source, ref, crypto.iso(crypto.utcnow())))

    def save(self, event_id: int, res: int, rows: list[tuple]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          [(event_id, res, *r) for r in rows])

    def last_ts(self) -> dict[tuple[int, int], int]:
        with self._conn() as c:
            return {(e, r): t for e, r, t in c.execute("SELECT event_id, res, MAX(ts) FROM candles GROUP BY 1, 2")}

    def paths(self, event_ids: list[int] | None = None) -> dict[int, list[tuple]]:
        """Per signal: minute candles while they exist, hourly candles after the last minute one.
        Each item: (iso_ts, open, high, low, close). event_ids limits what is loaded (the table is big)."""
        rows: dict[tuple[int, int], list] = {}
        sql, args = "SELECT event_id, res, ts, o, h, l, c FROM candles", []
        if event_ids is not None:
            if not event_ids:
                return {}
            sql += f" WHERE event_id IN ({','.join('?' * len(event_ids))})"
            args = list(event_ids)
        with self._conn() as c:
            for e, r, t, o, h, l_, cl in c.execute(sql + " ORDER BY 1, 2, 3", args):
                rows.setdefault((e, r), []).append((t, o, h, l_, cl))
        out: dict[int, list[tuple]] = {}
        for e in {k[0] for k in rows}:
            minutes, hours = rows.get((e, RES_MIN), []), rows.get((e, RES_HOUR), [])
            end = minutes[-1][0] + RES_MIN if minutes else 0
            merged = minutes + [x for x in hours if x[0] >= end]
            out[e] = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), o, h, l_, cl)
                      for t, o, h, l_, cl in merged]
        return out


# ---------- sources ----------

class CandleClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "insider-radar/1.0", "Accept": "application/json"})
        self._symbols: set[str] | None = None
        self.gt_calls = 0
        self.gt_blocked = False
        self._gt_last = 0.0

    # Binance
    def binance_symbols(self) -> set[str]:
        if self._symbols is None:
            resp = self.session.get(f"{BINANCE}/ticker/price", timeout=30)
            resp.raise_for_status()
            self._symbols = {x["symbol"] for x in resp.json() if x["symbol"].endswith("USDT")}
        return self._symbols

    def binance(self, symbol: str, res: int, start: int, end: int) -> list[tuple]:
        """Candles with open time in [start, end) as (ts, o, h, l, c, quote_volume)."""
        interval = "1m" if res == RES_MIN else "1h"
        out, cursor = [], start
        while cursor < end:
            resp = self.session.get(f"{BINANCE}/klines", timeout=30, params={
                "symbol": symbol, "interval": interval, "startTime": cursor * 1000,
                "endTime": (end - 1) * 1000, "limit": 1000})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            out += [(k[0] // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in batch]
            cursor = batch[-1][0] // 1000 + res
            if len(batch) < 1000:
                break
        return out

    # GeckoTerminal
    def _gt(self, path: str, **params) -> dict:
        if self.gt_blocked:
            raise requests.HTTPError("GeckoTerminal paused after a rate limit")
        wait = GT_SPACING_S - (time.monotonic() - self._gt_last)
        if wait > 0:
            time.sleep(wait)
        self.gt_calls += 1
        resp = self.session.get(GECKOTERMINAL + path, params=params, timeout=30)
        self._gt_last = time.monotonic()
        if resp.status_code == 429:
            self.gt_blocked = True
        resp.raise_for_status()
        return resp.json()

    @property
    def gt_available(self) -> bool:
        return not self.gt_blocked and self.gt_calls < GT_CALLS_PER_RUN

    def gt_pools(self, symbol: str) -> list[dict]:
        """Liquid pools whose base token has this symbol, most liquid first."""
        data = self._gt("/search/pools", query=symbol).get("data", [])
        pools = []
        for p in data:
            a = p.get("attributes", {})
            base = (a.get("name") or "").split(" / ")[0].strip()
            if base.upper() == symbol.upper() and float(a.get("reserve_in_usd") or 0) >= MIN_POOL_LIQUIDITY:
                pools.append({"id": p["id"], "liquidity": float(a["reserve_in_usd"]),
                              "price": float(a.get("base_token_price_usd") or 0)})
        return sorted(pools, key=lambda x: -x["liquidity"])

    def gt(self, pool_id: str, res: int, start: int, end: int) -> list[tuple]:
        """GeckoTerminal returns newest first and pages backwards from before_timestamp."""
        network, address = pool_id.split("_", 1)
        timeframe = "minute" if res == RES_MIN else "hour"
        out, before = {}, end
        while before > start:
            rows = self._gt(f"/networks/{network}/pools/{address}/ohlcv/{timeframe}", aggregate=1, limit=1000,
                            before_timestamp=before, currency="usd", token="base").get("data", {}) \
                .get("attributes", {}).get("ohlcv_list", [])
            fresh = [r for r in rows if start <= int(r[0]) < end]
            for r in fresh:
                out[int(r[0])] = (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
            if len(rows) < 1000 or not fresh:
                break
            before = min(int(r[0]) for r in rows)
        return [out[t] for t in sorted(out)]


def _price_matches(candles: list[tuple], at: int, price: float) -> bool:
    """Close of the candle at (or just before) the detection minute vs. the price we recorded."""
    near = [c for c in candles if c[0] <= at] or candles[:1]
    return bool(near) and price > 0 and abs(near[-1][4] / price - 1) <= PRICE_TOLERANCE


class Retry(Exception):
    """A source could not be checked right now (network, rate limit): try again next run."""


def _close_to(price: float, ref: float | None) -> bool:
    return bool(ref) and price > 0 and abs(price / ref - 1) <= PRICE_TOLERANCE


def resolve(client: CandleClient, event: dict, latest_price: float | None = None) -> tuple[str, str | None]:
    """Find a candle source whose price matches ours: ('binance', symbol) | ('geckoterminal', pool_id) |
    ('none', None). The check uses the detection minute when the source still has it; GeckoTerminal keeps
    minute candles only for hours, so for older signals the pool's current price is compared with our
    latest recorded price instead. Raises Retry on network errors (never store 'none' for those)."""
    at = epoch(event["detected_at"]) // 60 * 60
    probe = (at - 10 * 60, at + 60)
    symbol = f"{event['symbol'].upper()}USDT"
    try:
        if symbol in client.binance_symbols() and _price_matches(client.binance(symbol, RES_MIN, *probe), at,
                                                                 event["base_price"]):
            return "binance", symbol
        if not client.gt_available:
            raise Retry("GeckoTerminal budget for this run is used up")
        pools = client.gt_pools(event["symbol"])
        for pool in sorted(pools, key=lambda x: -x["liquidity"])[:5]:
            if _close_to(pool["price"], latest_price) or (not latest_price and _close_to(pool["price"], event["base_price"])):
                if not client.gt_available:
                    raise Retry("GeckoTerminal budget for this run is used up")
                history = client.gt(pool["id"], RES_MIN, *probe)
                if not history or _price_matches(history, at, event["base_price"]):
                    return "geckoterminal", pool["id"]
    except requests.RequestException as exc:
        raise Retry(str(exc)) from exc
    return "none", None


def update(client: CandleClient, store: CandleStore, events: list[dict], now: datetime | None = None,
           latest_prices: dict[str, float] | None = None) -> dict:
    """Match new signals to a source and append the candles that appeared since the last run.
    latest_prices: coin_id -> our latest recorded price (to verify DEX pools for older signals)."""
    latest_prices = latest_prices or {}
    now_ts = int((now or crypto.utcnow()).timestamp()) // 60 * 60
    sources, last = store.sources(), store.last_ts()
    stats = {"resolved": 0, "binance": 0, "geckoterminal": 0, "none": 0, "candles": 0, "errors": 0, "deferred": 0}
    live = [e for e in events if now_ts < epoch(e["detected_at"]) + HOURLY_DAYS * 86400 + 3600]

    # 1. Match new signals to a source (newest first: they are the ones being traded right now).
    for e in sorted(live, key=lambda e: e["detected_at"], reverse=True):
        src = sources.get(e["id"])
        retry_none = src and src["source"] == "none" and \
            crypto.parse_iso(src["checked_at"]) < (now or crypto.utcnow()) - timedelta(hours=RETRY_NONE_H) and \
            now_ts < epoch(e["detected_at"]) + MINUTE_HOURS * 3600
        if (src is None or retry_none) and stats["resolved"] < RESOLVE_PER_RUN:
            try:
                kind, ref = resolve(client, e, latest_prices.get(e["coin_id"]))
            except Retry:
                stats["deferred"] += 1   # network hiccup or DEX budget used up: tried again next run
                continue
            store.set_source(e["id"], kind, ref)
            sources[e["id"]] = {"source": kind, "ref": ref, "checked_at": crypto.iso(crypto.utcnow())}
            stats["resolved"] += 1

    # 2. Append candles: every Binance signal (cheap), then DEX signals that waited longest (rate-limited).
    todo = []
    for e in live:
        src = sources.get(e["id"])
        if not src:
            continue
        stats[src["source"]] = stats.get(src["source"], 0) + 1
        if src["source"] == "none":
            continue
        for res, (start, end) in window(e["detected_at"]).items():
            start = max(start, last.get((e["id"], res), start - res) + res)
            stop = min(end, now_ts if res == RES_MIN else now_ts // 3600 * 3600)  # only finished periods
            if start < stop:
                todo.append((src["source"] != "binance", start, e["id"], res, src, stop))
    for is_dex, start, event_id, res, src, stop in sorted(todo, key=lambda t: (t[0], t[1])):
        if is_dex and not client.gt_available:
            continue
        try:
            fetch = client.gt if is_dex else client.binance
            rows = [r for r in fetch(src["ref"], res, start, stop) if start <= r[0] < stop]
        except requests.RequestException:
            stats["errors"] += 1
            continue
        store.save(event_id, res, rows)
        stats["candles"] += len(rows)
    return stats

"""Two months of radar history from Binance, so strategies can be tested on hundreds of
signals right away instead of waiting weeks for live ones.

The radar is replayed on hourly Binance candles for every USDT pair whose coin is in the
live radar's universe (CoinGecko top 1000, market cap $3M-1.5B). Wherever a coin met the
radar's conditions (24h +-25%, 1h +-10%, volume >= 60% of market cap), a signal is stored
with origin='backfill', together with the same candle window live signals get (1-minute
for 48 h, hourly from -24 h to +7 days), and every strategy's trades go to the journal.

Limits compared with live signals: detection is at the end of the hour that met the
condition; market cap is today's (no free history); no news / trending / project data.
Runs inside the GitHub Actions scan in time-boxed chunks and resumes where it stopped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

import candles
import crypto
import strategy

DAYS = 60
CHUNK_SECONDS = 300           # per scan run; the rest continues in the next run
SKIP_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "EURI", "AEUR", "XUSD", "USD1", "BFUSD", "PAXG", "WBTC",
              "WBETH", "BNSOL"}


def _ensure(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS backfill_progress (
        symbol TEXT PRIMARY KEY, signals INTEGER NOT NULL, done_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS backfill_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")


def universe(cg_client, session: requests.Session) -> list[dict]:
    """Binance USDT pairs whose coin the live radar would watch (by today's market cap)."""
    caps: dict[str, dict] = {}
    for page in range(1, crypto.PAGES + 1):
        for c in cg_client.markets(page):
            sym = (c.get("symbol") or "").upper()
            if sym and (sym not in caps or (c.get("market_cap") or 0) > caps[sym]["market_cap"]):
                caps[sym] = {"id": c["id"], "name": c["name"], "image": c.get("image", ""),
                             "market_cap": c.get("market_cap") or 0, "rank": c.get("market_cap_rank")}
    info = session.get(f"{candles.BINANCE}/exchangeInfo", params={"permissions": "SPOT"}, timeout=60)
    info.raise_for_status()
    out = []
    for x in info.json()["symbols"]:
        base, c = x["baseAsset"], caps.get(x["baseAsset"])
        if x["status"] != "TRADING" or x["quoteAsset"] != "USDT" or base in SKIP_BASES or not c:
            continue
        if crypto.MIN_MCAP <= c["market_cap"] <= crypto.MAX_MCAP and not crypto.SKIP_NAME.search(c["name"]):
            out.append({"symbol": x["symbol"], "base": base, **c})
    return sorted(out, key=lambda u: u["symbol"])


def detect(hours: list[tuple], coin: dict, until: int) -> list[dict]:
    """Replay the radar on hourly candles (ts, o, h, l, c, quote_volume); one signal per coin per 72 h."""
    out, last = [], -10**9
    closes = [h[4] for h in hours]
    for i in range(24, len(hours)):
        detected = hours[i][0] + 3600            # the radar would see it when the hour closes
        if detected >= until:
            break
        vol24 = sum(h[5] for h in hours[i - 23:i + 1])
        if vol24 < crypto.MIN_VOLUME or i - last < crypto.DEDUPE_HOURS:
            continue
        ch1, ch24 = closes[i] / closes[i - 1] - 1, closes[i] / closes[i - 24] - 1
        ch7 = closes[i] / closes[i - 168] - 1 if i >= 168 else None
        trig = (["pump24"] if ch24 >= 0.25 else []) + (["dump24"] if ch24 <= -0.25 else []) + \
               (["move1h"] if abs(ch1) >= 0.10 else [])
        if coin["market_cap"] and 0.6 <= vol24 / coin["market_cap"] <= crypto.MAX_SANE_VOL_RATIO:
            trig.append("volume")
        if not trig:
            continue
        last = i
        out.append({"detected": detected, "price": closes[i], "triggers": trig, "metrics": {
            "market_cap": coin["market_cap"], "market_cap_rank": coin["rank"], "total_volume": vol24,
            "fully_diluted_valuation": None, "ath_change_percentage": None,
            "price_change_percentage_1h_in_currency": ch1 * 100,
            "price_change_percentage_24h_in_currency": ch24 * 100,
            "price_change_percentage_7d_in_currency": ch7 * 100 if ch7 is not None else None}})
    return out


def run(db_path, cg_factory=crypto.CoinGeckoClient, client: candles.CandleClient | None = None,
        now: datetime | None = None, budget_s: float = CHUNK_SECONDS) -> dict:
    """Process coins until the time budget runs out. Returns progress stats; safe to call every run."""
    t0 = time.monotonic()
    client = client or candles.CandleClient()
    store, cstore = candles.CandleStore(db_path), crypto.CryptoStore(db_path)
    with sqlite3.connect(db_path) as c:
        _ensure(c)
        meta = dict(c.execute("SELECT key, value FROM backfill_meta"))
        done = {r[0] for r in c.execute("SELECT symbol FROM backfill_progress")}
    if meta.get("finished"):
        added = record_journal(db_path, now, only_missing=True)   # new or edited strategies get history too
        return {"finished": True, "signals": int(meta.get("signals", 0)), "coins": len(done), "journaled": added}

    live_events = [e for e in cstore.events() if e.get("origin", "live") == "live"]
    until = int(crypto.parse_iso(min(e["detected_at"] for e in live_events)).timestamp()) if live_events \
        else int((now or crypto.utcnow()).timestamp())            # history stops where live data begins
    if "universe" in meta:
        coins = json.loads(meta["universe"])
    else:
        coins = universe(cg_factory(), client.session)
        with sqlite3.connect(db_path) as c:
            c.execute("INSERT OR REPLACE INTO backfill_meta VALUES ('universe', ?)", (json.dumps(coins),))

    start = until // 3600 * 3600 - (DAYS + 1) * 86400
    new_signals = 0
    for coin in coins:
        if coin["symbol"] in done:
            continue
        if time.monotonic() - t0 > budget_s:
            break
        hours = client.binance(coin["symbol"], candles.RES_HOUR, start, until + candles.HOURLY_DAYS * 86400)
        found = detect(hours, coin, until)
        for sig in found:
            detected_at = datetime.fromtimestamp(sig["detected"], timezone.utc)
            eid = cstore.add_event({"id": coin["id"], "symbol": coin["base"], "name": coin["name"],
                                    "image": coin["image"], "current_price": sig["price"], **sig["metrics"]},
                                   sig["triggers"], detected_at, origin="backfill")
            if eid is None:            # already stored by an interrupted earlier run
                continue
            win = candles.window(crypto.iso(detected_at))
            m0, m1 = win[candles.RES_MIN]
            store.save(eid, candles.RES_MIN, [r for r in client.binance(coin["symbol"], candles.RES_MIN, m0, m1)
                                              if m0 <= r[0] < m1])
            h0, h1 = win[candles.RES_HOUR]
            store.save(eid, candles.RES_HOUR, [r for r in hours if h0 <= r[0] < h1])
            store.set_source(eid, "binance", coin["symbol"])
            new_signals += 1
        with sqlite3.connect(db_path) as c:
            c.execute("INSERT OR REPLACE INTO backfill_progress VALUES (?, ?, ?)",
                      (coin["symbol"], len(found), crypto.iso(crypto.utcnow())))
        done.add(coin["symbol"])

    finished = len(done) >= len(coins)
    if finished:
        record_journal(db_path, now)
        with sqlite3.connect(db_path) as c:
            total = c.execute("SELECT COUNT(*) FROM crypto_events WHERE origin = 'backfill'").fetchone()[0]
            c.execute("INSERT OR REPLACE INTO backfill_meta VALUES ('finished', ?)", (crypto.iso(crypto.utcnow()),))
            c.execute("INSERT OR REPLACE INTO backfill_meta VALUES ('signals', ?)", (str(total),))
    return {"finished": finished, "coins": len(done), "of": len(coins), "new_signals": new_signals}


def record_journal(db_path, now: datetime | None = None, only_missing: bool = False) -> int:
    """Simulate every strategy on the historic signals; their windows are complete, so trades are final.
    only_missing: just the strategy versions that have no history trades yet."""
    import app as webapp  # local import: app imports half the project
    now = now or crypto.utcnow()
    cstore, store = crypto.CryptoStore(db_path), candles.CandleStore(db_path)
    cdata = {"events": cstore.events(), "prices": {}, "coins": cstore.coins(), "weights": cstore.weights(),
             "xtb_crypto": set(), "news": {}, "venues": store.sources()}
    rows, _ = webapp.build_crypto_rows(cdata, {"history": "1"})
    rows = [r for r in rows if r.get("origin") == "backfill"]
    journal = strategy.Journal(db_path)
    catalog = [(k, p) for k, _, _, p in strategy.PRESETS] + [(f"c{s['id']}", s["params"]) for s in cstore.strategies()]
    if only_missing:
        ids = {r["id"] for r in rows}
        have = {(j["strategy"], j["version"]) for j in journal.rows() if j["event_id"] in ids}
        catalog = [(k, p) for k, p in catalog if (k, strategy.version(p)) not in have]
    matched = {k: [r for r in rows if strategy.matches(r, p)] for k, p in catalog}
    needed = sorted({r["id"] for rs in matched.values() for r in rs})
    if not needed:
        return 0
    paths = store.paths(needed)   # minute candles are heavy: load only what the strategies use
    return sum(journal.record(key, params, strategy.backtest(matched[key], {}, params, now, paths=paths)["trades"],
                              lambda t: True) for key, params in catalog)

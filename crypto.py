"""Crypto radar: big moves on smaller tokens (CoinGecko, free, no key needed).

Each run pulls the top N coins by market cap, keeps the small/mid caps, and records
an *event* when a token moves hard (24h/1h), trades abnormal volume or trends on
CoinGecko. The price at detection is the baseline; snapshots every run (plus
CoinGecko hourly history to fill gaps while the app was off) measure what happened next.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import calibration
import signals
import safety

CG_URL = "https://api.coingecko.com/api/v3"
PAGES = int(os.environ.get("CRYPTO_PAGES", "4"))                 # 250 coins per page
MIN_MCAP = float(os.environ.get("CRYPTO_MIN_MCAP", "3e6"))
MAX_MCAP = float(os.environ.get("CRYPTO_MAX_MCAP", "1.5e9"))      # "smaller tokens"
MIN_VOLUME = 200_000
TRACK_DAYS = 30
DETAILS_PER_RUN = int(os.environ.get("CRYPTO_DETAILS_PER_RUN", "8"))  # project info calls per scan
DEDUPE_HOURS = 72                                                  # one event per coin per 3 days
BENCHMARK = "bitcoin"
HORIZONS = [(1, "1 h"), (24, "24 h"), (72, "3 dni"), (168, "7 dni")]
SKIP_NAME = re.compile(r"\b(wrapped|bridged|staked|liquid staking|restaked|usd|eur|tokenized)\b", re.I)
SKIP_ID = re.compile(r"tokenized|xstock|-stock\b", re.I)  # tokenized equities (Robinhood, xStocks) are not crypto


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


LOCAL_TZ = ZoneInfo(os.environ.get("APP_TZ", "Europe/Warsaw"))  # CEST in summer, CET in winter


def local(ts: str, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Stored UTC timestamp -> local wall-clock time for display."""
    return parse_iso(ts).astimezone(LOCAL_TZ).strftime(fmt)


def max_gap(points: list[tuple[str, float]], start: str, end: datetime) -> timedelta:
    """Longest stretch without a price between `start` and `end` (strategy exits need a dense path)."""
    times = [parse_iso(start)] + [parse_iso(ts) for ts, _ in points if ts > start and parse_iso(ts) <= end] + [end]
    return max((b - a for a, b in zip(times, times[1:])), default=timedelta(0))


# ---------- HTTP ----------

class CoinGeckoClient:
    def __init__(self, api_key: str | None = None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        api_key = api_key or os.environ.get("COINGECKO_API_KEY")
        if api_key:
            self.session.headers["x-cg-demo-api-key"] = api_key
        self.delay = 2.2 if api_key else 6.5  # public API allows only a few calls per minute
        self._last = 0.0

    def _get(self, path: str, **params):
        for attempt in range(2):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            resp = self.session.get(CG_URL + path, params=params, timeout=30)
            self._last = time.monotonic()
            if resp.status_code == 429 and attempt == 0:
                time.sleep(65)
                continue
            resp.raise_for_status()
            return resp.json()

    def markets(self, page: int) -> list[dict]:
        return self._get("/coins/markets", vs_currency="usd", order="market_cap_desc", per_page=250,
                         page=page, price_change_percentage="1h,24h,7d")

    def trending(self) -> set[str]:
        return {c["item"]["id"] for c in self._get("/search/trending").get("coins", [])}

    def coin(self, coin_id: str) -> dict:
        d = self._get(f"/coins/{coin_id}", localization="false", tickers="false", market_data="false",
                      community_data="false", developer_data="false")
        links = d.get("links", {})
        desc = re.sub(r"<[^>]+>", "", html.unescape((d.get("description") or {}).get("en", ""))).strip()
        return {
            "description": desc,
            "categories": [c for c in d.get("categories", []) if c],
            "homepage": next((u for u in links.get("homepage", []) if u), ""),
            "twitter": links.get("twitter_screen_name") or "",
            "github": [u for u in (links.get("repos_url") or {}).get("github", []) if u][:3],
            "genesis_date": d.get("genesis_date") or "",
            "sentiment_up": d.get("sentiment_votes_up_percentage"),
            "watchlist": d.get("watchlist_portfolio_users"),
            "platforms": [p for p in (d.get("platforms") or {}) if p][:4],
        }

    def chart(self, coin_id: str, days: int) -> list[tuple[str, float]]:
        d = self._get(f"/coins/{coin_id}/market_chart", vs_currency="usd", days=days)
        return [(iso(datetime.fromtimestamp(ms / 1000, timezone.utc)), p) for ms, p in d.get("prices", [])]

    def simple_price(self, ids: list[str]) -> dict[str, float]:
        if not ids:
            return {}
        d = self._get("/simple/price", ids=",".join(ids), vs_currencies="usd")
        return {k: v["usd"] for k, v in d.items() if "usd" in v}


# ---------- Storage ----------

class CryptoStore:
    def __init__(self, path):
        self.path = str(path)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS crypto_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT NOT NULL, symbol TEXT, name TEXT,
                image TEXT, detected_at TEXT NOT NULL, base_price REAL NOT NULL,
                triggers TEXT NOT NULL, metrics TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS crypto_prices (
                coin_id TEXT NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY (coin_id, ts))""")
            c.execute("""CREATE TABLE IF NOT EXISTS crypto_coins (
                coin_id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS crypto_strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, params TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            calibration.ensure_table(c)

    def _conn(self):
        return sqlite3.connect(self.path)

    def add_event(self, coin: dict, triggers: list[str], at: datetime) -> None:
        metrics = {k: coin.get(k) for k in (
            "market_cap", "market_cap_rank", "fully_diluted_valuation", "total_volume",
            "circulating_supply", "total_supply", "max_supply", "ath", "ath_change_percentage",
            "price_change_percentage_1h_in_currency", "price_change_percentage_24h_in_currency",
            "price_change_percentage_7d_in_currency")}
        with self._conn() as c:
            c.execute("INSERT INTO crypto_events (coin_id, symbol, name, image, detected_at, base_price, "
                      "triggers, metrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (coin["id"], coin["symbol"].upper(), coin["name"], coin.get("image", ""), iso(at),
                       coin["current_price"], json.dumps(triggers), json.dumps(metrics)))

    def events(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, coin_id, symbol, name, image, detected_at, base_price, triggers, "
                             "metrics FROM crypto_events ORDER BY detected_at DESC").fetchall()
        return [{"id": r[0], "coin_id": r[1], "symbol": r[2], "name": r[3], "image": r[4],
                 "detected_at": r[5], "base_price": r[6], "triggers": json.loads(r[7]),
                 "metrics": json.loads(r[8])} for r in rows]

    def save_prices(self, coin_id: str, points: list[tuple[str, float]]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO crypto_prices VALUES (?, ?, ?)",
                          [(coin_id, ts, p) for ts, p in points if p])

    def prices(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list] = {}
        with self._conn() as c:
            for cid, ts, p in c.execute("SELECT coin_id, ts, price FROM crypto_prices ORDER BY coin_id, ts"):
                out.setdefault(cid, []).append((ts, p))
        return out

    def save_coin(self, coin_id: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO crypto_coins VALUES (?, ?, ?)",
                      (coin_id, json.dumps(data), iso(utcnow())))

    def coins(self) -> dict[str, dict]:
        with self._conn() as c:
            return {k: json.loads(d) for k, d in c.execute("SELECT coin_id, data FROM crypto_coins")}

    def strategies(self) -> list[dict]:
        with self._conn() as c:
            return [{"id": i, "name": n, "params": json.loads(p), "created_at": t}
                    for i, n, p, t in c.execute("SELECT id, name, params, created_at FROM crypto_strategies ORDER BY id")]

    def save_strategy(self, name: str, params: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO crypto_strategies (name, params, created_at) VALUES (?, ?, ?)",
                      (name, json.dumps(params), iso(utcnow())))

    def delete_strategy(self, strategy_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM crypto_strategies WHERE id = ?", (strategy_id,))

    def weights(self) -> dict:
        with self._conn() as c:
            return calibration.load_weights(c, "crypto")

    def set_weights(self, weights: dict | None) -> None:
        with self._conn() as c:
            calibration.save_weights(c, "crypto", weights)


# ---------- Detection ----------

def _pct(coin: dict, key: str) -> float:
    return (coin.get(key) or 0) / 100


def triggers_for(coin: dict, trending: set[str]) -> list[str]:
    mcap, vol = coin.get("market_cap") or 0, coin.get("total_volume") or 0
    price = coin.get("current_price") or 0
    if not (MIN_MCAP <= mcap <= MAX_MCAP) or vol < MIN_VOLUME or not price:
        return []
    if SKIP_NAME.search(coin.get("name", "")) or SKIP_ID.search(coin["id"]) or (0.97 <= price <= 1.03 and
                                                  abs(_pct(coin, "price_change_percentage_24h_in_currency")) < .02):
        return []  # stablecoins and wrapped/staked copies of other assets
    ch1 = _pct(coin, "price_change_percentage_1h_in_currency")
    ch24 = _pct(coin, "price_change_percentage_24h_in_currency")
    out = []
    if ch24 >= 0.25:
        out.append("pump24")
    if ch24 <= -0.25:
        out.append("dump24")
    if abs(ch1) >= 0.10:
        out.append("move1h")
    if vol / mcap >= 0.6:
        out.append("volume")
    if coin["id"] in trending:
        out.append("trending")
    return out


TRIGGER_LABELS = {"pump24": "Wybicie 24h", "dump24": "Zrzut 24h", "move1h": "Ruch 1h",
                  "volume": "Nietypowy wolumen", "trending": "Trending"}


# ---------- Scoring ----------

CRYPTO_FACTORS = {
    "vol_spike": (12, "Wolumen ≥ 60% kapitalizacji"),
    "vol_extreme": (-8, "Wolumen > 200% kap. (możliwy wash trading)"),
    "pump24": (10, "Wybicie +25–100% w 24h"),
    "parabolic": (-10, "Parabola > +100% w 24h"),
    "dump24": (-5, "Spadek ≥ 25% w 24h"),
    "trend7d": (8, "Trend 7d w górę"),
    "trending": (10, "Trending na CoinGecko"),
    "near_ath": (6, "Blisko ATH (price discovery)"),
    "far_from_ath": (3, "≥ 85% poniżej ATH"),
    "low_float": (-10, "FDV ≥ 3× kapitalizacji (odblokowania)"),
    "full_float": (5, "Prawie cała podaż w obiegu"),
    "micro_cap": (0, "Mikro-cap < $20 mln"),
    "low_liquidity": (-8, "Wolumen < $500k"),
    "has_github": (5, "Ma publiczne repozytorium"),
    "community": (5, "Duża społeczność (≥ 50k obserwujących)"),
    "sentiment": (3, "Sentyment ≥ 80% pozytywny"),
    "meme": (-5, "Memecoin"),
    "narrative": (4, "Modna narracja (AI / RWA / DePIN)"),
    "young": (0, "Projekt młodszy niż rok"),
}
BASE_SCORE = 40
NARRATIVES = re.compile(r"artificial intelligence|\bai\b|real world assets|rwa|depin", re.I)

KINDS = {
    "pump": ("Pompa z wolumenem", "Cena i wolumen rosną naraz. Ktoś agresywnie kupuje: to może być news, "
                                  "listing albo skoordynowana pompa. Kluczowe jest to, czy wolumen się utrzyma."),
    "breakout": ("Wybicie", "Mocny wzrost ceny bez skrajnego wolumenu. Warto sprawdzić, co jest katalizatorem."),
    "dump": ("Zrzut", "Gwałtowny spadek. Może to być okazja do odbicia albo początek dłuższego spadku "
                      "(unlock, hack, wyjście dużego posiadacza)."),
    "volume": ("Nietypowy wolumen", "Obrót jest nieproporcjonalny do kapitalizacji, a cena jeszcze się nie ruszyła. "
                                    "Czasem to akumulacja przed ruchem."),
    "trending": ("Trending", "Token jest popularny na CoinGecko, więc rośnie zainteresowanie detalu."),
    "move": ("Szybki ruch 1h", "Duży ruch w ciągu godziny, często na newsie albo przy niskiej płynności."),
}


def kind_of(triggers: list[str]) -> str:
    if "pump24" in triggers:
        return "pump" if "volume" in triggers else "breakout"
    if "dump24" in triggers:
        return "dump"
    if "volume" in triggers:
        return "volume"
    if "trending" in triggers:
        return "trending"
    return "move"


def analyze(event: dict, details: dict | None, weights: dict | None = None) -> dict:
    w = {**{k: v[0] for k, v in CRYPTO_FACTORS.items()}, **(weights or {})}
    m = event["metrics"]
    mcap, vol = m.get("market_cap") or 0, m.get("total_volume") or 0
    ch24 = (m.get("price_change_percentage_24h_in_currency") or 0) / 100
    ch7 = (m.get("price_change_percentage_7d_in_currency") or 0) / 100
    ath = (m.get("ath_change_percentage") or 0) / 100
    fdv = m.get("fully_diluted_valuation")
    reasons, factors, score = [], [], BASE_SCORE

    def add(key: str, why: str) -> None:
        nonlocal score
        pts = round(w[key])
        score += pts
        factors.append(key)
        reasons.append(("+" if pts > 0 else "-" if pts < 0 else "·", why))

    ratio = vol / mcap if mcap else 0
    if ratio > 2:
        add("vol_extreme", f"Wolumen to {ratio:.1f}× kapitalizacji. To skrajnie dużo i może być sztucznie nakręcone.")
    elif ratio >= 0.6:
        add("vol_spike", f"Wolumen to {ratio:.0%} kapitalizacji, więc zainteresowanie gwałtownie rośnie.")
    if ch24 >= 1:
        add("parabolic", f"Kurs urósł o {ch24:+.0%} w 24h. Po takich parabolach często przychodzi ostra korekta.")
    elif ch24 >= 0.25:
        add("pump24", f"Wybicie o {ch24:+.0%} w 24h.")
    elif ch24 <= -0.25:
        add("dump24", f"Spadek o {ch24:.0%} w 24h.")
    if ch7 >= 0.20 and ch24 > 0:
        add("trend7d", f"Trend tygodniowy też w górę ({ch7:+.0%} w 7 dni).")
    if "trending" in event["triggers"]:
        add("trending", "Jest na liście trending CoinGecko.")
    if ath >= -0.10:
        add("near_ath", f"Kurs jest blisko ATH ({ath:.0%}). Nad nim nie ma sprzedających z górki.")
    elif ath <= -0.85:
        add("far_from_ath", f"Kurs jest {abs(ath):.0%} poniżej ATH. Jest duże pole do odbicia, ale projekt bywa już martwy.")
    if fdv and mcap:
        if fdv / mcap >= 3:
            add("low_float", f"FDV jest {fdv / mcap:.1f}× większe od kapitalizacji. "
                             "Przyszłe odblokowania tokenów będą ciążyć cenie.")
        elif fdv / mcap <= 1.2:
            add("full_float", "Prawie cała podaż jest już w obiegu, więc ryzyko odblokowań jest małe.")
    if mcap < 20e6:
        add("micro_cap", f"Mikro-cap (${mcap / 1e6:.1f} mln): możliwe wielokrotne wzrosty, ale i łatwa manipulacja.")
    if vol < 500_000:
        add("low_liquidity", f"Niski obrót (${vol / 1e3:,.0f}k), więc trudno wejść i wyjść bez poślizgu.")

    if details:
        cats = " ".join(details.get("categories", []))
        if details.get("github"):
            add("has_github", "Projekt ma publiczne repozytorium kodu, czyli coś jest budowane.")
        if (details.get("watchlist") or 0) >= 50_000:
            add("community", f"Obserwuje go {details['watchlist']:,} osób na CoinGecko.")
        if (details.get("sentiment_up") or 0) >= 80:
            add("sentiment", f"{details['sentiment_up']:.0f}% głosów społeczności jest pozytywnych.")
        if re.search(r"\bmeme", cats, re.I):
            add("meme", "Memecoin: cena zależy od hype'u, a nie od produktu.")
        if NARRATIVES.search(cats):
            add("narrative", "Pasuje do modnej narracji (AI / RWA / DePIN).")
        g = details.get("genesis_date")
        if g and g >= (utcnow() - timedelta(days=365)).date().isoformat():
            add("young", f"Młody projekt (start {g}): krótka historia i większa niepewność.")

    score = max(0, min(100, score))
    level, label = signals.level_of(score)
    kind = kind_of(event["triggers"])
    return {"score": score, "level": level, "label": label, "factors": factors, "reasons": reasons,
            "kind": kind, "kind_label": KINDS[kind][0], "kind_text": KINDS[kind][1]}


# ---------- Performance ----------

def price_at(points: list[tuple[str, float]], target: datetime, tolerance: timedelta) -> float | None:
    """First snapshot at/after target, if it is close enough to count."""
    t = iso(target)
    for ts, p in points:
        if ts >= t:
            return p if parse_iso(ts) - target <= tolerance else None
    return None


def performance(event: dict, points: list[tuple[str, float]], bench: list[tuple[str, float]] | None,
                now: datetime | None = None) -> dict:
    now = now or utcnow()
    base_t, base = parse_iso(event["detected_at"]), event["base_price"]
    after = [(ts, p) for ts, p in points if ts >= event["detected_at"]]
    out = {"now": None, "alpha_now": None, "h": {}, "path": [(event["detected_at"], 0.0)], "missing": []}
    if not base:
        return out
    out["path"] += [(ts, p / base - 1) for ts, p in after if ts > event["detected_at"]]
    if after:
        out["now"] = after[-1][1] / base - 1
    b0 = price_at(bench or [], base_t, timedelta(hours=2)) if bench else None
    if b0 and bench and after:
        b_now = price_at(bench, parse_iso(after[-1][0]), timedelta(hours=2)) or bench[-1][1]
        out["alpha_now"] = out["now"] - (b_now / b0 - 1)
    for hours, _ in HORIZONS:
        target = base_t + timedelta(hours=hours)
        if target > now:
            continue
        tol = timedelta(hours=max(1, hours * 0.25))
        p = price_at(after, target, tol)
        if p is None:
            out["missing"].append(hours)
            continue
        r = p / base - 1
        bp = price_at(bench or [], target, tol) if b0 else None
        out["h"][hours] = {"ret": r, "alpha": (r - (bp / b0 - 1)) if bp else None}
    return out


def summarize(perfs: list[dict]) -> list[dict]:
    rows = []
    for key, label in [("now", "Od wykrycia"), *HORIZONS]:
        if key == "now":
            rets = [p["now"] for p in perfs if p["now"] is not None]
            alphas = [p["alpha_now"] for p in perfs if p["alpha_now"] is not None]
        else:
            hs = [p["h"][key] for p in perfs if key in p["h"]]
            rets, alphas = [h["ret"] for h in hs], [h["alpha"] for h in hs if h["alpha"] is not None]
        wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
        avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        rows.append({"label": label, "key": key, "n": len(rets), "avg": avg(rets),
                     "median": sorted(rets)[len(rets) // 2] if rets else None,
                     "win_rate": len(wins) / len(rets) if rets else None,
                     "avg_win": avg(wins), "avg_loss": avg(losses), "alpha": avg(alphas)})
    return rows


def average_path(perfs: list[dict], hours: int = 168, step: int = 6) -> list[tuple[int, float | None]]:
    """Mean return at every `step` hours since detection, sampled from each event's path."""
    out = []
    for h in range(0, hours + 1, step):
        vals = []
        for p in perfs:
            t0 = parse_iso(p["path"][0][0])
            target = iso(t0 + timedelta(hours=h))
            hit = next((v for ts, v in p["path"] if ts >= target), None)
            last_ts = p["path"][-1][0]
            if hit is not None and last_ts >= target:
                vals.append(hit)
        out.append((h, sum(vals) / len(vals) if vals else None))
    while out and out[-1][1] is None:
        out.pop()
    return out


# ---------- Background runner ----------

class CryptoScanner:
    def __init__(self, store: CryptoStore, client_factory, pages: int = PAGES):
        self.store = store
        self.client_factory = client_factory
        self.pages = pages
        self.lock = threading.Lock()
        self.running = False
        self.last_run: str | None = None
        self.last_new = 0
        self.last_error: str | None = None
        self.backfilled: dict[str, datetime] = {}  # coin -> last history fetch, so gaps cost one call

    def run_once(self, now: datetime | None = None) -> None:
        if not self.lock.acquire(blocking=False):
            return
        self.running = True
        try:
            self._run(self.client_factory(), now or utcnow())
            self.last_error = None
        except Exception as exc:
            self.last_error = safety.safe_error(exc)
        finally:
            self.last_run = datetime.now().strftime("%H:%M:%S")
            self.running = False
            self.lock.release()

    def _run(self, client, now: datetime) -> None:
        coins = []
        for page in range(1, self.pages + 1):
            coins.extend(client.markets(page))
        try:
            trending = client.trending()
        except Exception:
            trending = set()

        events = self.store.events()
        recent = {e["coin_id"] for e in events
                  if parse_iso(e["detected_at"]) >= now - timedelta(hours=DEDUPE_HOURS)}
        new = 0
        for coin in coins:
            trig = triggers_for(coin, trending)
            if trig and coin["id"] not in recent:
                self.store.add_event(coin, trig, now)
                new += 1
        self.last_new = new
        events = self.store.events()

        # Snapshot prices of everything still tracked (+ BTC as the benchmark).
        stamp = iso(now)
        tracked = {e["coin_id"] for e in events
                   if parse_iso(e["detected_at"]) >= now - timedelta(days=TRACK_DAYS)} | {BENCHMARK}
        by_id = {c["id"]: c for c in coins}
        for cid in tracked & by_id.keys():
            self.store.save_prices(cid, [(stamp, by_id[cid]["current_price"])])
        missing = sorted(tracked - by_id.keys())
        for i in range(0, len(missing), 200):
            for cid, p in client.simple_price(missing[i:i + 200]).items():
                self.store.save_prices(cid, [(stamp, p)])

        # Project info for new tokens (rate limited, the rest comes next run).
        known = self.store.coins()
        for cid in [c for c in dict.fromkeys(e["coin_id"] for e in events) if c not in known][:DETAILS_PER_RUN]:
            try:
                self.store.save_coin(cid, client.coin(cid))
            except Exception:
                pass

        # Fill gaps (app was off) from CoinGecko hourly history, so horizons and
        # strategy simulations see a continuous path for the first week after detection.
        prices = self.store.prices()
        gaps = []
        for e in events:
            if e["coin_id"] not in tracked:
                continue
            end = min(now, parse_iso(e["detected_at"]) + timedelta(hours=HORIZONS[-1][0]))
            stale = self.backfilled.get(e["coin_id"], now - timedelta(days=1)) < now - timedelta(hours=6)
            if stale and max_gap(prices.get(e["coin_id"], []), e["detected_at"], end) > timedelta(minutes=90):
                gaps.append(e["coin_id"])
        for cid in list(dict.fromkeys(gaps))[:4] + ([BENCHMARK] if gaps else []):
            self.backfilled[cid] = now
            try:
                self.store.save_prices(cid, client.chart(cid, days=TRACK_DAYS))
            except Exception:
                pass

    def trigger(self) -> None:
        threading.Thread(target=self.run_once, daemon=True).start()

    def loop(self, interval_s: int) -> None:
        while True:
            self.run_once()
            time.sleep(interval_s)

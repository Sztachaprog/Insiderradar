"""One scan cycle + static export, for GitHub Actions (see .github/workflows/scan.yml).

    python ci.py                  # scan stocks, crypto (and whales every ~30 min), export site/
    python ci.py --only crypto    # one scanner
    python ci.py --no-scan        # just re-export site/ from form4.db

The static site is the regular Flask app rendered page by page into site/, with
links prefixed by the GitHub Pages path (/<repo>/).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app as webapp
import backfill
import candles
import crypto
import form4_scanner as fs
import market
import news
import notify
import strategy
import safety
import search
import whales
import xtb

SITE = Path("site")
EXPORT_FLAG = Path("export_due")   # written by the scan step when the static site is due; the workflow checks it
# CoinGecko Demo: 10,000 credits a month. The full scan runs as often as the rest of the month's budget
# allows (never more often than CG_MIN_EVERY_MIN); the margin covers local runs and retries.
CG_MONTHLY_BUDGET = int(os.environ.get("CG_MONTHLY_BUDGET", "9000"))
CG_MIN_EVERY_MIN = int(os.environ.get("CG_MIN_EVERY_MIN", "30"))
CG_MAX_EVERY_MIN = 180
CG_CALLS_PER_SCAN_GUESS = 10
# How often each task runs (minutes). The workflow is triggered every ~5 min; each task remembers its
# last run in the database, so irregular or extra triggers never make anything run too often.
EVERY = {
    "coingecko_full": CG_MIN_EVERY_MIN,   # ranking, DEX-only coins, project info: Schedule.coingecko_every
    "stocks": int(os.environ.get("EVERY_STOCKS_MIN", "14")),              # SEC filings arrive with a delay anyway
    "whales": int(os.environ.get("EVERY_WHALES_MIN", "55")),
    "export": int(os.environ.get("EVERY_EXPORT_MIN", "9")),
    "search": int(os.environ.get("EVERY_SEARCH_MIN", "355")),    # strategy search: new signals arrive slowly
}
STATUS = Path(os.environ.get("SCAN_STATUS", "scan_status.json"))  # scanner status, shown by the export step
WHALES_STATUS = Path("whales_status.json")   # written by the whales workflow (SCAN_STATUS=whales_status.json)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def timed(name: str, fn) -> None:
    t0 = time.monotonic()
    fn()
    log(f"{name}: {time.monotonic() - t0:.0f} s")


def _next_month(start: datetime) -> datetime:
    return (start + timedelta(days=32)).replace(day=1)


class Schedule:
    """Last run of each task, stored in the database (survives between workflow runs)."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS ci_schedule (task TEXT PRIMARY KEY, last_run TEXT NOT NULL)")

            c.execute("""CREATE TABLE IF NOT EXISTS ci_cg_usage (
                month TEXT PRIMARY KEY, calls INTEGER NOT NULL, scans INTEGER NOT NULL, seeded INTEGER NOT NULL)""")

    def due(self, task: str, now: datetime | None = None) -> bool:
        now = now or crypto.utcnow()
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT last_run FROM ci_schedule WHERE task = ?", (task,)).fetchone()
        every = self.coingecko_every(now) if task == "coingecko_full" else EVERY[task]
        return not row or crypto.parse_iso(row[0]) <= now - timedelta(minutes=every) + timedelta(seconds=30)

    def _usage(self, now: datetime) -> tuple[int, int, int]:
        """(credits used this month, full scans counted, credits assumed before counting started)."""
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT calls, scans, seeded FROM ci_cg_usage WHERE month = ?",
                            (now.strftime("%Y-%m"),)).fetchone()
        if row:
            return row
        # A month we started counting in the middle of: assume the budget was spent evenly so far.
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        seeded = round(CG_MONTHLY_BUDGET * (now - start) / (_next_month(start) - start))
        return seeded if start != now else 0, 0, seeded

    def coingecko_used(self, calls: int, now: datetime | None = None) -> None:
        now = now or crypto.utcnow()
        used, scans, seeded = self._usage(now)
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR REPLACE INTO ci_cg_usage VALUES (?, ?, ?, ?)",
                      (now.strftime("%Y-%m"), used + calls, scans + 1, seeded))

    def coingecko_every(self, now: datetime | None = None) -> float:
        """Minutes between full CoinGecko scans so the monthly budget lasts until the 1st."""
        now = now or crypto.utcnow()
        used, scans, seeded = self._usage(now)
        per_scan = (used - seeded) / scans if scans >= 5 else CG_CALLS_PER_SCAN_GUESS
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        minutes_left = (_next_month(start) - now) / timedelta(minutes=1)
        scans_left = max(CG_MONTHLY_BUDGET - used, 0) / max(per_scan, 1)
        return min(max(minutes_left / max(scans_left, 1), CG_MIN_EVERY_MIN), CG_MAX_EVERY_MIN)

    def done(self, task: str, now: datetime | None = None) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR REPLACE INTO ci_schedule VALUES (?, ?)", (task, crypto.iso(now or crypto.utcnow())))


RETIRED_TABLES = ("ict_bars",)   # BTC 5M candles of the removed ICT session model (~10 MB)


def drop_retired_tables(path) -> None:
    """Data of removed features: drop it once and give the space back."""
    with sqlite3.connect(path) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        gone = [t for t in RETIRED_TABLES if t in have]
        for t in gone:
            c.execute(f"DROP TABLE {t}")
    if gone:
        with sqlite3.connect(path) as c:
            c.execute("VACUUM")
        log(f"usunięte stare tabele: {', '.join(gone)}")


def scan(only: set[str], force_whales: bool, force_all: bool = False):
    store = webapp.Store(webapp.DB_PATH)
    cstore = crypto.CryptoStore(webapp.DB_PATH)
    wstore = whales.WhaleStore(webapp.WHALES_DB_PATH or webapp.DB_PATH)
    ua = os.environ.get("SEC_USER_AGENT")
    tracker = webapp.Tracker(store, market.YahooClient, sec_factory=(lambda: fs.SecClient(ua)) if ua else None,
                             xtb_factory=xtb.XtbClient)
    scanner = webapp.Scanner(store, lambda: fs.SecClient(ua), webapp.SCAN_PAGES, after_scan=tracker.refresh)
    cscanner = crypto.CryptoScanner(cstore, crypto.CoinGeckoClient, news_factory=news.NewsClient)
    wscanner = whales.WhaleScanner(wstore, whales.client_for)

    schedule = Schedule(webapp.DB_PATH)
    drop_retired_tables(webapp.DB_PATH)
    due = {t: force_all or schedule.due(t) for t in EVERY}
    if "stocks" in only and due["stocks"]:
        if ua:
            timed("insiderzy + ceny", scanner.run_once)
            log(f"  nowe zgłoszenia: {scanner.last_new}, błąd: {scanner.last_error}, ceny: {tracker.last_error}")
            schedule.done("stocks")
        else:
            log("insiderzy: pominięte, brak sekretu SEC_USER_AGENT")
    elif "stocks" in only:
        log("insiderzy: jeszcze nie pora")
    candle_status, history_status = Status(), Status()
    if "crypto" in only:
        # Full CoinGecko scan about hourly (it also refreshes the ticker->coin map); Binance in between.
        full = due["coingecko_full"] or not cstore.universe()
        timed("krypto (CoinGecko, pełny)" if full else "krypto (Binance, szybki)",
              lambda: cscanner.run_once(mode="full" if full else "quick"))
        log(f"  nowe ruchy: {cscanner.last_new}, błąd: {cscanner.last_error}")
        if full:
            schedule.coingecko_used(cscanner.last_calls)
            log(f"  CoinGecko: {cscanner.last_calls} zapytań, następny pełny skan za "
                f"{schedule.coingecko_every():.0f} min")
        if full and (cscanner.last_calls or not cscanner.last_error):
            schedule.done("coingecko_full")   # a failed scan that spent credits waits its turn too
        timed("świece i dziennik strategii", lambda: candles_and_journal(store, cstore, candle_status))
        timed("historia 2 mies. z Binance", lambda: run_backfill(history_status))
        if due["search"]:
            timed("szukanie strategii", lambda: run_search(cstore))
            schedule.done("search")
    # Whale holders change slowly and the scan is the heaviest; every ~30 min is plenty.
    if "whales" in only and (force_whales or due["whales"]):
        timed("wieloryby", wscanner.run_once)
        log(f"  nowe portfele: {wscanner.last_new}, błąd: {wscanner.last_error}")
        schedule.done("whales")

    if only and due["export"]:   # the export step of the workflow only runs when this flag exists
        EXPORT_FLAG.write_text(crypto.iso(crypto.utcnow()))
        schedule.done("export")

    objs = {"stocks": scanner, "tracker": tracker, "crypto": cscanner, "whales": wscanner, "candles": candle_status,
            "history": history_status}
    fields = ("last_run", "last_new", "last_error")
    if only:
        old = read_status()
        STATUS.write_text(json.dumps({**old, **{k: {f: getattr(o, f, None) for f in fields}
                                                for k, o in objs.items() if getattr(o, "last_run", None)}}))
    else:
        for k, vals in {**read_status(), **read_status(WHALES_STATUS, keys={"whales"})}.items():
            if k in objs and isinstance(vals, dict):
                for f, v in vals.items():
                    setattr(objs[k], f, v)
    return store, cstore, wstore, scanner, tracker, cscanner, wscanner


def read_status(path: Path | None = None, keys: set[str] | None = None) -> dict:
    """Scanner status saved by the previous step; a missing, empty or broken file is just 'no status'."""
    try:
        data = json.loads((path or STATUS).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Statuses written before error scrubbing existed could contain URLs with keys.
    return {k: {f: safety.scrub(v) if isinstance(v, str) else v for f, v in vals.items()}
            for k, vals in data.items() if isinstance(vals, dict) and (keys is None or k in keys)}


def _cache_methods(obj, names: list[str]) -> None:
    """The export renders ~1000 pages from one unchanging database: read each table once."""
    for name in names:
        setattr(obj, name, functools.cache(getattr(obj, name)))


def export(parts, base_path: str) -> int:
    store, cstore, wstore, scanner, tracker, cscanner, wscanner = parts
    _cache_methods(store, ["all", "tracked", "prices", "companies", "histories", "weights"])
    _cache_methods(cstore, ["events", "prices", "coins", "weights", "strategies", "news"])
    _cache_methods(wstore, ["transfers", "whales", "tokens", "token_prices", "balances", "signals", "labels"])
    original_build = whales.build
    whales.build = functools.cache(lambda s, now=None, include_treasury=False: original_build(s, now, include_treasury))
    # The same signal list is rebuilt for hundreds of pages: compute it once per distinct filter.
    original_rows = webapp.build_crypto_rows
    rows_cache: dict = {}

    def cached_rows(cdata, args):
        key = tuple(sorted((k, v) for k, v in (args.items() if hasattr(args, "items") else [])))
        if key not in rows_cache:
            rows_cache[key] = original_rows(cdata, args)
        return rows_cache[key]
    webapp.build_crypto_rows = cached_rows
    try:
        flask_app = webapp.create_app(store=store, scanner=scanner, tracker=tracker, start_background=False,
                                      crypto_store=cstore, crypto_scanner=cscanner,
                                      whale_store=wstore, whale_scanner=wscanner, static=True)
        client = flask_app.test_client()
        base_url = "http://localhost" + base_path.rstrip("/")

        pages = ["/", "/wyniki", "/krypto", "/krypto/wyniki", "/krypto/strategie", "/wieloryby",
                 "/krypto/strategie/otwarte", "/krypto/strategie/historia", "/krypto/strategie/szukanie", "/portfel", "/krypto/otwarte", "/wieloryby/otwarte"]
        for code in "PS":
            rows, _ = webapp.build_rows(store.all(), {"code": code, "min": "0", "all_owners": "1", "plan": "1"},
                                        {"tracked": store.tracked(), "prices": store.prices(),
                                         "companies": store.companies()})
            pages += [f"/filing/{r['accession']}" for r in rows]
        cutoff = crypto.iso(crypto.utcnow() - timedelta(days=crypto.TRACK_DAYS))
        pages += [f"/krypto/{e['id']}" for e in cstore.events()
                  if e["detected_at"] >= cutoff and e.get("origin", "live") == "live"]
        # every journal position (current rule versions) and the signal page its token name links to
        versions = {k: strategy.version(p) for k, _, _, p in strategy.PRESETS if k not in strategy.REFERENCE_ONLY}
        versions.update({f"c{s['id']}": strategy.version(s["params"]) for s in cstore.strategies()})
        for r in strategy.Journal(webapp.DB_PATH).rows():
            if versions.get(r["strategy"]) == r["version"]:
                pages += [f"/krypto/pozycja/{r['strategy']}/{r['event_id']}", f"/krypto/{r['event_id']}"]
        data = whales.build(wstore, include_treasury=True)
        pages += [f"/wieloryby/token/{t['token']}" for t in data["tokens"]]
        pages += [f"/wieloryby/portfel/{w['address']}" for w in data["whales"]]

        if SITE.exists():
            shutil.rmtree(SITE)
        failed = 0
        for path in dict.fromkeys(pages):
            resp = client.get(path, base_url=base_url)
            if resp.status_code != 200:
                failed += 1
                log(f"  {path}: HTTP {resp.status_code}")
                continue
            out = SITE / path.strip("/") / "index.html" if path != "/" else SITE / "index.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.data)
        shutil.copytree(Path(flask_app.static_folder), SITE / "static")   # CSS/JS shared by every page
        (SITE / ".nojekyll").write_text("")
        (SITE / "version.json").write_text(json.dumps({"build": flask_app.config["BUILD_ID"]}))
        log(f"eksport: {len(pages) - failed} stron do {SITE}/")
        return failed
    finally:
        whales.build = original_build
        webapp.build_crypto_rows = original_rows


class Status:
    """Scanner-like status holder for steps that are not scanner objects."""
    last_run = last_new = last_error = None


JOURNAL_DAYS = 9   # signals younger than this can still get candles / change their trades


def candles_and_journal(store, cstore, status: Status, now=None) -> None:
    """1. Append new candles for live signals. 2. Re-simulate every strategy on the signals that are
    still open and write the trades to the journal (final trades are never rewritten)."""
    now = now or crypto.utcnow()
    kstore = candles.CandleStore(webapp.DB_PATH)
    try:
        latest = {cid: pts[-1][1] for cid, pts in cstore.prices().items() if pts}
        stats = candles.update(candles.CandleClient(), kstore, cstore.events(), now, latest)
        log(f"  świece: +{stats['candles']}, źródła binance/geckoterminal/brak: {stats.get('binance', 0)}/"
            f"{stats.get('geckoterminal', 0)}/{stats.get('none', 0)}, odłożone {stats['deferred']}, błędy {stats['errors']}")
        status.last_new, status.last_error = stats["candles"], (f"{stats['errors']} błędów pobierania"
                                                               if stats["errors"] else None)
    except Exception as exc:  # candles must never break the scan
        status.last_error = safety.safe_error(exc)
        log(f"  świece: błąd {status.last_error}")
    status.last_run = datetime.now().strftime("%H:%M:%S")

    cdata = {"events": cstore.events(), "prices": cstore.prices(), "coins": cstore.coins(),
             "weights": cstore.weights(), "xtb_crypto": set(), "news": cstore.news()}
    rows, _ = webapp.build_crypto_rows(cdata, {})
    recent = [r for r in rows if crypto.parse_iso(r["detected_at"]) >= now - timedelta(days=JOURNAL_DAYS)]
    paths = kstore.paths([r["id"] for r in recent])
    sources = kstore.sources()

    def is_final(t: dict) -> bool:
        if t["status"] == "open":
            return False
        if now - crypto.parse_iso(t["entry_at"]) > timedelta(days=JOURNAL_DAYS - 1):
            return True                                   # window over: whatever we have is the answer
        if t["status"] == "no_data":
            return False
        no_candles = sources.get(t["row"]["id"], {}).get("source") == "none"
        return t["source"] == "candles" or no_candles     # snapshot-based results wait for candles

    journal = strategy.Journal(webapp.DB_PATH)
    catalog = [(k, p) for k, _, _, p in strategy.PRESETS] + [(f"c{s['id']}", s["params"]) for s in cstore.strategies()]
    written = sum(journal.record(key, params, strategy.backtest(recent, cdata["prices"], params, now, paths=paths)["trades"],
                                 is_final) for key, params in catalog)
    log(f"  dziennik strategii: zapisano/zaktualizowano {written} transakcji")


def run_search(cstore) -> None:
    """Try rule combinations on every signal with candles; rules that also hold on newer signals
    are saved as "Auto: ..." strategies, so the scans track them live from now on."""
    try:
        cdata = {"events": cstore.events(), "prices": {}, "coins": cstore.coins(), "weights": cstore.weights(),
                 "xtb_crypto": set(), "news": {}}
        rows, _ = webapp.build_crypto_rows(cdata, {"history": "1"})
        paths = candles.CandleStore(webapp.DB_PATH).paths([r["id"] for r in rows])
        result = search.run(rows, paths, crypto.utcnow())
        search.SearchStore(webapp.DB_PATH).save(result)
        added = search.track_passing(result, cstore)
        log(f"  strategie: {result['tried']} kombinacji na {result['signals']} sygnałach, "
            f"przeszło test {result['passed']} z {result['train_positive']}, nowe śledzone: {added or 'brak'}")
    except Exception as exc:  # never break the scan
        log(f"  szukanie strategii: błąd {safety.safe_error(exc)}")


def run_backfill(status: Status) -> None:
    """One-time replay of 2 months of radar history, in ~5-minute chunks per scan until finished."""
    try:
        r = backfill.run(webapp.DB_PATH)
        status.last_new = r.get("new_signals", r.get("signals"))
        log(f"  historia: {'gotowa' if r['finished'] else 'w toku'}, tokeny {r['coins']}/{r.get('of', r['coins'])}, "
            f"nowe sygnały {r.get('new_signals', 0)}")
    except Exception as exc:  # never break the scan
        status.last_error = safety.safe_error(exc)
        log(f"  historia: błąd {status.last_error}")
    status.last_run = datetime.now().strftime("%H:%M:%S")


def site_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
    owner, name = repo.split("/", 1)
    return os.environ.get("SITE_URL") or f"https://{owner.lower()}.github.io/{name}"


# Presets that would alert on nearly everything (baseline) or are a deliberate loser (control).
NO_ALERT_STRATEGIES = {"all", "chase"}


def collect_alerts(parts, now=None) -> list[tuple[str, str]]:
    store, cstore, wstore = parts[0], parts[1], parts[2]
    now = now or crypto.utcnow()
    site = site_url()
    alerts: list[tuple[str, str]] = []

    # Insider buys added in the last 2 days (older ones were either sent or are stale news).
    md = {"tracked": store.tracked(), "prices": store.prices(), "companies": store.companies(),
          "histories": store.histories(), "weights": store.weights(), "xtb": store.xtb_links()}
    recent = (datetime.now() - timedelta(days=2)).isoformat()
    added = store.added()
    rows, _ = webapp.build_rows(store.all(), {"code": "P", "min": "100000"}, md)
    alerts += notify.stock_alerts([r for r in rows if added.get(r["accession"], "") >= recent], site)

    # Crypto moves from the last 2 hours: strong score, or an entry for a saved/preset strategy.
    cdata = {"events": cstore.events(), "prices": cstore.prices(), "coins": cstore.coins(), "weights": cstore.weights(),
             "xtb_crypto": store.xtb_catalog("crypto"), "news": cstore.news()}
    crows, _ = webapp.build_crypto_rows(cdata, {})
    fresh = [r for r in crows if crypto.parse_iso(r["detected_at"]) >= now - timedelta(hours=2)]
    catalog = [(name, params) for key, name, _, params in strategy.PRESETS if key not in NO_ALERT_STRATEGIES]
    catalog += [(s["name"], s["params"]) for s in cstore.strategies()]
    hits: dict[int, list[str]] = {}
    for name, params in catalog:
        for r in fresh:
            if strategy.matches(r, params):   # the direction matters: a short is "sell", not "buy"
                side = " (SHORT)" if params.get("side") == "short" and "short" not in name.lower() else ""
                hits.setdefault(r["id"], []).append(name + side)
    alerts += notify.crypto_alerts(fresh, site, hits)

    # Whale spot signals and big accumulation / distribution transfers.
    data = whales.build(wstore)
    cat = store.xtb_catalog("crypto")
    for t in data["tokens"]:
        t["xtb"] = xtb.crypto_url((t["symbol"] or "").lower(), t["name"] or "", cat)
    data["big"] = [b for b in data["big"] if b["ts"] >= crypto.iso(now - timedelta(days=2))]
    alerts += notify.whale_alerts(data, site)
    return alerts


def send_alerts(parts) -> None:
    if not notify.configured():
        log("telegram: pominięte, brak sekretów TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return
    try:
        alerts = collect_alerts(parts)
        result = notify.deliver(alerts, notify.Outbox(webapp.DB_PATH), notify.Telegram())
        log(f"telegram: wysłane {result['sent']}, pominięte {result['skipped']}, start {result['baseline']}")
    except Exception as exc:  # alerts must never break the scan or the data save
        log(f"telegram: błąd {safety.safe_error(exc)}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", default="stocks,crypto,whales")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--whales", action="store_true", help="scan whales regardless of the clock")
    p.add_argument("--all", action="store_true", help="run every task now, ignoring the schedule")
    p.add_argument("--import-whales", metavar="DB", help="copy whale tables from DB into WHALES_DB first")
    p.add_argument("--base", default=os.environ.get("PAGES_BASE")
                   or "/" + os.environ.get("GITHUB_REPOSITORY", "/").split("/")[-1])
    args = p.parse_args(argv)
    if args.import_whales:
        target = whales.WhaleStore(webapp.WHALES_DB_PATH or webapp.DB_PATH)
        log(f"wieloryby przeniesione z {args.import_whales}: {target.import_from(args.import_whales)}")
    parts = scan(set() if args.no_scan else set(args.only.split(",")), args.whales, args.all)
    if not args.no_scan and args.only != "whales":   # alerts go out from the main scan, which reads both databases
        send_alerts(parts)
    if args.no_export:
        return 0
    return 1 if export(parts, args.base) else 0


if __name__ == "__main__":
    sys.exit(main())

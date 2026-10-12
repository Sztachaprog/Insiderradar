"""Web UI for form4_scanner.

A background thread scans EDGAR every SCAN_INTERVAL_MIN minutes and stores every
parsed Form 4 in SQLite; the page filters the stored filings instantly.

    $env:SEC_USER_AGENT = "Imie Nazwisko mail@example.com"
    python app.py            # http://127.0.0.1:5002
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import calibration
import candles
import crypto
import form4_scanner as fs
import history
import market
import signals
import safety
import news
import notify
import strategy
import whales
import xtb

DB_PATH = Path(os.environ.get("FORM4_DB", Path(__file__).with_name("form4.db")))
# Whales can live in their own database (their own GitHub workflow); by default the same file.
WHALES_DB_PATH = Path(os.environ["WHALES_DB"]) if os.environ.get("WHALES_DB") else None
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "3"))
SCAN_INTERVAL_MIN = int(os.environ.get("SCAN_INTERVAL_MIN", "15"))
TRACK_DAYS = int(os.environ.get("TRACK_DAYS", "120"))  # keep refreshing prices this long after adding
BENCHMARK = "SPY"
HISTORY_DAYS = 7          # re-check an insider's track record weekly
XTB_PER_RUN = int(os.environ.get("XTB_PER_RUN", "20"))  # new tickers matched to XTB per scan
XTB_RECHECK_DAYS = 14     # tickers not found on XTB are retried (XTB keeps adding US stocks)
HISTORY_PER_RUN = int(os.environ.get("HISTORY_PER_RUN", "5"))  # insiders per scan (each up to ~40 SEC requests)
CRYPTO_INTERVAL_MIN = int(os.environ.get("CRYPTO_INTERVAL_MIN", "15"))
WHALE_INTERVAL_MIN = int(os.environ.get("WHALE_INTERVAL_MIN", "30"))


# ---------- Storage ----------

class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS filings (
                accession TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                scanned_at TEXT NOT NULL)""")
            # Price at the moment a filing entered the app, then daily closes to measure the move.
            c.execute("""CREATE TABLE IF NOT EXISTS tracked (
                accession TEXT PRIMARY KEY, ticker TEXT NOT NULL,
                base_date TEXT NOT NULL, base_price REAL NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS prices (
                ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
                PRIMARY KEY (ticker, date))""")
            c.execute("""CREATE TABLE IF NOT EXISTS companies (
                ticker TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS xtb_links (
                ticker TEXT PRIMARY KEY, stock TEXT, cfd TEXT, checked_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS xtb_catalog (
                kind TEXT NOT NULL, slug TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (kind, slug))""")
            c.execute("""CREATE TABLE IF NOT EXISTS insider_history (
                owner_cik TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
            calibration.ensure_table(c)

    def _conn(self):
        return sqlite3.connect(self.path)

    def accessions(self) -> set[str]:
        with self._conn() as c:
            return {r[0] for r in c.execute("SELECT accession FROM filings")}

    def add(self, filings: list[fs.Filing]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO filings VALUES (?, ?, ?)",
                          [(f.accession, json.dumps(asdict(f)), now) for f in filings])

    def all(self) -> list[fs.Filing]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM filings").fetchall()
        out = []
        for (data,) in rows:
            d = json.loads(data)
            d["transactions"] = [fs.Transaction(**t) for t in d["transactions"]]
            out.append(fs.Filing(**d))
        return out

    def replace(self, filings: list[fs.Filing]) -> None:
        with self._conn() as c:
            c.executemany("UPDATE filings SET data = ? WHERE accession = ?",
                          [(json.dumps(asdict(f)), f.accession) for f in filings])

    def added(self) -> dict[str, str]:
        with self._conn() as c:
            return dict(c.execute("SELECT accession, scanned_at FROM filings"))

    def tracked(self) -> dict[str, tuple[str, str, float]]:
        with self._conn() as c:
            return {a: (t, d, p) for a, t, d, p in c.execute("SELECT * FROM tracked")}

    def track(self, accession: str, ticker: str, base_date: str, base_price: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO tracked VALUES (?, ?, ?, ?)",
                      (accession, ticker, base_date, base_price))

    def save_prices(self, ticker: str, closes: list[tuple[str, float]]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)",
                          [(ticker, d, p) for d, p in closes])

    def prices(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = defaultdict(list)
        with self._conn() as c:
            for t, d, p in c.execute("SELECT ticker, date, close FROM prices ORDER BY ticker, date"):
                out[t].append((d, p))
        return dict(out)

    def save_company(self, ticker: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO companies VALUES (?, ?, ?)",
                      (ticker, json.dumps(data), datetime.now().isoformat(timespec="seconds")))

    def companies(self) -> dict[str, dict]:
        with self._conn() as c:
            return {t: json.loads(d) for t, d in c.execute("SELECT ticker, data FROM companies")}

    def save_history(self, owner_cik: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO insider_history VALUES (?, ?, ?)",
                      (owner_cik, json.dumps(data), datetime.now().isoformat(timespec="seconds")))

    def histories(self) -> dict[str, dict]:
        with self._conn() as c:
            return {k: {**json.loads(d), "fetched_at": t}
                    for k, d, t in c.execute("SELECT owner_cik, data, fetched_at FROM insider_history")}

    def weights(self) -> dict:
        with self._conn() as c:
            return calibration.load_weights(c, "stocks")

    def xtb_links(self) -> dict[str, dict]:
        with self._conn() as c:
            return {t: {"stock": s, "cfd": f, "checked_at": at}
                    for t, s, f, at in c.execute("SELECT ticker, stock, cfd, checked_at FROM xtb_links")}

    def save_xtb_link(self, ticker: str, links: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO xtb_links VALUES (?, ?, ?, ?)",
                      (ticker, links.get("stock"), links.get("cfd"), datetime.now().isoformat(timespec="seconds")))

    def xtb_catalog(self, kind: str) -> set[str]:
        with self._conn() as c:
            return {r[0] for r in c.execute("SELECT slug FROM xtb_catalog WHERE kind = ?", (kind,))}

    def xtb_catalog_age(self) -> str:
        with self._conn() as c:
            return c.execute("SELECT MIN(fetched_at) FROM xtb_catalog").fetchone()[0] or ""

    def save_xtb_catalog(self, kind: str, slugs: set[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute("DELETE FROM xtb_catalog WHERE kind = ?", (kind,))
            c.executemany("INSERT INTO xtb_catalog VALUES (?, ?, ?)", [(kind, sl, now) for sl in slugs])

    def set_weights(self, weights: dict | None) -> None:
        with self._conn() as c:
            calibration.save_weights(c, "stocks", weights)


# ---------- Price tracking ----------

def tradeable(f: fs.Filing) -> bool:
    return bool(f.ticker) and f.ticker not in {"NONE", "N/A"} and         any(t.code in ("P", "S") for t in f.transactions)


class Tracker:
    """After each scan: snapshot the price of newly added filings, refresh closes, fetch profiles."""

    def __init__(self, store: Store, client_factory, track_days: int = TRACK_DAYS, sec_factory=None,
                 xtb_factory=None):
        self.store = store
        self.client_factory = client_factory
        self.sec_factory = sec_factory
        self.xtb_factory = xtb_factory
        self.track_days = track_days
        self.last_run: str | None = None
        self.last_error: str | None = None

    def refresh(self, today: date | None = None) -> None:
        today = today or date.today()
        client = self.client_factory()
        tracked, added, companies = self.store.tracked(), self.store.added(), self.store.companies()
        cutoff = (today - timedelta(days=self.track_days)).isoformat()
        filings = [f for f in self.store.all() if tradeable(f)]
        tickers = {f.ticker for f in filings
                   if f.accession not in tracked or tracked[f.accession][1] >= cutoff} | {BENCHMARK}

        live: dict[str, float] = {}
        closes: dict[str, list[tuple[str, float]]] = {}
        errors = []
        for t in sorted(tickers):
            try:
                ch = client.chart(t, range_="1y")  # a year for the 52-week context
            except Exception as exc:  # unknown/delisted ticker - skip, keep the rest
                errors.append(f"{t}: {type(exc).__name__}")
                continue
            closes[t] = ch["closes"]
            self.store.save_prices(t, ch["closes"])
            if ch["price"]:
                live[t] = ch["price"]
            if t != BENCHMARK and t not in companies:
                try:
                    self.store.save_company(t, client.profile(t))
                except Exception:
                    pass  # profile is optional; retried on the next refresh

        for f in filings:
            if f.accession in tracked or f.ticker not in closes:
                continue
            day = added[f.accession][:10]
            if day == today.isoformat() and f.ticker in live:
                self.store.track(f.accession, f.ticker, day, live[f.ticker])
            elif (hit := market.price_on(closes[f.ticker], day)):
                self.store.track(f.accession, f.ticker, day, hit[1])

        if self.sec_factory:
            self.refresh_histories(client, filings, today)
        if self.xtb_factory:
            try:
                self.refresh_xtb(filings, today)
            except Exception:
                pass  # XTB links are a convenience; never block prices

        self.last_run = datetime.now().strftime("%H:%M:%S")
        self.last_error = f"brak cen dla {len(errors)} tickerów" if errors else None

    def refresh_xtb(self, filings: list[fs.Filing], today: date) -> None:
        """Match tickers to XTB instrument pages (sitemap catalog refreshed daily)."""
        client = self.xtb_factory()
        if self.store.xtb_catalog_age() < (today - timedelta(days=1)).isoformat():
            for kind in xtb.SITEMAPS:
                self.store.save_xtb_catalog(kind, client.catalog(kind))
        catalogs = {k: self.store.xtb_catalog(k) for k in ("stock", "cfd")}
        links = self.store.xtb_links()
        retry = (today - timedelta(days=XTB_RECHECK_DAYS)).isoformat()
        todo = {}
        for f in filings:
            known = links.get(f.ticker)
            if not known or (not known["stock"] and not known["cfd"] and known["checked_at"] < retry):
                todo.setdefault(f.ticker, f.issuer)
        for ticker, issuer in list(todo.items())[:XTB_PER_RUN]:
            self.store.save_xtb_link(ticker, xtb.resolve_stock(client, ticker, issuer, catalogs))

    def refresh_histories(self, yahoo, filings: list[fs.Filing], today: date) -> None:
        """Track record of insiders who bought: their earlier buys and how those went."""
        have = self.store.histories()
        stale = (today - timedelta(days=HISTORY_DAYS)).isoformat()
        owners = [f.owner_cik for f in filings
                  if f.owner_cik and any(t.code == "P" for t in f.transactions)
                  and (f.owner_cik not in have or have[f.owner_cik]["fetched_at"] < stale)]
        sec = self.sec_factory()
        for cik in list(dict.fromkeys(owners))[:HISTORY_PER_RUN]:
            try:
                self.store.save_history(cik, history.lookup(sec, yahoo, cik))
            except Exception:
                pass  # retried next scan


# ---------- Background scanner ----------

class Scanner:
    def __init__(self, store: Store, client_factory, pages: int, after_scan=None):
        self.store = store
        self.after_scan = after_scan
        self.client_factory = client_factory
        self.pages = pages
        self.lock = threading.Lock()
        self.running = False
        self.last_run: str | None = None
        self.last_new = 0
        self.last_error: str | None = None

    def run_once(self) -> None:
        if not self.lock.acquire(blocking=False):
            return  # a scan is already in progress
        self.running = True
        try:
            client = self.client_factory()
            seen = self.store.accessions()
            filings = fs.scan(client, self.pages, seen)
            self.store.add(filings)
            self.backfill_footnotes(client)
            self.last_new, self.last_error = len(filings), None
            if self.after_scan:
                self.after_scan()
        except Exception as exc:  # keep the loop alive; show the error in the UI
            self.last_error = safety.safe_error(exc)
        finally:
            self.last_run = datetime.now().strftime("%H:%M:%S")
            self.running = False
            self.lock.release()

    def backfill_footnotes(self, client) -> None:
        """Re-fetch buys stored before footnotes were parsed - they decide placement vs open market."""
        stale = [f for f in self.store.all()
                 if (f.footnotes is None or not f.owner_cik) and any(t.code == "P" for t in f.transactions)]
        fixed = []
        for f in stale:
            url = fs.ARCHIVE_URL.format(cik=f.issuer_cik.lstrip("0") or "0",
                                        acc_nodash=f.accession.replace("-", ""), acc=f.accession)
            try:
                xml = fs.extract_ownership_xml(client.get(url))
            except Exception:
                continue
            if xml:
                fixed.append(fs.parse_form4(xml, f.accession))
        self.store.replace(fixed)

    def trigger(self) -> None:
        threading.Thread(target=self.run_once, daemon=True).start()

    def loop(self, interval_s: int) -> None:
        while True:
            self.run_once()
            time.sleep(interval_s)


# ---------- Filtering for the view ----------

def _float(value: str | None) -> float | None:
    try:
        return float(value.replace(" ", "").replace(",", "")) if value else None
    except ValueError:
        return None


EMPTY_MARKET = {"tracked": {}, "prices": {}, "companies": {}, "histories": {}, "weights": {}, "xtb": {}}


def sparkline(path: list[tuple[str, float]], w: int = 84, h: int = 26) -> dict | None:
    """SVG polyline points for a return path; y=0 line included so up/down reads at a glance."""
    if len(path) < 2:
        return None
    vals = [v for _, v in path]
    lo, hi = min(vals + [0]), max(vals + [0])
    span = (hi - lo) or 1
    y = lambda v: round(h - 2 - (v - lo) / span * (h - 4), 1)  # noqa: E731
    pts = " ".join(f"{round(i / (len(vals) - 1) * w, 1)},{y(v)}" for i, v in enumerate(vals))
    return {"points": pts, "zero": y(0), "w": w, "h": h, "up": vals[-1] >= 0}


def build_rows(filings: list[fs.Filing], args, market_data=EMPTY_MARKET) -> tuple[list[dict], dict]:
    md = {**EMPTY_MARKET, **market_data}
    tracked, prices, companies, histories = md["tracked"], md["prices"], md["companies"], md["histories"]
    code = args.get("code", "P") if args.get("code") in {"P", "S"} else "P"
    min_value = _float(args.get("min", "100000")) or 0
    max_value = _float(args.get("max"))
    insiders_only = args.get("all_owners") != "1"
    skip_plan = args.get("plan") != "1"
    query = (args.get("q") or "").strip().lower()
    min_score = {"medium": 35, "strong": 60}.get(args.get("signal", ""), 0)
    kind_filter = args.get("kind", "")
    xtb_only = args.get("xtb") == "1"

    hits = []
    for f in filings:
        if not fs.matches(f, code, min_value, insiders_only, skip_plan):
            continue
        if max_value is not None and f.total_value(code) > max_value:
            continue
        if query and query not in f.ticker.lower() and query not in f.owner.lower() \
                and query not in f.issuer.lower():
            continue
        hits.append(f)

    # Cluster signal: several different insiders trading the same ticker.
    insiders_per_ticker: dict[str, set[str]] = defaultdict(set)
    for f in hits:
        insiders_per_ticker[f.ticker].add(f.owner)
    bench = prices.get(BENCHMARK)

    rows = []
    for f in hits:
        txs = [t for t in f.transactions if t.code == code]
        shares = sum(t.shares for t in txs)
        value = f.total_value(code)
        company = companies.get(f.ticker, {})
        closes = prices.get(f.ticker, [])
        row = {
            "accession": f.accession,
            "code": code,
            "date": max(t.date for t in txs),
            "ticker": f.ticker or "?",
            "issuer": f.issuer,
            "owner": f.owner,
            "role": f.role,
            "value": value,
            "shares": shares,
            "avg_price": value / shares if shares else 0,
            "plan": all(t.under_10b5_1 for t in txs),
            "cluster": len(insiders_per_ticker[f.ticker]),
            "url": f.edgar_url,
            "sector": company.get("sector", ""),
            "industry": company.get("industry", ""),
            "market_cap": company.get("market_cap"),
            "pct_mcap": None,
            "price_now": closes[-1][1] if closes else None,
            "base_date": None, "base_price": None,
            "perf": None, "spark": None,
            "xtb": {k: v for k, v in (md["xtb"].get(f.ticker) or {}).items() if k in ("stock", "cfd") and v},
        }
        if xtb_only and not row["xtb"]:
            continue
        if f.accession in tracked:
            _, row["base_date"], row["base_price"] = tracked[f.accession]
            row["perf"] = market.performance(closes, row["base_date"], row["base_price"], bench)
            row["spark"] = sparkline(row["perf"]["path"])
        row["context"] = market.price_context(closes, row["date"])
        row["history"] = history.for_filing(histories.get(f.owner_cik), f.accession, row["date"])
        row["signal"] = signals.analyze(row, f.footnotes, row["market_cap"], row["price_now"],
                                        context=row["context"], history=row["history"], weights=md["weights"])
        if (row["signal"]["score"] or 0) < min_score:
            continue
        if kind_filter and row["signal"]["kind"] != kind_filter:
            continue
        rows.append(row)

    sort = args.get("sort", "value")
    perf_now = lambda r: r["perf"]["now"] if r["perf"] and r["perf"]["now"] is not None else -9  # noqa: E731
    key = {"value": lambda r: r["value"], "date": lambda r: r["date"],
           "cluster": lambda r: (r["cluster"], r["value"]),
           "signal": lambda r: (r["signal"]["score"] or 0, r["value"]),
           "perf": perf_now}.get(sort, lambda r: r["value"])
    rows.sort(key=key, reverse=True)

    summary = {
        "count": len(rows),
        "total": sum(r["value"] for r in rows),
        "tickers": len({r["ticker"] for r in rows}),
        "clusters": sorted({r["ticker"] for r in rows if r["cluster"] > 1}),
        "tracked": sum(1 for r in rows if r["perf"] and r["perf"]["now"] is not None),
    }
    return rows, summary


def calibration_view(items: list[dict], factors: dict, weights: dict, horizon_label: str,
                     long_enough: bool, min_horizon_label: str) -> dict:
    """long_enough: short horizons are mostly noise, so weights may only be applied from longer ones."""
    cal = calibration.calibrate(items, factors, weights)
    cal["verdict"] = calibration.verdict(cal["spearman"])
    cal["horizon_label"] = horizon_label
    cal["min_horizon_label"] = min_horizon_label
    cal["can_apply"] = cal["can_apply"] and long_enough
    return cal


def performance_view(rows: list[dict], prices: dict, weights: dict | None = None, horizon: str = "now") -> dict:
    """Stats + chart series for the tracked subset of `rows`, with SPY over the same windows."""
    tracked = [r for r in rows if r["perf"] and r["perf"]["now"] is not None]
    bench = prices.get(BENCHMARK, [])
    bench_perfs = []
    for r in tracked:
        b0 = market.price_on(bench, r["base_date"])
        if b0:
            bench_perfs.append(market.performance(bench, r["base_date"], b0[1]))
    perfs = [r["perf"] for r in tracked]
    h = int(horizon) if horizon.isdigit() else None

    def ret(r):
        if h is None:
            return r["perf"]["now"]
        return r["perf"]["h"][h]["ret"] if h in r["perf"]["h"] else None

    labels = dict(market.HORIZONS)
    items = [{"score": r["signal"]["score"], "factors": r["signal"]["factors"], "ret": ret(r)} for r in tracked]
    return {
        "calibration": calibration_view(items, signals.FACTORS, weights or {},
                                        labels.get(h, "od dodania") if h else "od dodania",
                                        long_enough=(h or 0) >= 5, min_horizon_label="1 tydz."),
        "horizon": horizon,
        "rows": tracked,
        "stats": market.summarize(perfs),
        "path": market.average_path(perfs),
        "bench_path": market.average_path(bench_perfs),
        "horizons": market.HORIZONS,
    }


# ---------- Crypto view ----------

def build_crypto_rows(cdata: dict, args) -> tuple[list[dict], dict]:
    prices, coins, weights = cdata["prices"], cdata["coins"], cdata["weights"]
    bench = prices.get(crypto.BENCHMARK)
    trig = args.get("trigger", "")
    min_score = {"medium": 35, "strong": 60}.get(args.get("signal", ""), 0)
    min_mcap, max_mcap = _float(args.get("min_mcap")), _float(args.get("max_mcap"))
    query = (args.get("q") or "").strip().lower()

    rows = []
    with_history = args.get("history") == "1"
    for e in cdata["events"]:
        if e.get("origin", "live") != "live" and not with_history:
            continue  # replayed history is for strategy tests, not the live radar
        m = e["metrics"]
        mcap = m.get("market_cap") or 0
        if trig and trig not in e["triggers"]:
            continue
        if (min_mcap and mcap < min_mcap) or (max_mcap and mcap > max_mcap):
            continue
        if query and query not in e["symbol"].lower() and query not in e["name"].lower():
            continue
        details = coins.get(e["coin_id"])
        sig = crypto.analyze(e, details, weights)
        if sig["score"] < min_score:
            continue
        points = prices.get(e["coin_id"], [])
        perf = crypto.performance(e, points, bench)
        rows.append({**e, "signal": sig, "details": details or {}, "perf": perf,
                     "xtb": xtb.crypto_url(e["coin_id"], e["name"], cdata.get("xtb_crypto") or set()),
                     "news": (cdata.get("news") or {}).get(e["coin_id"], {}).get("items", []),
                     "spark": sparkline(perf["path"]), "mcap": mcap,
                     "price_now": points[-1][1] if points else None,
                     "ch1h": (m.get("price_change_percentage_1h_in_currency") or 0) / 100,
                     "ch24": (m.get("price_change_percentage_24h_in_currency") or 0) / 100,
                     "ch7d": (m.get("price_change_percentage_7d_in_currency") or 0) / 100,
                     "vol_ratio": vr if mcap and (vr := (m.get("total_volume") or 0) / mcap) <= crypto.MAX_SANE_VOL_RATIO else None,
                     "trigger_labels": [crypto.TRIGGER_LABELS[t] for t in e["triggers"]]})

    sort = args.get("sort", "time")
    perf_now = lambda r: r["perf"]["now"] if r["perf"]["now"] is not None else -9  # noqa: E731
    key = {"time": lambda r: r["detected_at"], "signal": lambda r: (r["signal"]["score"], r["detected_at"]),
           "perf": perf_now, "ch24": lambda r: abs(r["ch24"])}.get(sort, lambda r: r["detected_at"])
    rows.sort(key=key, reverse=True)
    summary = {"count": len(rows), "tokens": len({r["coin_id"] for r in rows}),
               "strong": sum(r["signal"]["level"] == "strong" for r in rows),
               "tracked": sum(r["perf"]["now"] is not None for r in rows)}
    return rows, summary


def crypto_performance_view(rows: list[dict], weights: dict, horizon: str = "24") -> dict:
    tracked = [r for r in rows if r["perf"]["now"] is not None]
    perfs = [r["perf"] for r in tracked]
    h = int(horizon) if horizon.isdigit() else None

    def ret(r):
        if h is None:
            return r["perf"]["now"]
        return r["perf"]["h"][h]["ret"] if h in r["perf"]["h"] else None

    labels = dict(crypto.HORIZONS)
    items = [{"score": r["signal"]["score"], "factors": r["signal"]["factors"], "ret": ret(r)} for r in tracked]
    # Direction call: score >= 50 means "expect a rise", below means "expect a fall".
    called = [(i["score"] >= 50, i["ret"] > 0) for i in items if i["ret"]]  # 0 / None: nothing happened yet
    return {
        "rows": tracked, "stats": crypto.summarize(perfs), "path": crypto.average_path(perfs),
        "horizons": crypto.HORIZONS, "horizon": horizon,
        "calibration": calibration_view(items, crypto.CRYPTO_FACTORS, weights,
                                        labels.get(h, "od wykrycia") if h else "od wykrycia",
                                        long_enough=(h or 0) >= 24, min_horizon_label="24 h"),
        "direction": {"n": len(called), "hit": sum(a == b for a, b in called) / len(called) if called else None,
                      "up_calls": sum(a for a, _ in called)},
    }


def fmt_usd(v) -> str:
    """Prices from $60k BTC down to $0.00000123 memecoins."""
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.2f}"
    if v <= 0:
        return "$0"
    decimals = max(2, min(10, 3 - math.floor(math.log10(v))))
    return f"${v:.{decimals}f}"


def fmt_ago(ts: str) -> str:
    secs = (crypto.utcnow() - crypto.parse_iso(ts)).total_seconds()
    h = int(secs // 3600)
    if h >= 24:
        return f"{h // 24} d {h % 24} h temu"
    return f"{h} h temu" if h else f"{int(secs // 60)} min temu"


# ---------- App ----------

def create_app(store: Store | None = None, scanner: Scanner | None = None,
               start_background: bool = True, tracker: Tracker | None = None,
               crypto_store: crypto.CryptoStore | None = None, crypto_scanner=None,
               whale_store: whales.WhaleStore | None = None, whale_scanner=None, static: bool = False) -> Flask:
    """static: read-only render for GitHub Pages (no forms that need a server).
    SCAN_DISABLED=1 browses a downloaded database locally without scanning."""
    start_background = start_background and os.environ.get("SCAN_DISABLED") != "1"
    app = Flask(__name__)
    # Shared CSS/JS: the version in the link changes with the content, so browsers never keep a stale copy.
    app.jinja_env.globals["asset_version"] = hashlib.sha1(b"".join(
        (Path(app.static_folder) / f).read_bytes() for f in ("app.css", "app.js"))).hexdigest()[:10]
    app.config["BUILD_ID"] = crypto.iso(crypto.utcnow())  # static export: pages poll version.json for a newer one
    store = store or Store(DB_PATH)
    cstore = crypto_store or crypto.CryptoStore(store.path)
    kstore = candles.CandleStore(store.path)
    journal = strategy.Journal(store.path)
    if scanner is None:
        ua = os.environ.get("SEC_USER_AGENT") or ("viewer only@example.com" if not start_background else None)
        if not ua:
            raise SystemExit('Set SEC_USER_AGENT, e.g. $env:SEC_USER_AGENT = "Jan Kowalski jan@example.com"')
        tracker = tracker or Tracker(store, market.YahooClient, sec_factory=lambda: fs.SecClient(ua),
                                     xtb_factory=xtb.XtbClient)
        scanner = Scanner(store, lambda: fs.SecClient(ua), SCAN_PAGES, after_scan=tracker.refresh)
    wstore = whale_store or whales.WhaleStore(WHALES_DB_PATH or store.path)
    if whale_scanner is None:
        whale_scanner = whales.WhaleScanner(wstore, whales.client_for)
    if crypto_scanner is None:
        crypto_scanner = crypto.CryptoScanner(cstore, crypto.CoinGeckoClient, news_factory=news.NewsClient)
    if start_background:
        threading.Thread(target=scanner.loop, args=(SCAN_INTERVAL_MIN * 60,), daemon=True).start()
        threading.Thread(target=crypto_scanner.loop, args=(CRYPTO_INTERVAL_MIN * 60,), daemon=True).start()
        threading.Thread(target=whale_scanner.loop, args=(WHALE_INTERVAL_MIN * 60,), daemon=True).start()

    def market_data():
        return {"tracked": store.tracked(), "prices": store.prices(), "companies": store.companies(),
                "histories": store.histories(), "weights": store.weights(), "xtb": store.xtb_links()}

    def crypto_data():
        return {"events": cstore.events(), "prices": cstore.prices(), "coins": cstore.coins(),
                "weights": cstore.weights(), "xtb_crypto": store.xtb_catalog("crypto"), "news": cstore.news()}

    def common(**extra):
        return {"scanner": scanner, "tracker": tracker, "crypto_scanner": crypto_scanner,
                "interval": SCAN_INTERVAL_MIN, "crypto_interval": CRYPTO_INTERVAL_MIN,
                "whale_scanner": whale_scanner, "whale_interval": WHALE_INTERVAL_MIN, "static": static, "build_id": app.config["BUILD_ID"],
                "generated_at": datetime.now(crypto.LOCAL_TZ).strftime("%d.%m.%Y %H:%M %Z"),
                "args": request.args, **extra}

    @app.template_filter("pct")
    def pct(v, signed=True):
        if v is None:
            return "—"
        return f"{v:+.1%}" if signed else f"{v:.0%}"

    @app.template_filter("money")
    def money(v):
        if not isinstance(v, (int, float)):  # None or a missing field (Jinja Undefined)
            return "—"
        sign = "−" if v < 0 else ""
        for div, suf in [(1e12, " bln"), (1e9, " mld"), (1e6, " mln")]:
            if abs(v) >= div:
                return f"{sign}${abs(v) / div:,.1f}{suf}"
        return f"{sign}${abs(v):,.0f}"

    app.add_template_filter(fmt_usd, "usd")
    app.add_template_filter(fmt_ago, "ago")
    app.add_template_filter(crypto.local, "local")
    app.add_template_filter(lambda a: f"{a[:6]}…{a[-4:]}" if a else "—", "short")

    @app.get("/")
    def index():
        filings = store.all()
        rows, summary = build_rows(filings, request.args, market_data())
        return render_template("index.html", rows=rows, summary=summary, stored=len(filings),
                               **common(page="scan"))

    @app.get("/wyniki")
    def results():
        md = market_data()
        rows, summary = build_rows(store.all(), request.args, md)
        perf = performance_view(rows, md["prices"], md["weights"], request.args.get("h", "now"))
        return render_template("performance.html", perf=perf, summary=summary,
                               scope="stocks", **common(page="perf"))

    @app.get("/filing/<accession>")
    def filing(accession):
        filings = store.all()
        f = next((x for x in filings if x.accession == accession), None)
        if f is None:
            abort(404)
        code = "P" if any(t.code == "P" for t in f.transactions) else "S"
        md = market_data()
        rows, _ = build_rows(filings, {"code": code, "min": "0", "all_owners": "1", "plan": "1"}, md)
        row = next((r for r in rows if r["accession"] == accession), None)
        if row is None:
            abort(404)
        closes = md["prices"].get(f.ticker, [])
        start = market.price_on(closes, row["base_date"] or row["date"])
        closes = closes[max(0, (start[0] if start else len(closes)) - 60):]  # ~3 months of context
        return render_template("filing.html", r=row, f=f, company=md["companies"].get(f.ticker, {}),
                               closes=closes, horizons=market.HORIZONS,
                               history_horizons=history.HORIZONS, **common(page="scan"))

    @app.post("/scan")
    def scan_now():
        scanner.trigger()
        return redirect(url_for("index", **request.args))

    # ----- crypto -----

    @app.get("/krypto")
    def crypto_index():
        rows, summary = build_crypto_rows(crypto_data(), request.args)
        tabs = [("radar", "Radar", url_for("crypto_index")), ("open", "Otwarte pozycje", url_for("spot_open"))]
        return render_template("crypto.html", rows=rows, summary=summary, tabs=tabs, active="radar",
                               **common(page="crypto"))

    def strategy_catalog(rows, prices, now, fee=strategy.FEE, stake=strategy.STAKE, custom=None) -> list[dict]:
        """Presets + saved strategies (+ an unsaved editor preview).
        Results come from the trade journal written by the scans; a strategy without journal
        entries (or a different fee) is simulated on the stored candles instead."""
        events = {r["id"]: r for r in rows}
        loaded = {}

        def paths():
            if "p" not in loaded:
                loaded["p"] = kstore.paths()
            return loaded["p"]
        catalog = [{"key": k, "name": n, "desc": d, "params": p, "saved": False} for k, n, d, p in strategy.PRESETS]
        catalog += [{"key": f"c{s['id']}", "id": s["id"], "name": s["name"], "desc": "Twoja strategia",
                     "params": s["params"], "saved": True} for s in cstore.strategies()]
        if custom is not None:
            catalog.append({"key": "custom", "name": "Nowa (niezapisana)", "desc": "Podgląd z edytora",
                            "params": custom, "saved": False})
        for s in catalog:
            jrows = journal.rows(s["key"], strategy.version(s["params"])) \
                if s["key"] != "custom" and abs(fee - strategy.FEE) < 1e-9 else []
            s["from_journal"] = bool(jrows)
            if jrows:
                s["result"] = strategy.summarize(strategy.journal_trades(jrows, events), stake)
            else:
                s["result"] = strategy.backtest(rows, prices, s["params"], now, fee, stake, paths())
            closed = [t for t in s["result"]["trades"] if t["status"] == "closed"]
            s["hist_closed"] = sum(t["row"].get("origin") == "backfill" for t in closed)
            s["rules"] = strategy.describe(s["params"])
            s["full"] = {**strategy.DEFAULTS, **s["params"]}
        return catalog

    @app.get("/krypto/wyniki")
    def crypto_results():
        cd = crypto_data()
        rows, summary = build_crypto_rows(cd, request.args)
        perf = crypto_performance_view(rows, cd["weights"], request.args.get("h", "24"))
        # Summary of every crypto section: spot radar (above), leveraged strategies, whale spot signals.
        all_rows, _ = build_crypto_rows(cd, {"history": "1"})
        strategies = strategy_catalog(all_rows, cd["prices"], crypto.utcnow())
        ranked = sorted((s for s in strategies if s["result"]["closed"]), key=lambda s: -s["result"]["pnl"])
        wdata = whale_xtb(whales.build(wstore))
        wsignals = sorted((t for t in wdata["tokens"] if t.get("signal")),
                          key=lambda t: t["signal"]["flagged_at"], reverse=True)
        wrets = [t["perf"]["now"] for t in wsignals if t.get("perf") and t["perf"]["now"] is not None]
        whale_summary = {"n": len(wsignals), "avg": sum(wrets) / len(wrets) if wrets else None,
                         "win_rate": sum(r > 0 for r in wrets) / len(wrets) if wrets else None}
        return render_template("crypto_perf.html", perf=perf, summary=summary, scope="crypto",
                               strategies=strategies, best=ranked[0] if ranked else None,
                               wsignals=wsignals, whale_summary=whale_summary,
                               whale_horizons=whales.HORIZONS, **common(page="crypto_perf"))

    @app.get("/krypto/<int:event_id>")
    def crypto_event(event_id):
        cd = crypto_data()
        rows, _ = build_crypto_rows(cd, {"history": "1"})
        row = next((r for r in rows if r["id"] == event_id), None)
        if row is None:
            abort(404)
        start = crypto.iso(crypto.parse_iso(row["detected_at"]) - timedelta(days=2))
        bars = kstore.paths([event_id]).get(event_id)
        if bars:  # chart from candles (closes), thinned to keep the page light
            step = max(1, len(bars) // 300)
            points = [(b[0], b[4]) for b in bars[::step]]
        else:
            points = [p for p in cd["prices"].get(row["coin_id"], []) if p[0] >= start]
        others = [r for r in rows if r["coin_id"] == row["coin_id"] and r["id"] != event_id]
        labels = [crypto.local(ts, "%d.%m %H:%M") for ts, _ in points]
        return render_template("crypto_coin.html", r=row, points=points, labels=labels, others=others,
                               horizons=crypto.HORIZONS, **common(page="crypto"))

    @app.get("/krypto/strategie")
    def crypto_strategies():
        cd = crypto_data()
        rows, _ = build_crypto_rows(cd, {"history": "1"})
        now = crypto.utcnow()
        fee = (_float(request.args.get("fee")) or strategy.FEE * 100) / 100
        stake = _float(request.args.get("stake")) or strategy.STAKE
        custom = strategy.params_from_form(request.args) if request.args.get("custom") else None
        catalog = strategy_catalog(rows, cd["prices"], now, fee, stake, custom)
        selected_key = request.args.get("s") or ("custom" if custom is not None else catalog[0]["key"])
        selected = next((s for s in catalog if s["key"] == selected_key), catalog[0])
        # Entry candidates right now: matching events from the last hour that are still open.
        fresh = now - timedelta(minutes=60)
        candidates = [t for t in selected["result"]["trades"]
                      if t["status"] == "open" and crypto.parse_iso(t["entry_at"]) >= fresh]
        tabs, active = strategy_tabs("strategies")
        return render_template("crypto_strategies.html", catalog=catalog, selected=selected, tabs=tabs, active=active,
                               candidates=candidates, fee=fee, stake=stake,
                               form=strategy.form_values(selected["params"]),
                               trigger_labels=crypto.TRIGGER_LABELS, **common(page="strategies"))

    # ----- positions: open trades, trade history, one position's chart -----

    def journal_positions(cd) -> tuple[list[dict], dict]:
        """Every trade of every strategy (current rule versions) from the journal, newest first."""
        rows, _ = build_crypto_rows(cd, {"history": "1"})
        events = {r["id"]: r for r in rows}
        catalog = [(k, n, p) for k, n, _, p in strategy.PRESETS if k not in strategy.REFERENCE_ONLY] + \
                  [(f"c{x['id']}", x["name"], x["params"]) for x in cstore.strategies()]
        out = []
        for key, name, params in catalog:
            full = {**strategy.DEFAULTS, **params}
            for t in strategy.journal_trades(journal.rows(key, strategy.version(params)), events):
                t.update(strategy_key=key, strategy_name=name, full=full,
                         tp_price=t["entry"] * (1 + full["tp"]), sl_price=t["entry"] * (1 - full["sl"]),
                         deadline=crypto.iso(crypto.parse_iso(t["entry_at"]) + timedelta(hours=full["hold_h"])))
                out.append(t)
        out.sort(key=lambda t: t["entry_at"], reverse=True)
        return out, events

    def last_prices(event_ids: list[int], cd) -> dict[int, tuple[str, float]]:
        """Latest known price per signal: last candle close, else our latest snapshot."""
        out = {}
        for eid, bars in kstore.paths(event_ids).items():
            if bars:
                out[eid] = (bars[-1][0], bars[-1][4])
        return out

    def strategy_tabs(active):
        return [("strategies", "Strategie", url_for("crypto_strategies")),
                ("open", "Otwarte pozycje", url_for("strategy_open")),
                ("history", "Historia transakcji", url_for("strategy_history"))], active

    @app.get("/krypto/strategie/otwarte")
    def strategy_open():
        cd = crypto_data()
        positions, events = journal_positions(cd)
        open_ = [t for t in positions if t["status"] == "open"]
        now_px = last_prices(list({t["event_id"] for t in open_}), cd)
        for t in open_:
            ts, px = now_px.get(t["event_id"], (None, events[t["event_id"]].get("price_now")))
            t["now_price"], t["now_ts"] = px, ts
            t["now_ret"] = px / t["entry"] - 1 if px else None
        tabs, active = strategy_tabs("open")
        return render_template("positions.html", mode="open", positions=open_, tabs=tabs, active=active,
                               heading="Otwarte pozycje", **common(page="strategies"))

    @app.get("/krypto/strategie/historia")
    def strategy_history():
        cd = crypto_data()
        positions, _ = journal_positions(cd)
        closed = sorted((t for t in positions if t["status"] == "closed"), key=lambda t: t["exit_at"], reverse=True)
        names = sorted({t["strategy_name"] for t in closed})
        tabs, active = strategy_tabs("history")
        return render_template("positions.html", mode="history", positions=closed, strategy_names=names, tabs=tabs,
                               active=active, heading="Historia transakcji", **common(page="strategies"))

    @app.get("/krypto/pozycja/<key>/<int:event_id>")
    def position(key, event_id):
        cd = crypto_data()
        positions, events = journal_positions(cd)
        t = next((x for x in positions if x["strategy_key"] == key and x["event_id"] == event_id), None)
        if t is None:
            abort(404)
        bars = kstore.paths([event_id]).get(event_id, [])
        if not bars:  # no candles: fall back to price snapshots as flat bars
            bars = [(ts, p, p, p, p) for ts, p in cd["prices"].get(t["row"]["coin_id"], [])]
        start = crypto.iso(crypto.parse_iso(t["entry_at"]) - timedelta(hours=2))
        stop_dt = crypto.parse_iso(t["exit_at"]) + timedelta(hours=3) if t["exit_at"] else crypto.utcnow()
        view = [b for b in bars if start <= b[0] <= crypto.iso(stop_dt)]
        step = max(1, len(view) // 400)
        view = view[::step]
        if not t["exit_at"] and bars:
            t["now_price"], t["now_ret"] = bars[-1][4], bars[-1][4] / t["entry"] - 1
        chart = {"labels": [crypto.local(b[0], "%d.%m %H:%M") for b in view], "ts": [b[0] for b in view],
                 "close": [b[4] for b in view], "high": [b[2] for b in view], "low": [b[3] for b in view]}
        return render_template("position.html", t=t, chart=chart, has_candles=bool(kstore.paths([event_id])),
                               **common(page="strategies"))

    @app.get("/krypto/otwarte")
    def spot_open():
        cd = crypto_data()
        rows, _ = build_crypto_rows(cd, {})
        now = crypto.utcnow()
        live = [r for r in rows if crypto.parse_iso(r["detected_at"]) >= now - timedelta(days=crypto.HORIZONS[-1][0] / 24)]
        now_px = last_prices([r["id"] for r in live], cd)
        for r in live:
            ts, px = now_px.get(r["id"], (None, r["price_now"]))
            r["now_price"] = px
            r["now_ret"] = px / r["base_price"] - 1 if px and r["base_price"] else None
            r["ends_at"] = crypto.iso(crypto.parse_iso(r["detected_at"]) + timedelta(hours=crypto.HORIZONS[-1][0]))
        tabs = [("radar", "Radar", url_for("crypto_index")), ("open", "Otwarte pozycje", url_for("spot_open"))]
        return render_template("spot_open.html", rows=live, tabs=tabs, active="open", **common(page="crypto"))

    @app.post("/krypto/strategie/zapisz")
    def crypto_strategy_save():
        name = (request.form.get("name") or "").strip()[:60] or "Moja strategia"
        cstore.save_strategy(name, strategy.params_from_form(request.form))
        new_id = cstore.strategies()[-1]["id"]
        return redirect(url_for("crypto_strategies", s=f"c{new_id}"))

    @app.post("/krypto/strategie/<int:strategy_id>/usun")
    def crypto_strategy_delete(strategy_id):
        cstore.delete_strategy(strategy_id)
        return redirect(url_for("crypto_strategies"))

    @app.post("/krypto/scan")
    def crypto_scan_now():
        crypto_scanner.trigger()
        return redirect(url_for("crypto_index"))

    # ----- calibration -----

    @app.post("/kalibracja/<scope>/<action>")
    def calibrate(scope, action):
        if scope not in {"stocks", "crypto"} or action not in {"apply", "reset"}:
            abort(404)
        target = store if scope == "stocks" else cstore
        form = request.form.to_dict()
        if action == "reset":
            target.set_weights(None)
        elif scope == "stocks":
            md = market_data()
            rows, _ = build_rows(store.all(), form, md)
            cal = performance_view(rows, md["prices"], md["weights"], form.get("h", "now"))["calibration"]
            if cal["can_apply"]:
                store.set_weights(cal["changes"])
        else:
            cd = crypto_data()
            rows, _ = build_crypto_rows(cd, form)
            cal = crypto_performance_view(rows, cd["weights"], form.get("h", "24"))["calibration"]
            if cal["can_apply"]:
                cstore.set_weights(cal["changes"])
        return redirect(url_for("results" if scope == "stocks" else "crypto_results", **form))

    # ----- api -----

    @app.get("/api/filings")
    def api_filings():
        rows, summary = build_rows(store.all(), request.args, market_data())
        return jsonify(summary=summary, rows=rows)

    @app.get("/api/crypto")
    def api_crypto():
        rows, summary = build_crypto_rows(crypto_data(), request.args)
        return jsonify(summary=summary, rows=rows)

    # ----- whales -----

    def whale_xtb(data: dict) -> dict:
        cat = store.xtb_catalog("crypto")
        for t in data["tokens"]:
            t["xtb"] = xtb.crypto_url((t["symbol"] or "").lower(), t["name"] or "", cat)
        return data

    @app.get("/wieloryby")
    def whales_index():
        data = whale_xtb(whales.build(wstore, include_treasury=request.args.get("treasury") == "1"))
        min_usd = _float(request.args.get("min_tx")) or whales.BIG_TX_USD
        big = [b for b in data["big"] if (b["usd"] or 0) >= min_usd]
        tone = request.args.get("tone", "")
        if tone:
            big = [b for b in big if b["tone"] == tone]
        tabs = [("overview", "Przegląd", url_for("whales_index")), ("open", "Otwarte pozycje", url_for("whales_open"))]
        return render_template("whales.html", data=data, big=big[:100], min_usd=min_usd, tabs=tabs, active="overview",
                               tokens=wstore.tokens(), **common(page="whales"))

    @app.get("/wieloryby/otwarte")
    def whales_open():
        data = whale_xtb(whales.build(wstore))
        now = crypto.utcnow()
        horizon = timedelta(days=whales.HORIZONS[-1][0])
        open_ = sorted((t for t in data["tokens"] if t.get("signal")
                        and crypto.parse_iso(t["signal"]["flagged_at"]) >= now - horizon),
                       key=lambda t: t["signal"]["flagged_at"], reverse=True)
        for t in open_:
            t["ends_at"] = crypto.iso(crypto.parse_iso(t["signal"]["flagged_at"]) + horizon)
        tabs = [("overview", "Przegląd", url_for("whales_index")), ("open", "Otwarte pozycje", url_for("whales_open"))]
        return render_template("whales_open.html", rows=open_, tabs=tabs, active="open", horizons=whales.HORIZONS,
                               **common(page="whales"))

    @app.get("/wieloryby/token/<token>")
    def whale_token(token):
        data = whale_xtb(whales.build(wstore, include_treasury=True))
        token = whales.norm_key(token)
        row = next((t for t in data["tokens"] if t["token"] == token), None)
        if row is None:
            abort(404)
        transfers = [b for b in wstore.transfers() if b["token"] == row["token"]]
        for t in transfers:
            t["text"], t["tone"] = whales.describe_transfer(t)
            t["explorer"] = whales.explorer_for(t["token"])
        whale_map = wstore.whales()
        points = wstore.token_prices().get(row["token"], [])
        return render_template("whale_token.html", t=row, transfers=transfers[:150], whale_map=whale_map,
                               points=points, labels=[crypto.local(ts, "%d.%m %H:%M") for ts, _ in points],
                               horizons=whales.HORIZONS, **common(page="whales"))

    @app.get("/wieloryby/portfel/<address>")
    def whale_wallet(address):
        address = whales.norm(address)
        data = whales.build(wstore, include_treasury=True)
        w = next((x for x in data["whales"] if x["address"] == address), None)
        if w is None:
            abort(404)
        transfers = [t for t in wstore.transfers() if t["address"] == address]
        for t in transfers:
            t["text"], t["tone"] = whales.describe_transfer(t)
            t["explorer"] = whales.explorer_for(t["token"])
        return render_template("whale_wallet.html", w=w, transfers=transfers[:150], **common(page="whales"))

    @app.post("/wieloryby/dodaj")
    def whale_add():
        address = (request.form.get("address") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}", address):
            return redirect(url_for("whales_index", error="Niepoprawny adres. Podaj adres EVM (0x…) albo Solany."))
        whale_scanner.add_wallet(address, (request.form.get("label") or "").strip()[:60])
        return redirect(url_for("whale_wallet", address=whales.norm(address)))

    @app.post("/wieloryby/<address>/usun")
    def whale_delete(address):
        wstore.delete_whale(address)
        return redirect(url_for("whales_index"))

    @app.post("/wieloryby/scan")
    def whale_scan_now():
        whale_scanner.trigger()
        return redirect(url_for("whales_index"))

    @app.get("/api/whales")
    def api_whales():
        data = whales.build(wstore)
        return jsonify(tokens=[{k: v for k, v in t.items() if k != "positions"} for t in data["tokens"]],
                       big=data["big"][:200])

    @app.get("/api/status")
    def api_status():
        return jsonify(running=scanner.running, last_run=scanner.last_run,
                       last_new=scanner.last_new, last_error=scanner.last_error,
                       prices_run=getattr(tracker, "last_run", None),
                       crypto_running=getattr(crypto_scanner, "running", False),
                       crypto_last_run=getattr(crypto_scanner, "last_run", None),
                       whales_running=getattr(whale_scanner, "running", False))

    return app


if __name__ == "__main__":
    create_app().run(port=5050, debug=False, use_reloader=False)

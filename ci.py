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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app as webapp
import crypto
import form4_scanner as fs
import market
import whales
import xtb

SITE = Path("site")
STATUS = Path("scan_status.json")  # scanner status from the scan step, shown by the export step
WHALES_EVERY_MIN = int(os.environ.get("WHALES_EVERY_MIN", "30"))


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def timed(name: str, fn) -> None:
    t0 = time.monotonic()
    fn()
    log(f"{name}: {time.monotonic() - t0:.0f} s")


def scan(only: set[str], force_whales: bool):
    store = webapp.Store(webapp.DB_PATH)
    cstore = crypto.CryptoStore(webapp.DB_PATH)
    wstore = whales.WhaleStore(webapp.DB_PATH)
    ua = os.environ.get("SEC_USER_AGENT")
    tracker = webapp.Tracker(store, market.YahooClient, sec_factory=(lambda: fs.SecClient(ua)) if ua else None,
                             xtb_factory=xtb.XtbClient)
    scanner = webapp.Scanner(store, lambda: fs.SecClient(ua), webapp.SCAN_PAGES, after_scan=tracker.refresh)
    cscanner = crypto.CryptoScanner(cstore, crypto.CoinGeckoClient)
    wscanner = whales.WhaleScanner(wstore, whales.BlockscoutClient)

    if "stocks" in only:
        if ua:
            timed("insiderzy + ceny", scanner.run_once)
            log(f"  nowe zgłoszenia: {scanner.last_new}, błąd: {scanner.last_error}, ceny: {tracker.last_error}")
        else:
            log("insiderzy: pominięte, brak sekretu SEC_USER_AGENT")
    if "crypto" in only:
        timed("krypto", cscanner.run_once)
        log(f"  nowe ruchy: {cscanner.last_new}, błąd: {cscanner.last_error}")
    # Whale holders change slowly and the scan is the heaviest; every ~30 min is plenty.
    if "whales" in only and (force_whales or datetime.now(timezone.utc).minute % WHALES_EVERY_MIN < 10):
        timed("wieloryby", wscanner.run_once)
        log(f"  nowe portfele: {wscanner.last_new}, błąd: {wscanner.last_error}")

    objs = {"stocks": scanner, "tracker": tracker, "crypto": cscanner, "whales": wscanner}
    fields = ("last_run", "last_new", "last_error")
    if only:
        old = read_status()
        STATUS.write_text(json.dumps({**old, **{k: {f: getattr(o, f, None) for f in fields}
                                                for k, o in objs.items() if getattr(o, "last_run", None)}}))
    else:
        for k, vals in read_status().items():
            if k in objs and isinstance(vals, dict):
                for f, v in vals.items():
                    setattr(objs[k], f, v)
    return store, cstore, wstore, scanner, tracker, cscanner, wscanner


def read_status() -> dict:
    """Scanner status saved by the previous step; a missing, empty or broken file is just 'no status'."""
    try:
        data = json.loads(STATUS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_methods(obj, names: list[str]) -> None:
    """The export renders ~1000 pages from one unchanging database: read each table once."""
    for name in names:
        setattr(obj, name, functools.cache(getattr(obj, name)))


def export(parts, base_path: str) -> int:
    store, cstore, wstore, scanner, tracker, cscanner, wscanner = parts
    _cache_methods(store, ["all", "tracked", "prices", "companies", "histories", "weights"])
    _cache_methods(cstore, ["events", "prices", "coins", "weights", "strategies"])
    original_build = whales.build
    whales.build = functools.cache(lambda s, now=None, include_treasury=False: original_build(s, now, include_treasury))
    try:
        flask_app = webapp.create_app(store=store, scanner=scanner, tracker=tracker, start_background=False,
                                      crypto_store=cstore, crypto_scanner=cscanner,
                                      whale_store=wstore, whale_scanner=wscanner, static=True)
        client = flask_app.test_client()
        base_url = "http://localhost" + base_path.rstrip("/")

        pages = ["/", "/wyniki", "/krypto", "/krypto/wyniki", "/krypto/strategie", "/wieloryby"]
        for code in "PS":
            rows, _ = webapp.build_rows(store.all(), {"code": code, "min": "0", "all_owners": "1", "plan": "1"},
                                        {"tracked": store.tracked(), "prices": store.prices(),
                                         "companies": store.companies()})
            pages += [f"/filing/{r['accession']}" for r in rows]
        cutoff = crypto.iso(crypto.utcnow() - timedelta(days=crypto.TRACK_DAYS))
        pages += [f"/krypto/{e['id']}" for e in cstore.events() if e["detected_at"] >= cutoff]
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
        (SITE / ".nojekyll").write_text("")
        (SITE / "version.json").write_text(json.dumps({"build": flask_app.config["BUILD_ID"]}))
        log(f"eksport: {len(pages) - failed} stron do {SITE}/")
        return failed
    finally:
        whales.build = original_build


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", default="stocks,crypto,whales")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--whales", action="store_true", help="scan whales regardless of the clock")
    p.add_argument("--base", default=os.environ.get("PAGES_BASE")
                   or "/" + os.environ.get("GITHUB_REPOSITORY", "/").split("/")[-1])
    args = p.parse_args(argv)
    parts = scan(set() if args.no_scan else set(args.only.split(",")), args.whales)
    if args.no_export:
        return 0
    return 1 if export(parts, args.base) else 0


if __name__ == "__main__":
    sys.exit(main())

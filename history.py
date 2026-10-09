"""Track record of an insider: how their past open-market buys performed.

Pulls the owner's past Form 4s from SEC (data.sec.gov submissions), keeps the
P transactions and measures the stock 1/3/6 months later with Yahoo closes.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import form4_scanner as fs
import market

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
LOOKBACK_YEARS = 3
MAX_FILINGS = 40  # newest Form 4s checked per insider; keeps one lookup under ~10 s
HORIZONS = [(21, "1 mies."), (63, "3 mies."), (126, "6 mies.")]


def past_buys(sec, owner_cik: str, skip_accession: str = "", today: date | None = None) -> list[dict]:
    today = today or date.today()
    data = json.loads(sec.get(SUBMISSIONS_URL.format(cik=owner_cik.lstrip("0"))))
    recent = data.get("filings", {}).get("recent", {})
    since = (today - timedelta(days=365 * LOOKBACK_YEARS)).isoformat()
    accs = [a for f, a, d in zip(recent.get("form", []), recent.get("accessionNumber", []),
                                 recent.get("filingDate", []))
            if f == "4" and d >= since and a != skip_accession][:MAX_FILINGS]
    buys = []
    for acc in accs:
        url = fs.ARCHIVE_URL.format(cik=owner_cik.lstrip("0"), acc_nodash=acc.replace("-", ""), acc=acc)
        try:
            xml = fs.extract_ownership_xml(sec.get(url))
            f = fs.parse_form4(xml, acc) if xml else None
        except Exception:
            continue
        if not f or not f.ticker:
            continue
        txs = [t for t in f.transactions if t.code == "P" and t.shares]
        if txs:
            value = f.total_value("P")
            buys.append({"accession": acc, "ticker": f.ticker, "date": min(t.date for t in txs),
                         "value": value, "price": value / sum(t.shares for t in txs)})
    return buys


def score_buys(buys: list[dict], closes_by_ticker: dict[str, list[tuple[str, float]]]) -> dict:
    """Return after each past buy, from the close on the buy date (keys are str: stored as JSON)."""
    for b in buys:
        closes = closes_by_ticker.get(b["ticker"], [])
        at = market.price_on(closes, b["date"])
        b["ret"] = {}
        if not at:
            continue
        i, base = at
        for n, _ in HORIZONS:
            if i + n < len(closes):
                b["ret"][str(n)] = closes[i + n][1] / base - 1
    return {"buys": buys, "count": len(buys)}


def summarize(buys: list[dict]) -> dict:
    summary = {}
    for n, label in HORIZONS:
        rets = [b["ret"][str(n)] for b in buys if str(n) in b.get("ret", {})]
        summary[n] = {"label": label, "n": len(rets),
                      "avg": sum(rets) / len(rets) if rets else None,
                      "win_rate": sum(r > 0 for r in rets) / len(rets) if rets else None}
    return {"buys": buys, "count": len(buys), "summary": summary}


def for_filing(stored: dict | None, accession: str, tx_date: str) -> dict | None:
    """The insider's record *before* this filing, so a buy never vouches for itself."""
    if not stored:
        return None
    earlier = [b for b in stored.get("buys", []) if b["accession"] != accession and b["date"] < tx_date]
    return summarize(earlier) if earlier else None


def lookup(sec, yahoo, owner_cik: str, skip_accession: str = "") -> dict:
    buys = past_buys(sec, owner_cik, skip_accession)
    closes = {}
    for t in {b["ticker"] for b in buys}:
        try:
            closes[t] = yahoo.chart(t, range_="5y")["closes"]
        except Exception:
            pass
    return score_buys(buys, closes)

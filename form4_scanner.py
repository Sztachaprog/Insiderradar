"""Scan the latest SEC Form 4 filings and report insider open-market purchases.

Data source: SEC EDGAR (free, no API key). SEC requires a User-Agent with
contact info - set SEC_USER_AGENT, e.g. "Jan Kowalski jan@example.com".

Usage:
    python form4_scanner.py                      # last ~100 filings, buys >= $100k
    python form4_scanner.py --min-value 250000 --pages 3 --json
    python form4_scanner.py --state seen.json    # skip filings already reported
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
REQUEST_DELAY = 0.15  # SEC limit is 10 req/s

# Transaction codes: P = open-market purchase, S = open-market sale.
# Others (A awards, M option exercises, F tax withholding, G gifts) carry little signal.
CODE_LABELS = {"P": "BUY", "S": "SELL"}


@dataclass
class Transaction:
    date: str
    code: str
    shares: float
    price: float
    acquired_disposed: str
    under_10b5_1: bool

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass
class Filing:
    accession: str
    ticker: str
    issuer: str
    owner: str
    is_director: bool
    is_officer: bool
    officer_title: str
    is_ten_percent_owner: bool
    transactions: list[Transaction] = field(default_factory=list)
    issuer_cik: str = ""
    footnotes: str | None = None  # footnotes + remarks (None = stored before parsing them)
    owner_cik: str = ""

    @property
    def edgar_url(self) -> str:
        cik = self.issuer_cik.lstrip("0") or "0"
        return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                f"{self.accession.replace('-', '')}/{self.accession}-index.htm")

    def total_value(self, code: str) -> float:
        return sum(t.value for t in self.transactions if t.code == code)

    @property
    def role(self) -> str:
        parts = []
        if self.is_officer:
            parts.append(self.officer_title or "Officer")
        if self.is_director:
            parts.append("Director")
        if self.is_ten_percent_owner:
            parts.append("10% owner")
        return ", ".join(parts) or "Other"


# ---------- HTTP ----------

class SecClient:
    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0

    def get(self, url: str, **params) -> str:
        wait = REQUEST_DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, params=params or None, timeout=30)
        self._last = time.monotonic()
        resp.raise_for_status()
        return resp.text


# ---------- Parsing ----------

def parse_feed(atom_xml: str) -> list[tuple[str, str]]:
    """Return unique (cik, accession) pairs from the EDGAR 'current filings' Atom feed.

    Each Form 4 shows up twice (issuer + reporting owner); dedupe on accession.
    """
    root = ET.fromstring(atom_xml)
    seen: dict[str, str] = {}
    for entry in root.findall("a:entry", ATOM_NS):
        link = entry.find("a:link", ATOM_NS)
        if link is None:
            continue
        m = re.search(r"/data/(\d+)/\d+/(\d{10}-\d{2}-\d{6})-index", link.get("href", ""))
        if m and m.group(2) not in seen:
            seen[m.group(2)] = m.group(1)
    return [(cik, acc) for acc, cik in seen.items()]


def extract_ownership_xml(submission_txt: str) -> str | None:
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", submission_txt, re.S)
    return m.group(0) if m else None


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).lower() in {"1", "true"}


def _num(node: ET.Element, path: str) -> float:
    try:
        return float(_text(node, path, "0") or 0)
    except ValueError:
        return 0.0


def parse_form4(xml: str, accession: str) -> Filing:
    root = ET.fromstring(xml)
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")  # first owner; joint filings are rare
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    plan_flag = _flag(root, "aff10b5One")

    filing = Filing(
        accession=accession,
        ticker=_text(issuer, "issuerTradingSymbol").upper(),
        issuer=_text(issuer, "issuerName"),
        owner=_text(owner, "reportingOwnerId/rptOwnerName"),
        is_director=_flag(rel, "isDirector"),
        is_officer=_flag(rel, "isOfficer"),
        officer_title=_text(rel, "officerTitle"),
        is_ten_percent_owner=_flag(rel, "isTenPercentOwner"),
        issuer_cik=_text(issuer, "issuerCik"),
        owner_cik=_text(owner, "reportingOwnerId/rptOwnerCik"),
        footnotes=" ".join(filter(None, [*(" ".join(fn.itertext()).strip()
                                           for fn in root.findall("footnotes/footnote")),
                                         _text(root, "remarks")])),
    )
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        filing.transactions.append(
            Transaction(
                date=_text(tx, "transactionDate/value"),
                code=_text(tx, "transactionCoding/transactionCode"),
                shares=_num(tx, "transactionAmounts/transactionShares/value"),
                price=_num(tx, "transactionAmounts/transactionPricePerShare/value"),
                acquired_disposed=_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                under_10b5_1=plan_flag,
            )
        )
    return filing


# ---------- Filtering ----------

def matches(filing: Filing, code: str, min_value: float, insiders_only: bool,
            skip_10b5_1: bool) -> bool:
    if insiders_only and not (filing.is_officer or filing.is_director):
        return False
    relevant = [t for t in filing.transactions if t.code == code]
    if not relevant:
        return False
    if skip_10b5_1 and all(t.under_10b5_1 for t in relevant):
        return False
    return filing.total_value(code) >= min_value


# ---------- State ----------

def load_seen(path: Path | None) -> set[str]:
    if path and path.exists():
        return set(json.loads(path.read_text()))
    return set()


def save_seen(path: Path | None, seen: set[str], keep: int = 5000) -> None:
    if path:
        path.write_text(json.dumps(sorted(seen)[-keep:]))


# ---------- Main ----------

def scan(client: SecClient, pages: int, seen: set[str]) -> list[Filing]:
    pairs: list[tuple[str, str]] = []
    for page in range(pages):
        feed = client.get(FEED_URL, action="getcurrent", type="4", owner="include",
                          count=100, start=page * 100, output="atom")
        pairs.extend(parse_feed(feed))

    filings = []
    for cik, acc in dict.fromkeys(pairs):
        if acc in seen:
            continue
        url = ARCHIVE_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)
        try:
            xml = extract_ownership_xml(client.get(url))
            if xml:
                filings.append(parse_form4(xml, acc))
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"skip {acc}: {exc}", file=sys.stderr)
            continue
        seen.add(acc)
    return filings


def format_row(f: Filing, code: str) -> str:
    txs = [t for t in f.transactions if t.code == code]
    shares = sum(t.shares for t in txs)
    avg = f.total_value(code) / shares if shares else 0
    plan = " [10b5-1]" if txs and all(t.under_10b5_1 for t in txs) else ""
    return (f"{f.ticker or '?':<6} {CODE_LABELS.get(code, code):<4} ${f.total_value(code):>13,.0f}  "
            f"{shares:>11,.0f} sh @ ${avg:,.2f}  {f.owner} ({f.role}){plan}  "
            f"[{txs[0].date if txs else ''}] {f.accession}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--code", default="P", choices=["P", "S"], help="P = buys (default), S = sells")
    p.add_argument("--min-value", type=float, default=100_000, help="min total $ per filing")
    p.add_argument("--pages", type=int, default=1, help="feed pages, 100 filings each")
    p.add_argument("--all-owners", action="store_true", help="include non-officer/director (e.g. 10%% owners)")
    p.add_argument("--include-10b5-1", action="store_true", help="keep trades under pre-scheduled 10b5-1 plans")
    p.add_argument("--state", type=Path, help="JSON file of already-reported accessions")
    p.add_argument("--json", action="store_true", help="output JSON instead of a table")
    args = p.parse_args(argv)

    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        print('Set SEC_USER_AGENT, e.g. PowerShell: $env:SEC_USER_AGENT = "Jan Kowalski jan@example.com"',
              file=sys.stderr)
        return 2

    seen = load_seen(args.state)
    filings = scan(SecClient(ua), args.pages, seen)
    hits = [f for f in filings
            if matches(f, args.code, args.min_value, not args.all_owners, not args.include_10b5_1)]
    hits.sort(key=lambda f: f.total_value(args.code), reverse=True)
    save_seen(args.state, seen)

    if args.json:
        print(json.dumps([{**asdict(f), "role": f.role, "total_value": f.total_value(args.code)}
                          for f in hits], indent=2))
    elif not hits:
        print(f"No matching filings among {len(filings)} new Form 4s.")
    else:
        print(f"{len(hits)} match(es) among {len(filings)} new Form 4s:\n")
        for f in hits:
            print(format_row(f, args.code))
    return 0


if __name__ == "__main__":
    sys.exit(main())

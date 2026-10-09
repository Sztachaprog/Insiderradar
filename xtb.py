"""Which stocks / coins can be bought on XTB, with a link to the instrument page.

XTB has no public instrument API, but its sitemap lists every instrument page:
/pl/akcje/<slug> (real shares), /pl/akcje-cfd/<slug> (CFD), /pl/krypto/<slug>.
Slugs are either "<ticker>-us" or a company-name slug ("snowflake"), so a ticker is
matched by trying candidate slugs from the sitemap; name-based guesses are confirmed
by finding "<TICKER>.US" on the page. Results are cached, so each ticker costs at most
a few requests once.
"""

from __future__ import annotations

import re
import time
import unicodedata

import requests

BASE = "https://www.xtb.com/pl"
SITEMAPS = {
    "stock": f"{BASE}/sitemap/instruments/cashstocks/1.xml",
    "cfd": f"{BASE}/sitemap/instruments/shares/1.xml",
    "crypto": f"{BASE}/sitemap/instruments/crypto-cfd/1.xml",
}
PATHS = {"stock": "akcje", "cfd": "akcje-cfd", "crypto": "krypto"}
LABELS = {"stock": "XTB akcje", "cfd": "XTB CFD", "crypto": "XTB CFD"}
NAME_SUFFIXES = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group|"
                           r"sa|ag|nv|se|lp|llc|trust|class [a-z]|the|ordinary shares|common stock|adr|"
                           r"therapeutics|technologies|pharmaceuticals)\b\.?", re.I)
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def url_for(kind: str, slug: str) -> str:
    return f"{BASE}/{PATHS[kind]}/{slug}"


class XtbClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        self._last = 0.0

    def _get(self, url: str) -> requests.Response:
        wait = 0.5 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, timeout=30)
        self._last = time.monotonic()
        return resp

    def catalog(self, kind: str) -> set[str]:
        resp = self._get(SITEMAPS[kind])
        resp.raise_for_status()
        prefix = f"{BASE}/{PATHS[kind]}/"
        return {u[len(prefix):] for u in re.findall(r"<loc>([^<]+)</loc>", resp.text) if u.startswith(prefix)}

    def page_is(self, kind: str, slug: str, ticker: str) -> bool:
        """The page's own instrument appears as "TICKER.US" (XTB sometimes adds a digit: Sea = "SE1.US");
        tickers in the "popular stocks" widgets are quoted differently, so they do not match."""
        resp = self._get(url_for(kind, slug))
        return resp.status_code == 200 and re.search(rf'"{re.escape(ticker.upper())}\d?\.US"', resp.text) is not None


def name_slugs(issuer: str, catalog: set[str] = frozenset()) -> list[str]:
    """Candidate slugs, most specific first: the full name ("adaptive-biotechnologies-corp"),
    the name without legal suffixes ("snowflake", "arista-networks", +"-us"), and catalog
    slugs that start with the first word ("vistra" -> "vistra-energy")."""
    full = slugify(issuer)
    cleaned = slugify(NAME_SUFFIXES.sub(" ", issuer))
    words = [w for w in cleaned.split("-") if w]
    out = [full, f"{full}-cfd"]
    for s in ("-".join(words), "-".join(words[:2]), words[0] if words else ""):
        if s and len(s) > 2:
            out += [s, f"{s}-us"]
    if words and len(words[0]) > 2:
        out += sorted((c for c in catalog if c.startswith(words[0] + "-")), key=len)[:3]
    return list(dict.fromkeys(out))


def resolve_stock(client: XtbClient, ticker: str, issuer: str, catalogs: dict[str, set[str]],
                  max_checks: int = 5) -> dict[str, str | None]:
    """{'stock': url | None, 'cfd': url | None} for a US ticker."""
    t = ticker.lower().replace(".", "-")
    out: dict[str, str | None] = {}
    for kind in ("stock", "cfd"):
        cat = catalogs.get(kind, set())
        out[kind] = None
        if f"{t}-us" in cat:                       # ticker slug: unambiguous
            out[kind] = url_for(kind, f"{t}-us")
            continue
        checks = 0
        for slug in name_slugs(issuer, cat):
            if slug in cat and checks < max_checks:
                checks += 1
                try:
                    if client.page_is(kind, slug, ticker):
                        out[kind] = url_for(kind, slug)
                        break
                except requests.RequestException:
                    break
    return out


def crypto_url(coin_id: str, name: str, catalog: set[str]) -> str | None:
    """XTB lists ~45 coins as CFDs, named by slug (bitcoin, chainlink, ...)."""
    for slug in (coin_id, slugify(name), slugify(name).replace("-", "")):
        if slug and slug in catalog:
            return url_for("crypto", slug)
    return None

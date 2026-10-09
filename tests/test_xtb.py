import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp  # noqa: E402
import xtb  # noqa: E402
from tests.test_app import make_filing  # noqa: E402

CATALOGS = {
    "stock": {"prta-us", "snowflake", "apple-us", "apple-hospitality", "vistra-energy", "sea",
              "adaptive-biotechnologies-corp"},
    "cfd": {"snowflake", "apple-us"},
    "crypto": {"bitcoin", "chainlink", "bitcoincash"},
}
PAGES = {  # slug -> symbol the page is about (XTB quotes it as "TICKER.US")
    "snowflake": "SNOW", "apple-us": "AAPL", "apple-hospitality": "APLE", "vistra-energy": "VST",
    "sea": "SE1", "adaptive-biotechnologies-corp": "ADPT",
}


class FakeXtb:
    def __init__(self):
        self.fetched = []

    def catalog(self, kind):
        return set(CATALOGS[kind])

    def page_is(self, kind, slug, ticker):
        self.fetched.append(slug)
        import re
        return re.fullmatch(rf"{ticker.upper()}\d?", PAGES.get(slug, "")) is not None


@pytest.mark.parametrize("ticker, issuer, stock, cfd", [
    ("PRTA", "Prothena Corp plc", "akcje/prta-us", None),                   # ticker slug, no page fetch
    ("SNOW", "Snowflake Inc.", "akcje/snowflake", "akcje-cfd/snowflake"),    # name slug, verified
    ("AAPL", "Apple Inc.", "akcje/apple-us", "akcje-cfd/apple-us"),          # not apple-hospitality
    ("VST", "Vistra Corp.", "akcje/vistra-energy", None),                    # catalog prefix match
    ("SE", "Sea Ltd", "akcje/sea", None),                                    # XTB symbol SE1.US
    ("ADPT", "Adaptive Biotechnologies Corp", "akcje/adaptive-biotechnologies-corp", None),
    ("ZZZZ", "Nothing Here Inc", None, None),
])
def test_resolve_stock(ticker, issuer, stock, cfd):
    out = xtb.resolve_stock(FakeXtb(), ticker, issuer, CATALOGS)
    full = lambda path: path and f"{xtb.BASE}/{path}"  # noqa: E731
    assert out == {"stock": full(stock), "cfd": full(cfd)}


def test_ticker_slug_needs_no_page_fetch():
    client = FakeXtb()
    xtb.resolve_stock(client, "PRTA", "Prothena Corp plc", CATALOGS)
    assert client.fetched == []


def test_slugify_and_crypto_urls():
    assert xtb.slugify("Società Générale & Co.") == "societa-generale-co"
    assert xtb.crypto_url("chainlink", "Chainlink", CATALOGS["crypto"]).endswith("/krypto/chainlink")
    assert xtb.crypto_url("bitcoin-cash", "Bitcoin Cash", CATALOGS["crypto"]).endswith("/krypto/bitcoincash")
    assert xtb.crypto_url("starknet", "Starknet", CATALOGS["crypto"]) is None


def test_tracker_links_tickers_and_index_shows_badge_and_filter(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    store.add([make_filing("a-1", ticker="SNOW"), make_filing("b-1", ticker="ZZZZ", owner="Roe Jane")])
    tracker = webapp.Tracker(store, None, xtb_factory=FakeXtb)
    filings = [f for f in store.all() if webapp.tradeable(f)]
    tracker.refresh_xtb(filings, date(2026, 10, 9))
    links = store.xtb_links()
    assert links["SNOW"]["stock"] is None  # make_filing names every issuer "ACME CORP"
    assert store.xtb_catalog("crypto") == CATALOGS["crypto"]

    store.save_xtb_link("SNOW", {"stock": f"{xtb.BASE}/akcje/snowflake", "cfd": None})

    class Stub:
        running, last_run, last_new, last_error = False, "12:00:00", 0, None

    c = webapp.create_app(store=store, scanner=Stub(), start_background=False, crypto_scanner=Stub(),
                          whale_scanner=Stub()).test_client()
    html = c.get("/?min=0").get_data(as_text=True)
    assert f"{xtb.BASE}/akcje/snowflake" in html and "Tylko dostępne na XTB" in html
    rows = c.get("/api/filings?min=0&xtb=1").get_json()["rows"]
    assert [r["ticker"] for r in rows] == ["SNOW"]
    acc = rows[0]["accession"]
    assert "Kup akcje na XTB" in c.get(f"/filing/{acc}").get_data(as_text=True)


def test_missing_tickers_are_rechecked_after_two_weeks(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    store.add([make_filing("a-1", ticker="ZZZZ")])
    filings = store.all()
    calls = []

    class Counting(FakeXtb):
        def catalog(self, kind):
            return super().catalog(kind)

    def factory():
        calls.append(1)
        return Counting()

    tracker = webapp.Tracker(store, None, xtb_factory=factory)
    tracker.refresh_xtb(filings, date(2026, 10, 9))
    first = store.xtb_links()["ZZZZ"]["checked_at"]
    tracker.refresh_xtb(filings, date(2026, 10, 9))
    assert store.xtb_links()["ZZZZ"]["checked_at"] == first           # not re-resolved the same day

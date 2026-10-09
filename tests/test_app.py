import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp  # noqa: E402
import form4_scanner as fs  # noqa: E402
from tests.test_form4_scanner import form4  # noqa: E402


def make_filing(acc, ticker="ACME", owner="Doe John", shares="10000", price="25", **kw):
    xml = form4(shares=shares, price=price, ticker=ticker, **kw).replace("Doe John", owner)
    return fs.parse_form4(xml, acc)


@pytest.fixture
def store(tmp_path):
    s = webapp.Store(tmp_path / "t.db")
    s.add([
        make_filing("a-1", ticker="ACME", owner="Doe John"),                 # $250k
        make_filing("a-2", ticker="ACME", owner="Roe Jane", shares="8000"),  # $200k -> cluster
        make_filing("b-1", ticker="BETA", owner="Big Fund", shares="2000000", price="2.5",
                    officer="0", director="1", title=""),                    # $5M
        make_filing("c-1", ticker="CETA", code="S"),                         # sale
    ])
    return s


class StubScanner:
    running = False
    last_run = "12:00:00"
    last_new = 4
    last_error = None

    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1


@pytest.fixture
def client(store):
    scanner = StubScanner()
    app = webapp.create_app(store=store, scanner=scanner, start_background=False)
    app.testing = True
    c = app.test_client()
    c.scanner = scanner
    return c


def test_store_roundtrip_keeps_transactions(store):
    f = {x.accession: x for x in store.all()}["a-1"]
    assert f.total_value("P") == 250_000 and f.issuer_cik == "0000111111"
    assert store.accessions() == {"a-1", "a-2", "b-1", "c-1"}


def test_store_ignores_duplicates(store):
    store.add([make_filing("a-1", shares="1")])
    assert len(store.all()) == 4


def test_edgar_url_strips_leading_zeros():
    f = make_filing("0001193125-26-418446")
    assert f.edgar_url == ("https://www.sec.gov/Archives/edgar/data/111111/"
                           "000119312526418446/0001193125-26-418446-index.htm")


def test_api_default_filters_buys_and_marks_cluster(client):
    data = client.get("/api/filings").get_json()
    assert [r["ticker"] for r in data["rows"]] == ["BETA", "ACME", "ACME"]
    assert data["summary"]["clusters"] == ["ACME"]
    assert {r["ticker"]: r["cluster"] for r in data["rows"]}["ACME"] == 2


@pytest.mark.parametrize("query, tickers", [
    ("max=1000000", ["ACME", "ACME"]),           # drops the $5M outlier
    ("min=210%2C000", ["BETA", "ACME"]),         # comma-formatted input
    ("q=roe", ["ACME"]),                         # owner search
    ("code=S&min=0", ["CETA"]),
    ("sort=date", ["ACME", "ACME", "BETA"]),     # all same date; stable order is fine
])
def test_api_filters(client, query, tickers):
    rows = client.get(f"/api/filings?{query}").get_json()["rows"]
    assert sorted(r["ticker"] for r in rows) == sorted(tickers)


def test_bad_numbers_are_ignored(client):
    rows = client.get("/api/filings?min=abc&max=xyz").get_json()["rows"]
    assert len(rows) == 3


def test_index_renders_rows_and_status(client):
    html = client.get("/?min=0").get_data(as_text=True)
    assert "BETA" in html and "2 insiderów" in html and "EDGAR" in html
    assert "12:00:00" in html and "4 zgłoszeń w bazie" in html


def test_index_empty_message(client):
    html = client.get("/?q=nothing").get_data(as_text=True)
    assert "Brak transakcji" in html


def test_scan_now_triggers_and_keeps_filters(client):
    resp = client.post("/scan?min=500")
    assert client.scanner.triggered == 1
    assert resp.status_code == 302 and "min=500" in resp.headers["Location"]


def test_scanner_run_once_stores_and_reports_errors(tmp_path):
    store = webapp.Store(tmp_path / "s.db")

    class Client:
        def get(self, url, **params):
            if "browse-edgar" in url:
                from tests.test_form4_scanner import FEED
                return FEED
            if "111111" in url:
                return f"<XML>{form4()}</XML>"
            raise fs.requests.HTTPError("404")

    sc = webapp.Scanner(store, Client, pages=1)
    sc.run_once()
    assert store.accessions() == {"0002222222-26-000001"} and sc.last_new == 1
    assert sc.last_error is None and not sc.running

    def boom():
        raise RuntimeError("network down")
    bad = webapp.Scanner(store, boom, pages=1)
    bad.run_once()
    assert bad.last_error == "RuntimeError: network down" and not bad.running

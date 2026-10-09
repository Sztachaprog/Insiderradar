import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp  # noqa: E402
import market  # noqa: E402
import signals  # noqa: E402
from tests.test_app import make_filing  # noqa: E402
from tests.test_form4_scanner import form4  # noqa: E402
import form4_scanner as fs  # noqa: E402

CLOSES = [("2026-10-01", 10.0), ("2026-10-02", 10.5), ("2026-10-05", 11.0), ("2026-10-06", 9.0),
          ("2026-10-07", 12.0), ("2026-10-08", 12.5)]
SPY = [(d, 100.0 + i) for i, (d, _) in enumerate(CLOSES)]


def test_price_on_uses_last_session_on_or_before():
    assert market.price_on(CLOSES, "2026-10-04") == (1, 10.5)  # weekend -> Friday
    assert market.price_on(CLOSES, "2026-09-01") is None


def test_performance_horizons_and_alpha():
    p = market.performance(CLOSES, "2026-10-02", 10.0, SPY)
    assert p["now"] == pytest.approx(0.25)
    assert p["h"][1]["ret"] == pytest.approx(0.10)          # first session after base
    assert p["h"][3]["ret"] == pytest.approx(0.20)
    assert 5 not in p["h"]                                   # not enough sessions yet
    assert p["alpha_now"] == pytest.approx(0.25 - (105 / 101 - 1))
    assert p["path"][0] == ("2026-10-02", 0.0) and len(p["path"]) == 5


def test_summarize_win_rate_and_average_path():
    a = market.performance(CLOSES, "2026-10-02", 10.0)       # +25%
    b = market.performance(CLOSES, "2026-10-02", 15.0)       # -16.7%
    now = market.summarize([a, b])[0]
    assert now["n"] == 2 and now["win_rate"] == 0.5
    assert now["avg_win"] == pytest.approx(0.25) and now["avg_loss"] == pytest.approx(12.5 / 15 - 1)
    path = market.average_path([a, b])
    assert path[0] == 0 and len(path) == 5


def row(**kw):
    base = {"code": "P", "role": "Chief Executive Officer", "owner": "Doe John", "value": 500_000,
            "avg_price": 10.0, "cluster": 1, "plan": False}
    return {**base, **kw}


def test_signal_ceo_cluster_is_strong():
    s = signals.analyze(row(cluster=3), "", market_cap=50e6, price_now=9.5)
    assert s["kind"] == "insider" and s["level"] == "strong"


def test_signal_detects_placement_and_fund():
    s = signals.analyze(row(), "entered into a share purchase agreement with the Issuer", None, None)
    assert s["kind"] == "placement" and s["level"] == "weak"
    assert signals.analyze(row(owner="L1 Capital Pty Ltd"), "", None, None)["kind"] == "fund"
    assert signals.analyze(row(code="S"), "", None, None)["score"] is None


def test_footnotes_are_parsed():
    xml = form4().replace("</ownershipDocument>",
                          '<footnotes><footnote id="F1">Private placement.</footnote></footnotes>'
                          "<remarks>See note.</remarks></ownershipDocument>")
    assert fs.parse_form4(xml, "x").footnotes == "Private placement. See note."


class FakeYahoo:
    def __init__(self):
        self.profiles = 0

    def chart(self, ticker, range_="6mo"):
        if ticker == "BAD":
            raise RuntimeError("404")
        return {"price": 13.0, "name": ticker, "closes": SPY if ticker == "SPY" else CLOSES}

    def profile(self, ticker):
        self.profiles += 1
        return {"sector": "Tech", "market_cap": 1e9, "summary": "Makes things."}


def test_tracker_snapshots_live_price_and_profiles(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    store.add([make_filing("a-1"), make_filing("x-1", ticker="BAD")])
    yahoo = FakeYahoo()
    tracker = webapp.Tracker(store, lambda: yahoo)
    tracker.refresh(today=date.today())

    assert store.tracked() == {"a-1": ("ACME", date.today().isoformat(), 13.0)}
    assert store.companies()["ACME"]["sector"] == "Tech" and yahoo.profiles == 1
    assert {"ACME", "SPY"} <= set(store.prices())
    assert tracker.last_error == "brak cen dla 1 tickerów"

    tracker.refresh(today=date.today())  # second run: no duplicate tracking, no profile refetch
    assert len(store.tracked()) == 1 and yahoo.profiles == 1


def test_tracker_backfills_old_filings_from_history(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    store.add([make_filing("a-1")])
    with store._conn() as c:
        c.execute("UPDATE filings SET scanned_at = '2026-10-05T10:00:00'")
    webapp.Tracker(store, FakeYahoo).refresh(today=date(2026, 10, 9))
    assert store.tracked()["a-1"] == ("ACME", "2026-10-05", 11.0)


@pytest.fixture
def tracked_client(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    store.add([make_filing("a-1"), make_filing("b-1", ticker="BETA", owner="Big Fund LLC")])
    store.track("a-1", "ACME", "2026-10-02", 10.0)
    store.save_prices("ACME", CLOSES)
    store.save_prices("SPY", SPY)
    store.save_company("ACME", {"name": "Acme Corp", "sector": "Tech", "summary": "Makes anvils.",
                                "market_cap": 100e6})

    class Stub:
        running, last_run, last_new, last_error = False, "12:00:00", 0, None

    app = webapp.create_app(store=store, scanner=Stub(), start_background=False)
    app.testing = True
    return app.test_client()


def test_api_rows_carry_perf_signal_and_company(tracked_client):
    rows = {r["ticker"]: r for r in tracked_client.get("/api/filings").get_json()["rows"]}
    assert rows["ACME"]["perf"]["now"] == pytest.approx(0.25)
    assert rows["ACME"]["sector"] == "Tech" and rows["ACME"]["pct_mcap"] == pytest.approx(0.0025)
    assert rows["BETA"]["perf"] is None and rows["BETA"]["signal"]["kind"] == "fund"


def test_signal_and_kind_filters(tracked_client):
    rows = tracked_client.get("/api/filings?kind=insider").get_json()["rows"]
    assert [r["ticker"] for r in rows] == ["ACME"]


def test_pages_render(tracked_client):
    html = tracked_client.get("/").get_data(as_text=True)
    assert "+25.0%" in html and "Tech" in html
    detail = tracked_client.get("/filing/a-1").get_data(as_text=True)
    assert "Makes anvils." in detail and "Zakup insidera na rynku" in detail
    perf = tracked_client.get("/wyniki").get_data(as_text=True)
    assert "Wyniki według horyzontu" in perf and "100%" in perf  # 1 of 1 tracked is a win
    assert tracked_client.get("/filing/nope").status_code == 404

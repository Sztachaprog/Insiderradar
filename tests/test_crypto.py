import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp  # noqa: E402
import calibration  # noqa: E402
import crypto  # noqa: E402
import history  # noqa: E402
import market  # noqa: E402
import signals  # noqa: E402
from tests.test_form4_scanner import form4  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def coin(cid="moon", price=1.0, mcap=50e6, vol=40e6, ch1=2, ch24=40, ch7=60, **kw):
    return {"id": cid, "symbol": cid[:4], "name": cid.title(), "image": "", "current_price": price,
            "market_cap": mcap, "market_cap_rank": 400, "fully_diluted_valuation": mcap * 1.1,
            "total_volume": vol, "ath": 2.0, "ath_change_percentage": -50,
            "price_change_percentage_1h_in_currency": ch1, "price_change_percentage_24h_in_currency": ch24,
            "price_change_percentage_7d_in_currency": ch7, **kw}


# ---------- detection & scoring ----------

@pytest.mark.parametrize("kw, expected", [
    ({}, ["pump24", "volume"]),
    ({"ch24": -30, "vol": 1e6}, ["dump24"]),
    ({"ch24": 5, "ch1": 12, "vol": 1e6}, ["move1h"]),
    ({"mcap": 5e9}, []),                                  # too big for the radar
    ({"vol": 50_000}, []),                                # illiquid
    ({"name": "Wrapped Moon"}, []),
    ({"cid": "nvidia-robinhood-tokenized-stock"}, []),
    ({"price": 1.0, "ch24": 0.1, "ch1": 0, "vol": 1e6}, []),  # quiet stablecoin-like
])
def test_triggers(kw, expected):
    assert crypto.triggers_for(coin(**kw), trending=set()) == expected


def test_trending_alone_is_a_trigger():
    assert crypto.triggers_for(coin(ch24=3, ch1=0, vol=1e6), {"moon"}) == ["trending"]


def event(triggers=("pump24", "volume"), **kw):
    c = coin(**kw)
    return {"id": 1, "coin_id": c["id"], "symbol": "MOON", "name": "Moon", "image": "",
            "detected_at": crypto.iso(T0), "base_price": c["current_price"], "triggers": list(triggers),
            "metrics": {k: c.get(k) for k in c}}


def test_analyze_rewards_volume_pump_and_project_quality():
    details = {"github": ["https://github.com/x"], "watchlist": 80_000, "categories": ["Artificial Intelligence (AI)"]}
    s = crypto.analyze(event(), details)
    assert s["kind"] == "pump" and s["level"] == "strong"
    assert {"vol_spike", "pump24", "trend7d", "has_github", "community", "narrative"} <= set(s["factors"])


def test_analyze_penalises_parabola_meme_and_unlocks():
    e = event(ch24=150, fully_diluted_valuation=500e6)
    e["metrics"]["fully_diluted_valuation"] = 500e6
    s = crypto.analyze(e, {"categories": ["Meme"]})
    assert {"parabolic", "meme", "low_float"} <= set(s["factors"])
    assert s["score"] < crypto.analyze(event(), None)["score"] - 20


def test_weights_override_changes_score():
    base = crypto.analyze(event(), None)["score"]
    assert crypto.analyze(event(), None, {"vol_spike": 0})["score"] == base - 12


# ---------- performance ----------

def pts(*pairs):
    return [(crypto.iso(T0 + timedelta(hours=h)), p) for h, p in pairs]


def test_performance_horizons_gaps_and_btc_alpha():
    e = event()
    points = pts((0, 1.0), (1, 1.1), (24, 1.5), (30, 1.2))
    btc = pts((0, 100), (1, 100), (24, 110), (30, 110))
    p = crypto.performance(e, points, btc, now=T0 + timedelta(hours=100))
    assert p["now"] == pytest.approx(0.2) and p["alpha_now"] == pytest.approx(0.2 - 0.1)
    assert p["h"][1]["ret"] == pytest.approx(0.1) and p["h"][24]["alpha"] == pytest.approx(0.4)
    assert 72 in p["missing"] and 168 not in p["h"]       # app was off around +72h; +168h not reached


def test_summarize_and_average_path():
    a = crypto.performance(event(), pts((0, 1.0), (6, 1.2), (12, 1.4)), None, now=T0 + timedelta(hours=12))
    b = crypto.performance(event(), pts((0, 1.0), (6, 0.8), (12, 0.9)), None, now=T0 + timedelta(hours=12))
    now = crypto.summarize([a, b])[0]
    assert now["n"] == 2 and now["win_rate"] == 0.5
    path = crypto.average_path([a, b])
    assert path[0] == (0, 0.0) and path[1][1] == pytest.approx(0.0) and path[2][1] == pytest.approx(0.15)


# ---------- runner ----------

class FakeGecko:
    def __init__(self, price=1.0):
        self.price = price
        self.calls = []

    def markets(self, page):
        self.calls.append(("markets", page))
        return [coin(price=self.price), coin("bitcoin", price=60000, mcap=1e12, ch24=1),
                coin("calm", ch24=1, ch1=0, vol=1e6)] if page == 1 else []

    def trending(self):
        return set()

    def coin(self, cid):
        self.calls.append(("coin", cid))
        return {"description": "To the moon.", "categories": [], "github": []}

    def simple_price(self, ids):
        return {i: 0.5 for i in ids}

    def chart(self, cid, days):
        self.calls.append(("chart", cid))
        return []


def test_scanner_records_event_once_and_snapshots(tmp_path):
    store = crypto.CryptoStore(tmp_path / "c.db")
    g = FakeGecko()
    sc = crypto.CryptoScanner(store, lambda: g, pages=2)
    sc.run_once(now=T0)
    sc.run_once(now=T0 + timedelta(hours=1))                 # same coin within 72h -> no new event
    events = store.events()
    assert [e["coin_id"] for e in events] == ["moon"] and sc.last_new == 0 and sc.last_error is None
    assert set(store.prices()) == {"moon", "bitcoin"} and len(store.prices()["moon"]) == 2
    assert store.coins()["moon"]["description"] == "To the moon."
    assert ("coin", "moon") in g.calls and g.calls.count(("coin", "moon")) == 1


def test_scanner_reports_errors(tmp_path):
    def boom():
        raise RuntimeError("429")
    sc = crypto.CryptoScanner(crypto.CryptoStore(tmp_path / "c.db"), boom)
    sc.run_once()
    assert sc.last_error == "RuntimeError: 429" and not sc.running


# ---------- web ----------

@pytest.fixture
def client(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    cstore = crypto.CryptoStore(tmp_path / "t.db")
    now = crypto.utcnow()
    cstore.add_event(coin(price=1.0), ["pump24", "volume"], now - timedelta(hours=30))
    cstore.add_event(coin("rug", price=2.0, ch24=-40), ["dump24"], now - timedelta(hours=30))
    for cid, ps in {"moon": [1.0, 1.3, 1.5], "rug": [2.0, 1.5, 1.0], "bitcoin": [100, 101, 102]}.items():
        cstore.save_prices(cid, [(crypto.iso(now - timedelta(hours=h)), p) for h, p in zip([30, 6, 0], ps)])
    cstore.save_coin("moon", {"description": "Moon protocol.", "categories": ["DeFi"], "github": ["g"]})

    class Stub:
        running, last_run, last_new, last_error = False, "12:00:00", 0, None

        def trigger(self):
            self.triggered = True

    app = webapp.create_app(store=store, scanner=Stub(), start_background=False,
                            crypto_store=cstore, crypto_scanner=Stub())
    app.testing = True
    return app.test_client()


def test_crypto_api_and_filters(client):
    rows = client.get("/api/crypto").get_json()["rows"]
    assert {r["symbol"] for r in rows} == {"MOON", "RUG"}
    moon = next(r for r in rows if r["symbol"] == "MOON")
    assert moon["perf"]["now"] == pytest.approx(0.5) and moon["signal"]["kind"] == "pump"
    assert [r["symbol"] for r in client.get("/api/crypto?trigger=dump24").get_json()["rows"]] == ["RUG"]


def test_crypto_pages_render(client):
    assert "MOON" in client.get("/krypto").get_data(as_text=True)
    perf = client.get("/krypto/wyniki?h=now").get_data(as_text=True)
    assert "Kalibracja oceny" in perf and "trafność kierunku" in perf
    eid = client.get("/api/crypto?q=moon").get_json()["rows"][0]["id"]
    detail = client.get(f"/krypto/{eid}").get_data(as_text=True)
    assert "Moon protocol." in detail and "Pompa z wolumenem" in detail
    assert client.get("/krypto/999").status_code == 404


def test_calibration_reset_and_apply_guard(client, tmp_path):
    resp = client.post("/kalibracja/crypto/apply", data={"h": "now"})
    assert resp.status_code == 302                         # too few samples -> nothing applied
    assert crypto.CryptoStore(tmp_path / "t.db").weights() == {}
    assert client.post("/kalibracja/crypto/reset").status_code == 302
    assert client.post("/kalibracja/nope/apply").status_code == 404


# ---------- calibration ----------

def test_calibrate_finds_factor_that_works():
    items = [{"score": 70, "factors": ["good"], "ret": 0.10} for _ in range(20)] + \
            [{"score": 30, "factors": ["bad"], "ret": -0.05} for _ in range(20)]
    factors = {"good": (5, "Good"), "bad": (5, "Bad"), "rare": (5, "Rare")}
    cal = calibration.calibrate(items, factors, {})
    by = {f["key"]: f for f in cal["factors"]}
    assert by["good"]["verdict"] == "pomaga" and by["good"]["suggested"] > 5
    assert by["bad"]["verdict"] == "szkodzi" and by["bad"]["suggested"] < 5
    assert by["rare"]["verdict"] == "za mało danych" and cal["can_apply"]
    assert cal["spearman"] == pytest.approx(1.0) and cal["buckets"][3]["avg"] == pytest.approx(0.10)


def test_weight_storage_roundtrip(tmp_path):
    store = webapp.Store(tmp_path / "w.db")
    store.set_weights({"cluster2": 25})
    assert store.weights() == {"cluster2": 25.0}
    store.set_weights(None)
    assert store.weights() == {}


# ---------- stocks: context & insider history ----------

def test_price_context_drawdown_and_low():
    closes = [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 100 - i) for i in range(60)]
    c = market.price_context(closes, closes[-1][0])
    assert c["from_high"] == pytest.approx(41 / 100 - 1) and c["from_low"] == 0 and c["range_pos"] == 0
    assert c["month_before"] == pytest.approx(41 / 62 - 1)
    assert market.price_context(closes[:10], closes[9][0]) is None


def test_signal_uses_context_and_history():
    row = {"code": "P", "role": "Director", "owner": "Doe John", "value": 300_000, "avg_price": 10,
           "cluster": 1, "plan": False}
    ctx = {"from_high": -0.45, "from_low": 0.05, "month_before": -0.2}
    good = history.summarize([{"accession": "x", "ticker": "A", "date": "2025-01-01", "value": 1,
                               "ret": {"63": 0.3}}, {"accession": "y", "ticker": "A", "date": "2025-02-01",
                                                     "value": 1, "ret": {"63": 0.2}}])
    s = signals.analyze(row, "", None, None, context=ctx, history=good)
    assert {"after_drop", "near_low", "history_good"} <= set(s["factors"])


class FakeSec:
    def __init__(self):
        self.urls = []

    def get(self, url, **params):
        self.urls.append(url)
        if "submissions" in url:
            return json.dumps({"filings": {"recent": {
                "form": ["4", "4", "3"], "accessionNumber": ["0000000001-25-000001", "0000000001-25-000002", "z"],
                "filingDate": ["2026-03-01", "2026-02-01", "2026-01-01"]}}})
        code = "P" if url.endswith("000001.txt") else "S"
        return f"<XML>{form4(code=code)}</XML>"


class FakeYahoo:
    def chart(self, ticker, range_="6mo"):
        closes = [(f"2026-{m:02d}-{d:02d}", 10 + m + d / 100) for m in range(1, 13) for d in range(1, 29)]
        return {"price": 20, "name": ticker, "closes": closes}


def test_history_lookup_and_for_filing():
    data = history.lookup(FakeSec(), FakeYahoo(), "2222222")
    assert data["count"] == 1 and data["buys"][0]["ticker"] == "ACME"
    assert data["buys"][0]["ret"]["21"] > 0                       # rising fake series
    assert history.for_filing(data, "other", "2026-12-01")["summary"][21]["n"] == 1
    assert history.for_filing(data, "other", "2026-01-01") is None   # only buys *before* the filing count

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import crypto  # noqa: E402
import portfolio  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
NOW = crypto.iso(T0 + timedelta(days=40))


def at(h):
    return crypto.iso(T0 + timedelta(hours=h))


def trade(b, h_in, h_out, ret, sym="X", src="s", sl=None, score=50):
    return {"b": b, "src": src, "name": src, "sym": sym, "in": at(h_in), "out": at(h_out) if h_out is not None else None,
            "ret": ret, "sl": sl, "score": score, "url": ""}


def test_leveraged_trade_is_sized_by_risk_and_capped_at_the_margin():
    trades = [trade("lev", 0, 5, -0.105, sym="A", sl=0.10), trade("lev", 10, 20, -0.5, sym="B", sl=0.10)]
    r = portfolio.simulate(trades, {"capital": 10000}, NOW)
    lev = r["buckets"]["lev"]                     # 30% of 10,000 = 3,000; one stop costs 2% of it
    a, b = r["closed"]
    assert a["size"] == pytest.approx(600) and a["margin"] == pytest.approx(200) and a["pnl"] == pytest.approx(-63)
    assert b["pnl"] == pytest.approx(-b["margin"])                  # a gap past liquidation loses the margin only
    assert lev["equity"] == pytest.approx(3000 - 63 - b["margin"]) and lev["trades"] == 2


def test_one_leveraged_position_per_token_and_slots_for_stocks():
    trades = [trade("lev", 0, 10, 0.1, sym="A", src="s1", sl=0.05), trade("lev", 1, 10, 0.1, sym="A", src="s2", sl=0.05),
              *[trade("stocks", i, None, 0.05, sym=f"S{i}") for i in range(12)]]
    r = portfolio.simulate(trades, {"capital": 10000}, NOW)
    assert r["buckets"]["lev"]["skipped"] == 1                       # same token from a second strategy
    st = r["buckets"]["stocks"]                                      # 10 slots of 600 each, 2 signals skipped
    assert st["open"] == 10 and st["skipped"] == 2 and st["unrealized"] == pytest.approx(10 * 600 * 0.05)


def test_settings_filter_what_is_traded():
    trades = [trade("spot", 0, 5, 0.1, src="whales", score=80), trade("spot", 1, 5, 0.1, src="radar", score=80),
              trade("spot", 2, 5, 0.1, src="whales", score=10), trade("lev", 3, 5, 0.1, src="keep", sl=0.05),
              trade("lev", 4, 6, 0.1, src="drop", sl=0.05, sym="Y")]
    r = portfolio.simulate(trades, {"spotSources": ["whales"], "minScore": {"stocks": 0, "spot": 50},
                                    "strategies": ["keep"], "start": at(0)}, NOW)
    assert r["buckets"]["spot"]["trades"] == 1 and r["buckets"]["lev"]["trades"] == 1


def test_collect_maps_every_source():
    path = [(f"2026-09-{d:02d}", d / 100) for d in range(1, 26)]
    stock = {"ticker": "ACME", "base_date": "2026-09-01", "perf": {"now": 0.3, "path": path},
             "signal": {"score": 70}, "url": "u"}
    radar = {"symbol": "MOON", "detected_at": at(0), "origin": "live", "signal": {"score": 60},
             "perf": {"now": 0.2, "h": {168: {"ret": 0.1}}}}
    whale = {"symbol": "WH", "score": 40, "signal": {"flagged_at": at(0)}, "perf": {"now": 0.05, "h": {}}}
    row = {"symbol": "LEV", "origin": "live", "signal": {"score": 55}}
    lev = [{"row": row, "net": 0.02, "status": "closed", "entry_at": at(5), "exit_at": at(9), "strategy_key": "c1",
            "strategy_name": "Mine", "full": {"sl": 0.07}},
           {"row": row, "net": 0.02, "status": "closed", "entry_at": at(1), "exit_at": at(2), "strategy_key": "c1",
            "strategy_name": "Mine", "full": {"sl": 0.07}},                       # before the strategy existed
           {"row": {**row, "origin": "backfill"}, "net": 0.02, "status": "closed", "entry_at": at(6),
            "exit_at": at(7), "strategy_key": "p", "strategy_name": "P", "full": {"sl": 0.05}}]
    out = portfolio.collect([stock], [radar], [whale], lev, {"c1": at(3)}, T0)
    by = {t["sym"]: t for t in out}
    assert set(by) == {"ACME", "MOON", "WH", "LEV"}
    assert by["ACME"]["out"] == path[20][0] + portfolio.US_CLOSE and by["ACME"]["ret"] == pytest.approx(path[20][1] - 0.002)
    assert by["MOON"]["ret"] == pytest.approx(0.1 - 0.002) and by["MOON"]["out"]
    assert by["WH"]["out"] is None and by["LEV"]["sl"] == 0.07 and by["LEV"]["in"] == at(5)


def random_trades(seed):
    rnd = random.Random(seed)
    out = []
    for i in range(160):
        b = rnd.choice(portfolio.BUCKETS)
        h = rnd.uniform(0, 600)
        closed = rnd.random() < 0.7
        out.append(trade(b, h, h + rnd.uniform(1, 200) if closed else None, rnd.gauss(0.005, 0.08),
                         sym=rnd.choice("ABCDEFGH"), src=rnd.choice(["s1", "s2", "whales", "radar"]),
                         sl=rnd.choice([0.05, 0.07, 0.1, 0.2]) if b == "lev" else None, score=rnd.randint(0, 100)))
    out.sort(key=lambda t: (t["in"], t["b"], t["src"], t["sym"]))
    return out


@pytest.mark.parametrize("seed", range(30))
def test_browser_simulation_matches_python(seed):
    mini_racer = pytest.importorskip("py_mini_racer")
    ctx = mini_racer.MiniRacer()
    ctx.eval((ROOT / "static" / "portfolio.js").read_text(encoding="utf-8"))
    rnd = random.Random(seed)
    settings = {"capital": rnd.choice([1000, 10000, 55555]), "risk": rnd.choice([1, 2, 5]), "lev": rnd.choice([1, 2, 3]),
                "split": {"stocks": 50, "lev": 30, "spot": 20}, "maxPos": {"stocks": rnd.randint(1, 8), "spot": 3},
                "minScore": {"stocks": rnd.choice([0, 40]), "spot": 0},
                "strategies": rnd.choice([None, ["s1"]]), "spotSources": rnd.choice([["whales", "radar"], ["radar"]]),
                "start": at(rnd.choice([0, 100]))}
    trades = random_trades(seed)
    py = portfolio.simulate(trades, settings, NOW)
    js = json.loads(ctx.eval(f"JSON.stringify(simulatePortfolio({json.dumps(trades)}, {json.dumps(settings)}, "
                             f"{json.dumps(NOW)}, {json.dumps(portfolio.DEFAULTS)}))"))
    for b in portfolio.BUCKETS:
        for k, v in py["buckets"][b].items():
            if k == "curve":
                assert [[t, pytest.approx(x)] for t, x in v] == js["buckets"][b]["curve"]
            else:
                assert js["buckets"][b][k] == (pytest.approx(v) if v is not None else None), (b, k)
    assert [(p["sym"], p["in"]) for p in py["closed"]] == [(p["sym"], p["in"]) for p in js["closed"]]
    assert [p["pnl"] for p in js["open"]] == pytest.approx([p["pnl"] for p in py["open"]])

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crypto  # noqa: E402
import search  # noqa: E402
import strategy  # noqa: E402

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def row(i=1, at=T0, triggers=("pump24",), score=60, mcap=50e6, ch24=0.3, vol=1.0, base=1.0):
    return {"id": i, "coin_id": f"c{i}", "symbol": "X", "detected_at": crypto.iso(at), "base_price": base,
            "triggers": list(triggers), "signal": {"score": score}, "mcap": mcap, "ch24": ch24, "vol_ratio": vol}


def random_walk(seed, start=T0, minutes=60 * 50, step=1, gap_every=0):
    rnd, price, bars = random.Random(seed), 1.0, []
    for m in range(1, minutes, step):
        if gap_every and m % gap_every == 0:
            continue                                   # holes in the data, like real minute candles
        o = price * (1 + rnd.gauss(0, 0.004))
        c = o * (1 + rnd.gauss(0, 0.006))
        h, low = max(o, c) * (1 + abs(rnd.gauss(0, 0.004))), min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
        bars.append((crypto.iso(start + timedelta(minutes=m)), o, h, low, c))
        price = c
    return bars


@pytest.mark.parametrize("seed", range(12))
def test_fast_outcomes_equal_the_bar_by_bar_simulation(seed):
    r = row()
    bars = random_walk(seed, step=1 if seed % 3 else 7, gap_every=97 if seed % 2 else 0)
    now = T0 + timedelta(days=5)
    p = search.Path(r, bars)
    for side, tp, sl, h in search.EXITS:
        params = {"side": side, "tp": tp, "sl": sl, "hold_h": h}
        t = strategy.simulate(r, bars, params, now, fee=0.005)
        fast = p.outcome(side, tp, sl, h, now, 0.005)
        if t["status"] == "closed":
            assert fast == pytest.approx(t["net"]), (side, tp, sl, h)
        else:
            assert fast is None, (side, tp, sl, h, t["status"])


def test_open_trade_has_no_outcome_yet():
    bars = random_walk(1, minutes=60)
    p = search.Path(row(), bars)
    assert p.outcome("long", 0.5, 0.5, 48, T0 + timedelta(hours=1), 0) is None


def test_search_finds_a_real_edge_and_checks_it_on_newer_signals():
    rows, paths = [], {}
    for i in range(120):                      # dumps that always bounce +12% within an hour, pumps that fade
        at = T0 + timedelta(hours=6 * i)
        dump = i % 2 == 0
        r = row(i, at, triggers=("dump24",) if dump else ("pump24",), ch24=-0.3 if dump else 0.3)
        b0 = crypto.iso(at + timedelta(minutes=30))
        paths[i] = [(b0, 1.0, 1.12, 0.99, 1.11)] if dump else [(b0, 1.0, 1.01, 0.97, 0.98)]
        paths[i] += [(crypto.iso(at + timedelta(hours=h)), 1.0, 1.0, 1.0, 1.0) for h in range(1, 61)]
        rows.append(r)
    res = search.run(rows, paths, T0 + timedelta(days=60))
    assert res["train"] and res["test"] and res["split_at"]
    best = res["rules"][0]
    assert best["passed"] and best["train"]["avg"] > 0.05 and best["test"]["avg"] > 0.05
    assert strategy.matches(rows[0], best["params"]) and not strategy.matches(rows[1], best["params"])  # dumps only


def test_passing_rules_are_tracked_live_once(tmp_path):
    cstore = crypto.CryptoStore(tmp_path / "c.db")
    rule = {"params": {"triggers": ["dump24"], "tp": 0.1, "sl": 0.03, "hold_h": 4}, "passed": True}
    result = {"rules": [rule, {"params": {"triggers": ["pump24"]}, "passed": False}]}
    added = search.track_passing(result, cstore)
    assert len(added) == 1 and added[0].startswith(search.AUTO_PREFIX)
    assert search.track_passing(result, cstore) == []                 # already tracked
    store = search.SearchStore(tmp_path / "c.db")
    store.save({"rules": []})
    assert store.latest() == {"rules": []}


def test_auto_names_are_short_and_readable():
    p = {"triggers": ["move1h"], "side": "short", "min_mcap": 30e6, "max_mcap": 300e6, "min_score": 50}
    assert search.auto_name(p) == "Auto: short, Ruch 1h, 30–300 mln $"
    assert search.auto_name({}) == "Auto: long, każdy ruch"


def test_rule_whose_stop_gets_gapped_through_never_passes():
    rows, paths = [], {}
    for i in range(120):                      # +12% bounces, except one that gaps down 40% through any stop
        at = T0 + timedelta(hours=6 * i)
        r = row(i, at, triggers=("dump24",), ch24=-0.3)
        b0 = crypto.iso(at + timedelta(minutes=30))
        paths[i] = [(b0, 0.6, 0.6, 0.6, 0.6)] if i == 100 else [(b0, 1.0, 1.12, 0.99, 1.11)]
        paths[i] += [(crypto.iso(at + timedelta(hours=h)), 1.0, 1.0, 1.0, 1.0) for h in range(1, 61)]
        rows.append(r)
    res = search.run(rows, paths, T0 + timedelta(days=60))
    assert res["rules"] and not any(r["passed"] for r in res["rules"])
    assert all(not r["safe"] for r in res["rules"] if r["test"]["n"])

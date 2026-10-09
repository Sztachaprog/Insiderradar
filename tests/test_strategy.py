import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crypto  # noqa: E402
import strategy  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def row(score=60, mcap=20e6, ch24=0.05, vol=1.0, triggers=("volume",), base=1.0):
    return {"id": 1, "coin_id": "x", "symbol": "X", "detected_at": crypto.iso(T0), "base_price": base,
            "triggers": list(triggers), "signal": {"score": score}, "mcap": mcap, "ch24": ch24, "vol_ratio": vol}


def path(*pairs):
    return [(crypto.iso(T0 + timedelta(minutes=m)), p) for m, p in pairs]


LATER = T0 + timedelta(days=3)


def test_take_profit_fills_at_target_minus_fee():
    t = strategy.simulate(row(), path((15, 1.03), (30, 1.25)), {"tp": 0.10, "sl": 0.05}, LATER, fee=0.005)
    assert t["status"] == "closed" and t["reason"] == "take-profit"
    assert t["net"] == pytest.approx(0.10 - 0.005) and t["peak"] == pytest.approx(0.25)


def test_stop_loss_fills_at_observed_price():
    t = strategy.simulate(row(), path((15, 0.98), (30, 0.90)), {"sl": 0.05}, LATER, fee=0)
    assert t["reason"] == "stop-loss" and t["gross"] == pytest.approx(-0.10)


def test_time_exit_uses_snapshot_near_deadline():
    t = strategy.simulate(row(), path((60, 1.02), (23 * 60, 1.04), (25 * 60, 1.5)), {"hold_h": 24}, LATER, fee=0)
    assert t["reason"] == "czas" and t["gross"] == pytest.approx(0.04)


def test_gap_over_deadline_is_no_data_and_running_trade_is_open():
    gap = strategy.simulate(row(), path((60, 1.0), (40 * 60, 1.2)), {"hold_h": 24}, LATER)
    assert gap["status"] == "no_data"
    running = strategy.simulate(row(), path((60, 1.02)), {"hold_h": 24}, T0 + timedelta(hours=2), fee=0)
    assert running["status"] == "open" and running["net"] == pytest.approx(0.02)


@pytest.mark.parametrize("params, ok", [
    ({}, True),
    ({"triggers": ["pump24"]}, False),
    ({"exclude": ["volume"]}, False),
    ({"min_score": 70}, False),
    ({"max_mcap": 10e6}, False),
    ({"min_ch24": 0.10}, False),
    ({"max_vol": 0.5}, False),
])
def test_entry_filters(params, ok):
    assert strategy.matches(row(), params) is ok


def test_backtest_stats_and_equity_curve():
    rows = [dict(row(), id=1, coin_id="a"), dict(row(), id=2, coin_id="b"), dict(row(triggers=["pump24"]), coin_id="c")]
    prices = {"a": path((15, 1.2)), "b": path((15, 0.9))}
    b = strategy.backtest(rows, prices, {"triggers": ["volume"], "tp": 0.10, "sl": 0.05}, LATER, fee=0, stake=100)
    assert b["matched"] == 2 and b["closed"] == 2 and b["win_rate"] == 0.5
    assert b["pnl"] == pytest.approx(10 - 10) and b["max_dd"] == pytest.approx(-10)
    assert b["profit_factor"] == pytest.approx(1.0) and (b["tp_hits"], b["sl_hits"]) == (1, 1)


def test_form_roundtrip_in_human_units():
    form = MultiDict([("max_mcap", "30"), ("min_ch24", "-15"), ("tp", "8"), ("hold_h", "12"),
                      ("triggers", "volume"), ("triggers", "bogus"), ("max_vol", "abc")])
    p = strategy.params_from_form(form)
    assert p == {"max_mcap": 30e6, "min_ch24": pytest.approx(-0.15), "tp": pytest.approx(0.08),
                 "hold_h": 12, "triggers": ["volume"]}
    v = strategy.form_values(p)
    assert (v["max_mcap"], v["min_ch24"], v["tp"], v["min_mcap"]) == ("30", "-15", "8", "")
    assert "TP +8%" in " ".join(strategy.describe(p))


def test_local_time_is_cest():
    assert crypto.local("2026-10-09T15:43:53Z") == "2026-10-09 17:43 CEST"
    assert crypto.local("2026-11-09T15:43:53Z") == "2026-11-09 16:43 CET"

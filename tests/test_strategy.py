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


def candles(*bars):
    """(minute, open, high, low, close) -> candle path."""
    return [(crypto.iso(T0 + timedelta(minutes=m)), o, h, low, c) for m, o, h, low, c in bars]


SHORT = {"side": "short", "tp": 0.10, "sl": 0.05}


def test_short_takes_profit_when_the_price_falls():
    t = strategy.simulate(row(), candles((1, 1.0, 1.02, 0.97, 0.98), (2, 0.98, 0.99, 0.85, 0.88)), SHORT, LATER, fee=0.005)
    assert t["reason"] == "take-profit" and t["exit"] == pytest.approx(0.90)
    assert t["net"] == pytest.approx(0.10 - 0.005)


def test_short_stops_out_when_the_price_rises_and_gaps_fill_at_open():
    t = strategy.simulate(row(), candles((1, 1.0, 1.03, 0.99, 1.02), (2, 1.08, 1.10, 1.07, 1.09)), SHORT, LATER, fee=0)
    assert t["reason"] == "stop-loss" and t["exit"] == pytest.approx(1.08) and t["gross"] == pytest.approx(-0.08)


def test_short_with_both_levels_in_one_candle_counts_the_stop():
    t = strategy.simulate(row(), candles((1, 1.0, 1.06, 0.85, 0.9),), SHORT, LATER, fee=0)
    assert t["reason"] == "stop-loss"


def test_open_short_is_marked_to_market_and_levels_mirror():
    t = strategy.simulate(row(), candles((1, 1.0, 1.01, 0.95, 0.96),), SHORT, T0 + timedelta(minutes=5), fee=0)
    assert t["status"] == "open" and t["gross"] == pytest.approx(0.04)
    assert strategy.levels(2.0, SHORT) == (pytest.approx(1.8), pytest.approx(2.1))
    assert "SHORT" in strategy.describe(SHORT) and "TP −10% · SL +5% · max 24 h" in strategy.describe(SHORT)


def test_adding_the_side_keeps_old_journal_versions():
    import hashlib
    import json
    old_defaults = {k: v for k, v in strategy.DEFAULTS.items() if k not in strategy.ADDED_LATER}
    params = {"triggers": ["volume"], "tp": 0.12}
    before = hashlib.sha1(json.dumps({**old_defaults, **params}, sort_keys=True).encode()).hexdigest()[:8]
    assert strategy.version(params) == before
    assert strategy.version({**params, "side": "short"}) != before


def test_side_from_the_editor_form():
    assert strategy.params_from_form(MultiDict({"side": "short", "tp": "8"})) == {"side": "short", "tp": 0.08}
    assert "side" not in strategy.params_from_form(MultiDict({"side": "long"}))


def test_editor_caps_the_stop_below_liquidation():
    assert strategy.params_from_form(MultiDict({"sl": "35"}))["sl"] == strategy.MAX_SL



LADDER = {"tps": [0.05, 0.10, 0.20], "parts": [0.2, 0.4, 0.4], "sl": 0.05}


def test_ladder_takes_partial_profits_and_closes_the_rest_at_the_last_target():
    t = strategy.simulate(row(), candles((1, 1.0, 1.06, 0.99, 1.05), (2, 1.05, 1.11, 1.04, 1.10),
                                         (3, 1.10, 1.21, 1.09, 1.20)), LADDER, LATER, fee=0.005)
    assert t["reason"] == "take-profit" and t["tp_hits"] == 3
    assert t["gross"] == pytest.approx(0.2 * 0.05 + 0.4 * 0.10 + 0.4 * 0.20)
    assert t["net"] == pytest.approx(t["gross"] - 0.005)


def test_after_tp1_the_stop_is_at_the_entry_and_after_tp2_at_tp1():
    be = strategy.simulate(row(), candles((1, 1.0, 1.06, 0.99, 1.05), (2, 1.05, 1.06, 0.99, 1.0)), LADDER, LATER, fee=0)
    assert be["reason"] == "stop po TP1" and be["gross"] == pytest.approx(0.2 * 0.05)        # the rest at the entry
    tr = strategy.simulate(row(), candles((1, 1.0, 1.11, 0.99, 1.10), (2, 1.10, 1.10, 1.04, 1.05)), LADDER, LATER, fee=0)
    assert tr["reason"] == "stop po TP2" and tr["gross"] == pytest.approx(0.2 * 0.05 + 0.4 * 0.10 + 0.4 * 0.05)


def test_ladder_without_moving_the_stop_and_for_a_short():
    keep = strategy.simulate(row(), candles((1, 1.0, 1.06, 0.99, 1.05), (2, 1.05, 1.06, 0.94, 0.95)),
                             {**LADDER, "be": False}, LATER, fee=0)
    assert keep["reason"] == "stop po TP1" and keep["gross"] == pytest.approx(0.2 * 0.05 - 0.8 * 0.05)
    short = strategy.simulate(row(), candles((1, 1.0, 1.01, 0.94, 0.95), (2, 0.95, 1.01, 0.94, 1.0)),
                              {**LADDER, "side": "short"}, LATER, fee=0)
    assert short["reason"] == "stop po TP1" and short["gross"] == pytest.approx(0.2 * 0.05)


def test_open_ladder_trade_counts_what_was_already_banked():
    t = strategy.simulate(row(), candles((1, 1.0, 1.06, 0.99, 1.04),), LADDER, T0 + timedelta(minutes=5), fee=0)
    assert t["status"] == "open" and t["gross"] == pytest.approx(0.2 * 0.05 + 0.8 * 0.04)



def test_binance_only_rule():
    assert strategy.matches({**row(), "venue": "binance"}, {"cex_only": True})
    assert not strategy.matches({**row(), "venue": "geckoterminal"}, {"cex_only": True})
    assert not strategy.matches(row(), {"cex_only": True})                 # not resolved yet: not traded
    assert strategy.matches(row(), {})                                      # the rule is off by default
    assert all(p["cex_only"] for k, _, _, p in strategy.PRESETS)
    assert strategy.params_from_form(MultiDict({"cex_only": "1"}))["cex_only"] is True


def test_pullback_entry_fills_the_limit_and_measures_from_there():
    p = {"entry": "pullback", "entry_pct": 0.04, "tp": 0.10, "sl": 0.05}
    t = strategy.simulate(row(), candles((1, 1.0, 1.01, 0.95, 0.96), (2, 0.96, 1.07, 0.95, 1.06)), p, LATER, fee=0)
    assert t["entry"] == pytest.approx(0.96) and t["entry_at"] == crypto.iso(T0 + timedelta(minutes=1))
    assert t["reason"] == "take-profit" and t["gross"] == pytest.approx(0.10)        # +10% from 0.96, not from 1.0


def test_unfilled_order_is_no_trade():
    p = {"entry": "pullback", "entry_pct": 0.04, "entry_wait_h": 1}
    path = candles((1, 1.0, 1.02, 0.99, 1.01), (90, 1.01, 1.03, 0.90, 0.95))         # the dip comes too late
    assert strategy.simulate(row(), path, p, LATER)["status"] == "no_entry"
    assert strategy.simulate(row(), path[:1], p, T0 + timedelta(minutes=30))["status"] == "waiting"
    assert strategy.summarize([strategy.simulate(row(), path, p, LATER)])["matched"] == 0


def test_confirmed_short_enters_once_the_pump_turns():
    p = {"entry": "confirm", "entry_pct": 0.03, "side": "short", "tp": 0.10, "sl": 0.05}
    path = candles((1, 1.0, 1.20, 1.0, 1.19), (2, 1.19, 1.19, 1.15, 1.16), (3, 1.16, 1.16, 1.0, 1.02))
    t = strategy.simulate(row(), path, p, LATER, fee=0)                                 # high 1.20, turn at 1.164
    assert t["entry"] == pytest.approx(1.20 * 0.97) and t["entry_at"] == crypto.iso(T0 + timedelta(minutes=2))
    assert t["reason"] == "take-profit"


def test_confirmed_entry_twins():
    keys = {k for k, _, _, _ in strategy.PRESETS}
    assert {"accumulation_conf", "high_score_conf", "micro_conf"} <= keys and "fade_pump_conf" not in keys
    p = next(p for k, _, _, p in strategy.PRESETS if k == "micro_conf")
    assert p["entry"] == "confirm" and p["cex_only"] and strategy.is_twin("micro_conf") and not strategy.is_twin("micro")

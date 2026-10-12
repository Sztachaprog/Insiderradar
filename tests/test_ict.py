import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ict  # noqa: E402

DAY = date(2026, 9, 15)          # a Tuesday


def ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ict.NY).timestamp())


def session(day=DAY, after=None):
    """Asian range 100-101 the evening before, flat until 01:00, then a sweep above 101 to 101.5,
    a 5M close below the 01:00 swing low (100.1) -> short at 100.05, stop 101.5."""
    prev = day - timedelta(days=1)
    bars = [(ts(prev, 19) + i * 300, 100.5, 101.0, 100.0, 100.5) for i in range(60)]
    bars += [(ts(day, 0) + i * 300, 100.5, 100.8, 100.2, 100.5) for i in range(12)]
    bars += [(ts(day, 1, 0), 100.5, 100.6, 100.1, 100.3),        # swing low 100.1
             (ts(day, 1, 5), 100.3, 101.3, 100.25, 101.2),       # sweeps the Asian high
             (ts(day, 1, 10), 101.2, 101.5, 101.0, 101.1),       # manipulation extreme 101.5
             (ts(day, 1, 15), 101.1, 101.2, 100.0, 100.05)]      # closes below 100.1: entry
    bars += after if after is not None else [(ts(day, 1, 20), 100.0, 100.1, 97.0, 97.5)]   # target 97.15
    return bars


NOW = datetime(2026, 10, 1, tzinfo=ict.NY)


def test_sweep_and_structure_shift_give_a_short_to_the_target():
    (t,) = ict.backtest(session(), {}, NOW)
    risk = (101.5 - 100.05) / 100.05
    assert t["side"] == "short" and t["entry"] == 100.05 and t["stop"] == 101.5
    assert t["target"] == pytest.approx(100.05 - 2 * 1.45) and t["reason"] == "take-profit"
    assert t["r"] == pytest.approx((1 - t["target"] / 100.05 - ict.FEE) / risk)
    assert t["entry_ts"] == ts(DAY, 1, 20)                      # after the signal candle closed


def test_stop_first_when_one_candle_reaches_both():
    (t,) = ict.backtest(session(after=[(ts(DAY, 1, 20), 100.0, 101.6, 97.0, 98.0)]), {}, NOW)
    assert t["reason"] == "stop-loss" and t["r"] < -1


def test_time_exit_at_16_ny_and_weekday_filter():
    flat = [(ts(DAY, 1, 20) + i * 300, 100.0, 100.2, 99.8, 99.9) for i in range(12 * 15)]
    (t,) = ict.backtest(session(after=flat), {}, NOW)
    assert t["reason"] == "czas" and t["exit"] == 99.9
    monday = DAY - timedelta(days=1)
    assert ict.backtest(session(day=monday), {}, NOW) == []
    assert len(ict.backtest(session(day=monday), {"days": [0]}, NOW)) == 1


def test_no_trade_without_a_sweep_or_with_a_too_tight_stop():
    no_sweep = [b if b[2] <= 101 else (b[0], b[1], 100.9, b[3], min(b[4], 100.9)) for b in session()]
    assert ict.backtest(no_sweep, {}, NOW) == []
    assert ict.backtest(session(), {"max_risk": 0.01}, NOW) == []   # the 1.45% stop is above the cap


def test_summary_in_r():
    s = ict.summarize(ict.backtest(session(), {}, NOW))
    assert s["n"] == 1 and s["win_rate"] == 1 and s["shorts"] == 1 and s["total_r"] == pytest.approx(s["avg_r"])

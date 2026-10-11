import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import candles  # noqa: E402
import crypto  # noqa: E402
import strategy  # noqa: E402

T0 = datetime(2026, 10, 10, 12, 0, 30, tzinfo=timezone.utc)
AT = int(T0.timestamp()) // 60 * 60


def event(eid=1, symbol="XYZ", price=1.0, detected=T0):
    return {"id": eid, "coin_id": symbol.lower(), "symbol": symbol, "name": symbol.title(),
            "detected_at": crypto.iso(detected), "base_price": price}


def flat(start, n, res=60, price=1.0):
    return [(start + i * res, price, price * 1.01, price * 0.99, price, 10.0) for i in range(n)]


class FakeClient(candles.CandleClient):
    def __init__(self, binance_price=None, pools=(), dex_price=1.0, fail=False):
        super().__init__()
        self.binance_price, self.pools, self.dex_price, self.fail = binance_price, list(pools), dex_price, fail
        self.calls = []

    def binance_symbols(self):
        return {"XYZUSDT"} if self.binance_price is not None else set()

    def binance(self, symbol, res, start, end):
        self.calls.append(("binance", res, start, end))
        n = max(0, (end - start) // res)
        return flat(start, n, res, self.binance_price or 1.0)

    def gt_pools(self, symbol):
        self.gt_calls += 1
        if self.fail:
            raise requests.ConnectionError("down")
        return self.pools

    def gt(self, pool, res, start, end):
        self.gt_calls += 1
        self.calls.append(("gt", res, start, end))
        n = max(0, (end - start) // res)
        return flat(start, min(n, 1000), res, self.dex_price)


def test_window_is_measured_from_detection():
    w = candles.window(crypto.iso(T0))
    assert w[60] == (AT, AT + 48 * 3600)
    assert w[3600][0] == AT // 3600 * 3600 - 24 * 3600 and w[3600][1] == AT + 7 * 86400


def test_resolve_prefers_binance_when_price_matches():
    assert candles.resolve(FakeClient(binance_price=1.02), event()) == ("binance", "XYZUSDT")


def test_same_ticker_different_coin_on_binance_falls_back_to_dex():
    client = FakeClient(binance_price=42.0, pools=[{"id": "eth_0xpool", "liquidity": 5e5, "price": 1.01}])
    assert candles.resolve(client, event(), latest_price=1.0) == ("geckoterminal", "eth_0xpool")


def test_resolve_none_and_retry():
    assert candles.resolve(FakeClient(pools=[{"id": "p", "liquidity": 5e5, "price": 3.0}]), event(), 1.0) == ("none", None)
    with pytest.raises(candles.Retry):
        candles.resolve(FakeClient(fail=True), event())


def test_update_appends_only_new_finished_candles(tmp_path):
    store = candles.CandleStore(tmp_path / "c.db")
    client = FakeClient(binance_price=1.0)
    now = T0 + timedelta(hours=2, minutes=5)
    stats = candles.update(client, store, [event()], now)
    assert stats["binance"] == 1 and stats["resolved"] == 1
    last = store.last_ts()
    assert last[(1, 60)] == int(now.timestamp()) // 60 * 60 - 60       # the running minute is not stored
    client.calls.clear()
    candles.update(client, store, [event()], now + timedelta(minutes=10))
    minute_calls = [c for c in client.calls if c[0] == "binance" and c[1] == 60 and c[3] - c[2] > 120]
    assert minute_calls and all(c[2] == last[(1, 60)] + 60 for c in minute_calls)   # continues where it stopped


def test_dex_budget_and_paths_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(candles, "GT_CALLS_PER_RUN", 3)
    store = candles.CandleStore(tmp_path / "c.db")
    client = FakeClient(pools=[{"id": "eth_0xp", "liquidity": 5e5, "price": 1.0}])
    evs = [event(i, f"T{i}", detected=T0 + timedelta(minutes=i)) for i in range(1, 4)]
    stats = candles.update(client, store, evs, T0 + timedelta(hours=1), {f"t{i}": 1.0 for i in range(1, 4)})
    assert client.gt_calls <= 3 and stats["deferred"] >= 1          # the rest waits for the next run
    store.save(9, 60, flat(AT, 3))
    store.save(9, 3600, flat(AT // 3600 * 3600 - 3600, 4, 3600))
    path = store.paths([9])[9]
    assert len(path) == 3 + 2 and path[0][0] < path[-1][0]          # minutes, then hours after the last minute


# ---------- simulation on candles ----------

def row():
    return {"id": 1, "coin_id": "xyz", "detected_at": crypto.iso(T0), "base_price": 1.0, "triggers": ["volume"],
            "signal": {"score": 60}, "mcap": 10e6, "ch24": 0.1, "vol_ratio": 1.0}


def bar(minutes, o, h, low, c):
    return (crypto.iso(T0 + timedelta(minutes=minutes)), o, h, low, c)


def test_take_profit_on_candle_high_between_scans():
    path = [bar(1, 1.0, 1.03, 0.99, 1.02), bar(2, 1.02, 1.12, 1.01, 1.04)]    # closes never reach +10%
    t = strategy.simulate(row(), path, {"tp": 0.10, "sl": 0.05}, T0 + timedelta(days=2), fee=0)
    assert t["reason"] == "take-profit" and t["gross"] == pytest.approx(0.10) and t["source"] == "candles"


def test_both_levels_in_one_candle_counts_as_stop_loss():
    t = strategy.simulate(row(), [bar(1, 1.0, 1.15, 0.90, 1.0)], {"tp": 0.10, "sl": 0.05}, T0 + timedelta(days=2), fee=0)
    assert t["reason"] == "stop-loss" and t["gross"] == pytest.approx(-0.05)


def test_gap_through_stop_fills_at_open():
    t = strategy.simulate(row(), [bar(1, 1.0, 1.0, 0.99, 1.0), bar(2, 0.80, 0.82, 0.78, 0.8)], {"sl": 0.05},
                          T0 + timedelta(days=2), fee=0)
    assert t["gross"] == pytest.approx(-0.20)


# ---------- journal ----------

def test_journal_freezes_final_trades_and_versions_rules(tmp_path):
    j = strategy.Journal(tmp_path / "j.db")
    params = {"tp": 0.10}
    win = strategy.simulate(row(), [bar(1, 1.0, 1.2, 1.0, 1.1)], params, T0 + timedelta(days=2), fee=0)
    assert j.record("micro", params, [win], lambda t: True) == 1
    loss = strategy.simulate(row(), [bar(1, 1.0, 1.0, 0.5, 0.5)], params, T0 + timedelta(days=2), fee=0)
    assert j.record("micro", params, [loss], lambda t: True) == 0            # final rows never change
    assert j.rows("micro", strategy.version(params))[0]["reason"] == "take-profit"
    assert j.record("micro", {"tp": 0.2}, [loss], lambda t: False) == 1      # new rules = new version
    trades = strategy.journal_trades(j.rows("micro", strategy.version(params)), {1: row()})
    assert strategy.summarize(trades)["closed"] == 1

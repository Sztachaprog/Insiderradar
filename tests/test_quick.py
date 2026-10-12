import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci  # noqa: E402
import crypto  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def cg(cid, sym, price, mcap=50e6, vol=1e6):
    return {"id": cid, "symbol": sym.lower(), "name": sym.title(), "image": "", "current_price": price,
            "market_cap": mcap, "market_cap_rank": 300, "fully_diluted_valuation": mcap, "total_volume": vol,
            "ath_change_percentage": -50, "price_change_percentage_7d_in_currency": 5}


def tick(pair, price, ch24=1.0, qvol=2e6):
    return {"symbol": pair, "lastPrice": str(price), "priceChangePercent": str(ch24), "quoteVolume": str(qvol)}


@pytest.fixture
def scanner(tmp_path):
    store = crypto.CryptoStore(tmp_path / "c.db")
    store.save_universe([cg("moon", "MOON", 1.0), cg("other-ai", "AI", 5.0)], {"moon"})
    sc = crypto.CryptoScanner(store, None)
    # hourly full scan: Binance MOON matches CoinGecko MOON's price, Binance "AI" ($0.20) is another coin
    sc._map_binance(NOW - timedelta(minutes=30), ticker=[tick("MOONUSDT", 1.02), tick("AIUSDT", 0.20)])
    return sc


def test_quick_mode_detects_24h_move_from_binance(scanner):
    scanner._run_quick(NOW, ticker=[tick("MOONUSDT", 1.3, ch24=30), tick("BTCUSDT", 60000)])
    (e,) = scanner.store.events()
    assert e["coin_id"] == "moon" and "pump24" in e["triggers"] and e["base_price"] == 1.3
    assert "trending" in e["triggers"]                         # trending set comes from the last full scan
    assert scanner.store.prices()["bitcoin"][-1][1] == 60000   # benchmark snapshot


def test_quick_mode_ignores_same_ticker_different_coin(scanner):
    scanner._run_quick(NOW, ticker=[tick("AIUSDT", 0.20, ch24=50)])   # Binance "AI" is not CoinGecko's $5 "AI"
    assert scanner.store.events() == []


def test_quick_mode_1h_change_from_own_ticks(scanner):
    scanner.store.save_universe([cg("moon", "MOON", 1.0)], set())
    scanner._map_binance(NOW - timedelta(minutes=61), ticker=[tick("MOONUSDT", 1.0)])
    scanner._run_quick(NOW - timedelta(minutes=60), ticker=[tick("MOONUSDT", 1.00)])
    scanner._run_quick(NOW, ticker=[tick("MOONUSDT", 1.15)])
    (e,) = scanner.store.events()
    assert e["triggers"] == ["move1h"] and e["metrics"]["price_change_percentage_1h_in_currency"] == pytest.approx(15)


def test_quick_mode_needs_a_full_scan_first(tmp_path):
    sc = crypto.CryptoScanner(crypto.CryptoStore(tmp_path / "c.db"), None)
    sc.run_once(NOW, mode="quick")
    assert "pełny skan" in sc.last_error


def test_schedule_runs_tasks_only_when_due(tmp_path):
    sched = ci.Schedule(tmp_path / "s.db")
    assert sched.due("stocks", NOW)
    sched.done("stocks", NOW)
    assert not sched.due("stocks", NOW + timedelta(minutes=5))
    assert sched.due("stocks", NOW + timedelta(minutes=ci.EVERY["stocks"]))
    assert sched.due("coingecko_full", NOW)                    # independent tasks


def test_binance_map_keeps_only_price_matching_tickers(scanner):
    assert scanner.store.binance_map() == {"MOONUSDT": "MOON"}


def test_coingecko_interval_follows_the_monthly_budget(tmp_path):
    sched = ci.Schedule(tmp_path / "s.db")
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert sched.coingecko_every(start) == pytest.approx(48)          # 9000 credits / ~10 per scan over 30 days
    for i in range(10):                                               # scans cost 30 calls each: too many
        sched.coingecko_used(30, start + timedelta(hours=i))
    every = sched.coingecko_every(start + timedelta(hours=10))
    assert 120 < every <= ci.CG_MAX_EVERY_MIN                         # 8700 left / 30 = 290 scans for ~29.6 days
    for i in range(100):                                              # cheap scans: 3 calls each
        sched.coingecko_used(3, start + timedelta(hours=10, minutes=i))
    assert sched.coingecko_every(start + timedelta(hours=12)) == ci.CG_MIN_EVERY_MIN


def test_coingecko_budget_seeds_a_month_started_midway(tmp_path):
    sched = ci.Schedule(tmp_path / "s.db")
    mid = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)             # half of October assumed spent
    sched.coingecko_used(6, mid)
    used, scans, seeded = sched._usage(mid)
    assert seeded == 4500 and used == 4506 and scans == 1

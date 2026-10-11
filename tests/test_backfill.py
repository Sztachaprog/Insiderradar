import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backfill  # noqa: E402
import crypto  # noqa: E402

COIN = {"symbol": "XYZUSDT", "base": "XYZ", "id": "xyz", "name": "Xyz", "image": "", "market_cap": 50e6, "rank": 400}
T0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


def hours(prices, vol=1e6):
    return [(T0 + i * 3600, p, p, p, p, vol) for i, p in enumerate(prices)]


def test_detect_replays_radar_rules_with_72h_dedupe():
    prices = [1.0] * 30 + [1.3] * 10 + [1.0] * 20 + [1.4] * 5 + [1.0] * 80
    found = backfill.detect(hours(prices), COIN, until=T0 + 10**7)
    assert found[0]["triggers"] == ["pump24", "move1h"] and found[0]["detected"] == T0 + 31 * 3600
    assert found[0]["price"] == 1.3 and found[0]["metrics"]["ath_change_percentage"] is None
    assert len(found) == 1                       # the second move falls inside the 72 h dedupe window


def test_detect_stops_where_live_data_begins_and_needs_volume():
    prices = [1.0] * 30 + [1.5] * 10
    assert backfill.detect(hours(prices), COIN, until=T0 + 20 * 3600) == []
    assert backfill.detect(hours(prices, vol=1000), COIN, until=T0 + 10**7) == []


def test_unknown_ath_is_not_scored_as_near_ath():
    e = {"triggers": ["pump24"], "metrics": {"market_cap": 50e6, "total_volume": 5e6, "ath_change_percentage": None,
                                             "price_change_percentage_24h_in_currency": 30}}
    assert "near_ath" not in crypto.analyze(e, None)["factors"]

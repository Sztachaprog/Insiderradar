import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp  # noqa: E402
import crypto  # noqa: E402
import whales  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
TOK = "0x" + "a" * 40
WHALE, FUND, BINANCE, POOL = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "b" * 40, "0x" + "c" * 40
EXCHANGE_TAGS = {"tags": [{"name": "Binance: Hot Wallet", "slug": "binance-hot-wallet", "tagType": "name"},
                          {"name": "Exchange", "slug": "exchange", "tagType": "generic"}]}


def addr(h, contract=False, metadata=None, name=None, impl=None):
    return {"hash": h, "is_contract": contract, "metadata": metadata, "name": name,
            "implementations": [{"name": impl}] if impl else []}


@pytest.mark.parametrize("a, kind", [
    (addr(BINANCE, metadata=EXCHANGE_TAGS), "exchange"),
    (addr(POOL, contract=True, name="UniswapV3Pool"), "contract"),
    (addr(FUND, contract=True, name="GnosisSafeProxy", impl="SafeL2"), "safe"),
    (addr(WHALE), "wallet"),
])
def test_address_kinds(a, kind):
    assert whales.kind_of_address(a) == kind


def test_labels_skip_contract_type_names():
    assert whales.label_of(addr(FUND, name="GnosisSafeProxy")) == ""
    assert whales.label_of(addr(BINANCE, metadata=EXCHANGE_TAGS)) == "Binance: Hot Wallet"


@pytest.mark.parametrize("sym, price, ok", [("LINK", 12.8, True), ("USDC", 1.0, False), ("WETH", 2500, False),
                                           ("BNB", 700, False), ("STETH", 2400, False)])
def test_tradable_token(sym, price, ok):
    t = {"symbol": sym, "name": sym, "exchange_rate": str(price), "circulating_market_cap": "1e9"}
    assert whales.tradable_token(t) is ok


def tx(direction, kind, qty, days_ago, i=0):
    return {"direction": direction, "cp_kind": kind, "amount": qty,
            "ts": crypto.iso(NOW - timedelta(days=days_ago)), "i": i}


def test_position_stats_counts_only_market_flows():
    st = whales.position_stats([tx("in", "exchange", 100, 2), tx("in", "contract", 50, 10),
                                tx("in", "wallet", 10_000, 1),        # own-wallet shuffle: ignored
                                tx("out", "contract", 30, 40)], price=2.0, now=NOW)
    assert st["net30"] == 150 and st["net7_usd"] == 200 and st["buys30"] == 2
    assert st["moved30"] == 10_000 and st["accumulating"] and not st["distributing"]
    dump = whales.position_stats([tx("out", "exchange", 100, 3)], price=1, now=NOW)
    assert dump["distributing"] and dump["cex_in30"] == 100


def test_token_score_rewards_cluster_and_penalises_exchange_deposits():
    acc = dict(whales.position_stats([tx("in", "exchange", 1e6, 2), tx("in", "contract", 1e6, 9)], 1.0, NOW), price=1.0)
    score, reasons, factors = whales.token_score([acc, acc, acc], mcap=100e6)
    assert {"acc3", "flow_big", "recent", "cex_out"} <= set(factors) and score >= 80
    dist = dict(whales.position_stats([tx("out", "exchange", 5e6, 2)], 1.0, NOW), price=1.0)
    low, _, f2 = whales.token_score([dist], mcap=100e6)
    assert "dist" in f2 and "cex_in" in f2 and low < 20


def transfer(frm, to, value="5000000000000000000000", ts="2026-10-08T10:00:00.000000Z"):
    return {"transaction_hash": "0xt", "log_index": 1, "from": frm, "to": to, "timestamp": ts,
            "total": {"value": value, "decimals": "18"},
            "token": {"address_hash": TOK, "symbol": "TKN", "decimals": "18", "exchange_rate": "2"}}


def test_transfer_rows_direction_counterparty_and_usd():
    rows = whales.transfer_rows([transfer(addr(BINANCE, metadata=EXCHANGE_TAGS), addr(WHALE))], WHALE, {})
    (_, _, who, token, sym, direction, qty, usd, cp, cp_label, cp_kind, ts), = rows
    assert (who, token, direction, qty, usd, cp_kind, ts) == (WHALE, TOK, "in", 5000, 10_000, "exchange",
                                                              "2026-10-08T10:00:00Z")
    text, tone = whales.describe_transfer({"direction": "in", "cp_kind": "exchange"})
    assert tone == "up" and "giełdy" in text


# ---------- scanner + views on fake Blockscout ----------

class FakeScout:
    def __init__(self):
        self.calls = []

    def tokens(self, pages):
        return [{"address_hash": TOK, "symbol": "TKN", "name": "Token", "decimals": "18", "exchange_rate": "2",
                 "circulating_market_cap": "100000000", "total_supply": str(100_000_000 * 10 ** 18),
                 "holders_count": "1000", "icon_url": ""},
                {"address_hash": "0x" + "d" * 40, "symbol": "USDC", "name": "USD Coin", "decimals": "6",
                 "exchange_rate": "1", "circulating_market_cap": "1e10"}]

    def holders(self, token):
        self.calls.append(("holders", token))
        big = str(2_000_000 * 10 ** 18)                                     # $4M
        return [{"address": addr(WHALE), "value": big},
                {"address": addr(BINANCE, metadata=EXCHANGE_TAGS), "value": big},
                {"address": addr(POOL, contract=True, name="UniswapV3Pool"), "value": big},
                {"address": addr("0x" + "9" * 40), "value": str(10 * 10 ** 18)},           # tiny
                {"address": addr("0x" + "7" * 40), "value": str(20_000_000 * 10 ** 18)}]   # 20% supply

    def transfers(self, address, token=None):
        self.calls.append(("transfers", address))
        if address != WHALE:
            return []
        ex, pool = addr(BINANCE, metadata=EXCHANGE_TAGS), addr(POOL, contract=True)
        return [dict(transfer(ex, addr(WHALE), value=str(600_000 * 10 ** 18)), log_index=1),
                dict(transfer(pool, addr(WHALE), ts="2026-10-05T10:00:00.000000Z"), log_index=2)]

    def portfolio(self, address):
        return [{"token": {"address_hash": TOK}, "value": str(1_000_000 * 10 ** 18)}]

    def address(self, address):
        return {"hash": address, "name": None}


@pytest.fixture
def scanned(tmp_path):
    store = whales.WhaleStore(tmp_path / "w.db")
    scout = FakeScout()
    sc = whales.WhaleScanner(store, lambda: scout)
    sc.run_once(now=NOW)
    return store, sc, scout


def test_scanner_discovers_whales_and_filters_noise(scanned):
    store, sc, scout = scanned
    assert sc.last_error is None and set(store.tokens()) == {TOK}            # stablecoin skipped
    kinds = {a: w["kind"] for a, w in store.whales().items()}
    assert kinds == {WHALE: "wallet", "0x" + "7" * 40: "treasury"}
    assert store.labels()[BINANCE]["kind"] == "exchange" and store.labels()[POOL]["kind"] == "contract"
    assert ("transfers", "0x" + "7" * 40) not in scout.calls                  # treasuries are not synced
    assert len(store.transfers()) == 2


def test_build_flags_accumulation_signal(scanned):
    store, _, _ = scanned
    d = whales.build(store, NOW)
    (tok,) = d["tokens"]
    assert tok["acc"] == 1 and tok["net30_usd"] == pytest.approx(2 * (600_000 + 5_000))
    assert [w["address"] for w in d["whales"]] == [WHALE]                     # treasury hidden by default
    assert d["big"][0]["text"].startswith("Wypłata z giełdy")
    whales.WhaleScanner(store, FakeScout).run_once(now=NOW + timedelta(hours=13))
    assert whales.build(store, NOW + timedelta(hours=13))["tokens"][0]["whales"] == 1


def test_manual_wallet_and_pages(tmp_path):
    store = webapp.Store(tmp_path / "t.db")
    wstore = whales.WhaleStore(tmp_path / "t.db")
    whales.WhaleScanner(wstore, FakeScout).run_once(now=NOW)

    class Stub:
        running, last_run, last_new, last_error = False, "12:00:00", 0, None

        def trigger(self):
            pass

    wsc = whales.WhaleScanner(wstore, FakeScout)
    app = webapp.create_app(store=store, scanner=Stub(), start_background=False, crypto_scanner=Stub(),
                            whale_store=wstore, whale_scanner=wsc)
    c = app.test_client()
    html = c.get("/wieloryby").get_data(as_text=True)
    assert "TKN" in html and "Wypłata z giełdy" in html
    assert c.get(f"/wieloryby/token/{TOK}").status_code == 200
    assert c.get(f"/wieloryby/portfel/{WHALE}").status_code == 200
    fund = "0x" + "4" * 40
    resp = c.post("/wieloryby/dodaj", data={"address": fund, "label": "Fundusz X"})
    assert resp.status_code == 302 and wstore.whales()[fund]["manual"] == 1
    assert "Fundusz X" in c.get(f"/wieloryby/portfel/{fund}").get_data(as_text=True)
    assert "Niepoprawny" in c.post("/wieloryby/dodaj", data={"address": "nope"}, follow_redirects=True).get_data(as_text=True)
    c.post(f"/wieloryby/{fund}/usun")
    assert fund not in wstore.whales()

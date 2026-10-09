import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solana  # noqa: E402
import whales  # noqa: E402

MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
WHALE = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"      # case matters on Solana
POOL_AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
CEX = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def token_account(owner, amount):
    return {"owner": solana.TOKEN_PROGRAMS[0], "executable": False,
            "data": {"parsed": {"info": {"owner": owner, "mint": MINT,
                                         "tokenAmount": {"amount": str(amount), "decimals": 6}}}}}


class FakeHelius(solana.SolanaClient):
    """Real parsing code, canned RPC / HTTP answers."""

    def __init__(self, txs=None):
        super().__init__(key="test")
        self.txs = txs or []

    def _rpc(self, method, params):
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "ta1", "amount": "5"}, {"address": "ta2", "amount": "4"},
                              {"address": "ta3", "amount": "3"}]}
        if method == "getMultipleAccounts":
            if params[0] == ["ta1", "ta2", "ta3"]:
                return {"value": [token_account(WHALE, 3_000_000_000_000), token_account(POOL_AUTH, 9_000_000_000_000),
                                  token_account(WHALE, 1_000_000_000_000)]}
            # owners: whale is a System-Program wallet; the pool authority is a PDA (no account)
            return {"value": [{"owner": solana.SYSTEM_PROGRAM, "executable": False} if o == WHALE else None
                              for o in params[0]]}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": "x", "account": token_account(WHALE, 2_500_000)}]
                    if params[1]["programId"] == solana.TOKEN_PROGRAMS[0] else []}
        raise AssertionError(method)

    def tokens(self, pages=1):
        self.meta[MINT] = {"symbol": "JUP", "decimals": 6, "price": 0.5}
        return [{"address_hash": MINT, "symbol": "JUP", "name": "Jupiter", "decimals": 6, "icon_url": "",
                 "exchange_rate": "0.5", "circulating_market_cap": "1500000000",
                 "total_supply": str(10_000_000_000 * 10 ** 6), "holders_count": 900000}]

    def transfers(self, address, token=None):
        return solana.to_transfer_items(self.txs, address, token, self.meta)


def test_holders_merge_token_accounts_and_flag_pdas():
    hs = FakeHelius().holders(MINT)
    assert [(h["address"]["hash"], h["address"]["is_contract"], h["value"]) for h in hs] == [
        (POOL_AUTH, True, "9000000000000"), (WHALE, False, "4000000000000")]


def test_portfolio_and_case_preserving_keys():
    assert FakeHelius().portfolio(WHALE) == [{"token": {"address_hash": MINT}, "value": "2500000"}]
    assert whales.tkey("solana", MINT) == f"solana-{MINT}" and whales.split_key(f"solana-{MINT}") == ("solana", MINT)
    assert whales.norm(WHALE) == WHALE and whales.norm("0xABC") == "0xabc"


def test_transfer_items_classify_swaps_as_dex():
    txs = [{"signature": "sig1", "timestamp": 1791460000, "type": "SWAP",
            "tokenTransfers": [{"fromUserAccount": POOL_AUTH, "toUserAccount": WHALE, "mint": MINT, "tokenAmount": 1000}]},
           {"signature": "sig2", "timestamp": 1791460100, "type": "TRANSFER",
            "tokenTransfers": [{"fromUserAccount": WHALE, "toUserAccount": CEX, "mint": MINT, "tokenAmount": 50}]},
           {"signature": "sig3", "timestamp": 1791460200, "type": "SWAP", "transactionError": {"x": 1},
            "tokenTransfers": [{"fromUserAccount": POOL_AUTH, "toUserAccount": WHALE, "mint": MINT, "tokenAmount": 9}]}]
    rows = whales.transfer_rows(solana.to_transfer_items(txs, WHALE, MINT, {MINT: {"symbol": "JUP", "decimals": 6,
                                                                                    "price": 0.5}}), WHALE, {}, "solana")
    got = [(r[0], r[2], r[3], r[5], r[6], r[7], r[10]) for r in rows]
    assert got == [("sig1", WHALE, f"solana-{MINT}", "in", 1000.0, 500.0, "contract"),
                   ("sig2", WHALE, f"solana-{MINT}", "out", 50.0, 25.0, "wallet")]


def test_scanner_runs_solana_with_helius_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "k")
    monkeypatch.delenv("BLOCKSCOUT_API_KEY", raising=False)
    monkeypatch.setenv("WHALE_CHAINS", "solana")
    assert whales.active_chains() == ["solana"]
    txs = [{"signature": f"s{i}", "timestamp": int(NOW.timestamp()) - 86400 * i, "type": "SWAP",
            "tokenTransfers": [{"fromUserAccount": POOL_AUTH, "toUserAccount": WHALE, "mint": MINT,
                                "tokenAmount": 3_000_000}]} for i in (1, 2)]
    store = whales.WhaleStore(tmp_path / "w.db")
    sc = whales.WhaleScanner(store, lambda chain: FakeHelius(txs))
    sc.run_once(now=NOW)
    assert sc.last_error is None
    assert set(store.whales()) == {WHALE}                    # the PDA pool is not a whale
    d = whales.build(store, NOW)
    (tok,) = d["tokens"]
    assert tok["chain"] == "solana" and tok["explorer"] == "https://solscan.io" and tok["address"] == MINT
    assert tok["acc"] == 1 and d["big"][0]["text"].startswith("Zakup przez DEX")


def test_solana_needs_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("WHALE_CHAINS", raising=False)
    assert "solana" not in whales.active_chains()
    with pytest.raises(ValueError):
        solana.SolanaClient()


def test_custody_heuristic(tmp_path):
    store = whales.WhaleStore(tmp_path / "w.db")
    store.upsert_whale(CEX, "", "wallet", "2026-10-01T00:00:00Z")
    for i in range(whales.CUSTODY_TOKENS):
        store.save_balance(CEX, f"solana-Mint{i}", "2026-10-01T00:00:00Z", 1.0, None)
    whales.WhaleScanner(store, None)._mark_custody("solana")
    assert store.whales()[CEX]["kind"] == "custody" and store.labels()[CEX]["kind"] == "exchange"

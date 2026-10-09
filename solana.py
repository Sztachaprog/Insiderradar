"""Solana whales via Helius (HELIUS_API_KEY, free plan) + Jupiter token list (no key).

SolanaClient speaks the same small interface as whales.BlockscoutClient (tokens,
holders, transfers, portfolio, address) and returns Blockscout-shaped dicts, so the
whale scanner, scoring and alerts work on Solana unchanged.

- tokens:    Jupiter "top organic score" list (price, market cap, supply, holder count)
- holders:   getTokenLargestAccounts (top 20 token accounts) -> owner wallets. Owners
             that are not plain System-Program wallets (PDAs: pools, vaults, programs)
             are reported as contracts, like pools on Ethereum.
- transfers: Helius parsed transaction history (100 credits/call - keep the budget small);
             swaps count as DEX trades, plain transfers as wallet-to-wallet moves.
Solana has no free exchange tags; wallets that are top holders of many tokens at once
are marked as custody (exchange-like) by the scanner instead.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

RPC_URL = "https://mainnet.helius-rpc.com/"
TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
JUPITER_URL = "https://lite-api.jup.ag/tokens/v2/toporganicscore/24h"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]


def api_key() -> str:
    return os.environ.get("HELIUS_API_KEY", "").strip()


class SolanaClient:
    def __init__(self, key: str | None = None):
        self.key = api_key() if key is None else key
        if not self.key:
            raise ValueError("solana wymaga klucza HELIUS_API_KEY")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "insider-radar/1.0"
        self.meta: dict[str, dict] = {}   # mint -> symbol/decimals/price, filled by tokens()
        self._last = 0.0

    def _wait(self, gap: float) -> None:
        wait = gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _rpc(self, method: str, params: list):
        self._wait(0.15)
        resp = self.session.post(RPC_URL, params={"api-key": self.key}, timeout=40,
                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method}: {data['error'].get('message', '')}")
        return data["result"]

    # --- Blockscout-compatible interface ---

    def tokens(self, pages: int = 1) -> list[dict]:
        self._wait(0.5)
        resp = self.session.get(JUPITER_URL, params={"limit": 100}, timeout=40)
        resp.raise_for_status()
        out = []
        for t in resp.json():
            if not t.get("isVerified") or not t.get("usdPrice") or not t.get("mcap"):
                continue
            dec = int(t.get("decimals") or 0)
            self.meta[t["id"]] = {"symbol": t.get("symbol"), "decimals": dec, "price": t["usdPrice"]}
            out.append({"address_hash": t["id"], "symbol": t.get("symbol"), "name": t.get("name"), "decimals": dec,
                        "icon_url": t.get("icon"), "exchange_rate": str(t["usdPrice"]),
                        "circulating_market_cap": str(t["mcap"]),
                        "total_supply": str(int((t.get("totalSupply") or 0) * 10 ** dec)),
                        "holders_count": t.get("holderCount") or 0})
        out.sort(key=lambda t: -float(t["circulating_market_cap"]))
        return out

    def holders(self, mint: str) -> list[dict]:
        largest = self._rpc("getTokenLargestAccounts", [mint])["value"]
        accounts = self._rpc("getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
        by_owner: dict[str, int] = {}
        for acc in accounts:
            info = ((acc or {}).get("data") or {}).get("parsed", {}).get("info", {}) if isinstance((acc or {}).get("data"), dict) else {}
            if info.get("owner"):
                by_owner[info["owner"]] = by_owner.get(info["owner"], 0) + int(info.get("tokenAmount", {}).get("amount", 0))
        owners = list(by_owner)
        owner_accounts = self._rpc("getMultipleAccounts", [owners, {"encoding": "jsonParsed"}])["value"] if owners else []
        out = []
        for owner, acc in zip(owners, owner_accounts):
            wallet = acc is not None and acc.get("owner") == SYSTEM_PROGRAM and not acc.get("executable")
            out.append({"address": {"hash": owner, "is_contract": not wallet, "metadata": None, "name": None},
                        "value": str(by_owner[owner])})
        out.sort(key=lambda h: -int(h["value"]))
        return out

    def transfers(self, address: str, token: str | None = None) -> list[dict]:
        self._wait(0.6)  # Enhanced APIs: 2 req/s on the free plan
        resp = self.session.get(TX_URL.format(address=address), timeout=40,
                                params={"api-key": self.key, "limit": 100, "token-accounts": "balanceChanged"})
        resp.raise_for_status()
        return to_transfer_items(resp.json(), address, token, self.meta)

    def portfolio(self, address: str) -> list[dict]:
        out = []
        for program in TOKEN_PROGRAMS:
            res = self._rpc("getTokenAccountsByOwner", [address, {"programId": program}, {"encoding": "jsonParsed"}])
            for item in res["value"]:
                info = item["account"]["data"]["parsed"]["info"]
                out.append({"token": {"address_hash": info["mint"]}, "value": info["tokenAmount"]["amount"]})
        return out

    def address(self, address: str) -> dict:
        return {"hash": address, "name": None}


def to_transfer_items(txs: list[dict], address: str, token: str | None, meta: dict[str, dict]) -> list[dict]:
    """Helius parsed transactions -> Blockscout-style token transfer items for whales.transfer_rows."""
    items = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        swap = tx.get("type") == "SWAP"
        ts = datetime.fromtimestamp(tx.get("timestamp") or 0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for i, tt in enumerate(tx.get("tokenTransfers") or []):
            mint = tt.get("mint")
            if (token and mint != token) or address not in (tt.get("fromUserAccount"), tt.get("toUserAccount")):
                continue
            m = meta.get(mint, {})
            dec = int(m.get("decimals") or 0)
            items.append({
                "transaction_hash": tx.get("signature"), "log_index": i, "timestamp": ts,
                # In a swap the other side is the pool/aggregator: count it as a DEX trade.
                "from": {"hash": tt.get("fromUserAccount") or "", "is_contract": swap and tt.get("fromUserAccount") != address},
                "to": {"hash": tt.get("toUserAccount") or "", "is_contract": swap and tt.get("toUserAccount") != address},
                "total": {"value": str(int(float(tt.get("tokenAmount") or 0) * 10 ** dec)), "decimals": dec},
                "token": {"address_hash": mint, "symbol": m.get("symbol") or (mint or "")[:4],
                          "decimals": dec, "exchange_rate": m.get("price")},
            })
    return items

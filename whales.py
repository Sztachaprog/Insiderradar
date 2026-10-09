"""Whale tracker: big wallets that keep adding to a coin (Blockscout API).

Ethereum works without a key (public eth.blockscout.com). With a free Blockscout PRO
key (BLOCKSCOUT_API_KEY, dev.blockscout.com) the same scan also covers Arbitrum and
Optimism, with smaller per-chain budgets to stay inside the free credits. Base and
Polygon need a paid plan (WHALE_CHAINS=ethereum,arbitrum,optimism,base,polygon).

Discovery: the top holders of the ~150 largest ERC-20 tokens. Exchange wallets,
bridges/pools (contracts) and burn addresses are filtered out using Blockscout's
public tags; Safe multisigs are kept (funds use them). A holder worth >= $1M becomes
a whale. Every refresh stores balance snapshots, and each whale's transfers of that
token are pulled to see *how* the position changes: buys via DEX, withdrawals from
an exchange (cold storage = accumulation) or deposits to one (likely selling).

Per token, consistent accumulation by several whales becomes a long-term spot
signal; the price at that moment is recorded to measure the call over weeks.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import requests

import crypto
import signals
import safety

CHAINS = {
    "ethereum": {"id": 1, "label": "Ethereum", "explorer": "https://etherscan.io"},
    "base": {"id": 8453, "label": "Base", "explorer": "https://basescan.org", "paid": True},
    "arbitrum": {"id": 42161, "label": "Arbitrum", "explorer": "https://arbiscan.io"},
    "optimism": {"id": 10, "label": "Optimism", "explorer": "https://optimistic.etherscan.io"},
    "polygon": {"id": 137, "label": "Polygon", "explorer": "https://polygonscan.com", "paid": True},
    "solana": {"label": "Solana", "explorer": "https://solscan.io", "helius": True},
}
HIDDEN_KINDS = {"treasury", "custody"}   # team/vesting wallets and exchange-like custody: not traders
CUSTODY_TOKENS = 8                       # a Solana wallet in the top holders of this many tokens = exchange/custody
SOLANA_UNIVERSE = int(os.environ.get("WHALE_SOLANA_TOKENS", "80"))
SOLANA_HOLDERS = int(os.environ.get("WHALE_SOLANA_HOLDERS_PER_RUN", "10"))
SOLANA_TRANSFERS = int(os.environ.get("WHALE_SOLANA_TRANSFERS_PER_RUN", "5"))   # 100 Helius credits each
CHAIN = "ethereum"
PUBLIC_ETH = "https://eth.blockscout.com/api/v2"      # free, no key; keeps PRO credits for the other chains
PRO_API = "https://api.blockscout.com/{id}/api/v2"
EXTRA_UNIVERSE = int(os.environ.get("WHALE_EXTRA_TOKENS", "60"))       # per non-Ethereum chain
EXTRA_HOLDERS = int(os.environ.get("WHALE_EXTRA_HOLDERS_PER_RUN", "5"))
EXTRA_TRANSFERS = int(os.environ.get("WHALE_EXTRA_TRANSFERS_PER_RUN", "12"))
UNIVERSE = int(os.environ.get("WHALE_TOKENS", "150"))       # tokens by market cap
WHALE_MIN_USD = float(os.environ.get("WHALE_MIN_USD", "1e6"))
BIG_TX_USD = 1e6
HOLDERS_PER_RUN = int(os.environ.get("WHALE_HOLDERS_PER_RUN", "25"))
TRANSFERS_PER_RUN = int(os.environ.get("WHALE_TRANSFERS_PER_RUN", "60"))
TRANSFER_SYNC_HOURS = 12
TREASURY_SHARE = 0.05       # one wallet with >= 5% of supply is a team/treasury/vesting wallet, not a trader
SIGNAL_SCORE = 60
HORIZONS = [(7, "7 dni"), (30, "30 dni"), (90, "90 dni")]

SKIP_TOKEN = re.compile(r"usd|eur|wrapped|staked|bridged|liquid|restak|\bw?eth\b|steth|btc|tether|dai\b|"
                        r"gold|paxg|xaut|bond|treasury|fund\b", re.I)
EXCHANGE_RE = re.compile(r"binance|coinbase|kraken|okx|okex|bybit|bitfinex|kucoin|gate\.?io|htx|huobi|"
                         r"crypto\.com|upbit|bitget|mexc|robinhood|gemini|bithumb|bitstamp|poloniex|"
                         r"bitvavo|deribit|exchange|hot wallet|deposit", re.I)
# Exchange tokens (their top holders are the exchange's own wallets) and stable/yield wrappers.
SKIP_SYMBOLS = {"BNB", "WBT", "LEO", "OKB", "HTX", "BGB", "GT", "CRO", "KCS", "MX", "USYC", "A7A5", "BUIDL", "USTB"}
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}


# ---------- Chains & token keys ----------

def api_key() -> str:
    return safety.extract_key(os.environ.get("BLOCKSCOUT_API_KEY", ""))


# Since 2026-10-01 Base, Polygon (and zkSync) are no longer on Blockscout's Free tier.
FREE_TIER = [c for c, info in CHAINS.items() if not info.get("paid")]


def active_chains() -> list[str]:
    """Ethereum always; EVM chains with a Blockscout PRO key; Solana with a Helius key.
    WHALE_CHAINS=ethereum,base,... overrides (e.g. paid Blockscout plans)."""
    import solana
    wanted = [c.strip() for c in os.environ.get("WHALE_CHAINS", ",".join(FREE_TIER)).split(",")]

    def available(c: str) -> bool:
        if c == "ethereum":
            return True
        return bool(solana.api_key()) if CHAINS[c].get("helius") else bool(api_key())
    return [c for c in wanted if c in CHAINS and available(c)]


def client_for(chain: str):
    import solana
    return solana.SolanaClient() if CHAINS[chain].get("helius") else BlockscoutClient(chain)


def norm(address: str) -> str:
    """EVM addresses are case-insensitive (store lowercase); Solana base58 addresses are not."""
    address = (address or "").strip()
    return address.lower() if address.startswith("0x") else address


def tkey(chain: str, address: str) -> str:
    """Token id in the database: the bare address on Ethereum (as before), "<chain>-<address>" elsewhere."""
    address = norm(address)
    return address if chain == "ethereum" else f"{chain}-{address}"


def split_key(key: str) -> tuple[str, str]:
    return ("ethereum", key) if key.startswith("0x") else tuple(key.split("-", 1))


def explorer_for(key: str) -> str:
    return CHAINS.get(split_key(key)[0], CHAINS["ethereum"])["explorer"]


# ---------- HTTP ----------

class BlockscoutClient:
    def __init__(self, chain: str = CHAIN, key: str | None = None):
        key = api_key() if key is None else key
        self.params = {}
        if chain == "ethereum":
            self.base = PUBLIC_ETH
        elif key:
            self.base = PRO_API.format(id=CHAINS[chain]["id"])
            self.params = {"apikey": key}
        else:
            raise ValueError(f"{chain} wymaga klucza BLOCKSCOUT_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "insider-radar/1.0", "Accept": "application/json"})
        self._last = 0.0

    def _get(self, path: str, **params) -> dict:
        wait = 0.25 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(self.base + path, params={**params, **self.params} or None, timeout=40)
        self._last = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def tokens(self, pages: int) -> list[dict]:
        out, params = [], {"type": "ERC-20"}
        for _ in range(pages):
            d = self._get("/tokens", **params)
            out += d.get("items", [])
            if not d.get("next_page_params"):
                break
            params = {"type": "ERC-20", **{k: str(v).lower() if isinstance(v, bool) else v
                                           for k, v in d["next_page_params"].items() if v is not None}}
        return out

    def holders(self, token: str) -> list[dict]:
        return self._get(f"/tokens/{token}/holders").get("items", [])

    def transfers(self, address: str, token: str | None = None) -> list[dict]:
        params = {"type": "ERC-20"}
        if token:
            params["token"] = token
        return self._get(f"/addresses/{address}/token-transfers", **params).get("items", [])

    def portfolio(self, address: str) -> list[dict]:
        return self._get(f"/addresses/{address}/tokens", type="ERC-20").get("items", [])

    def address(self, address: str) -> dict:
        return self._get(f"/addresses/{address}")


# ---------- Classification ----------

def _tags(addr: dict) -> list[dict]:
    return ((addr.get("metadata") or {}).get("tags") or []) + (addr.get("public_tags") or [])


def label_of(addr: dict) -> str:
    named = [t["name"] for t in _tags(addr) if t.get("tagType") == "name" or "display_name" in t]
    name = addr.get("name") or ""
    if re.search(r"safe|proxy", name, re.I):  # contract type, not an owner
        name = ""
    return addr.get("ens_domain_name") or name or (named[0] if named else "")


def kind_of_address(addr: dict) -> str:
    """exchange | contract | safe | wallet."""
    text = " ".join([t.get("name", "") + " " + t.get("slug", "") for t in _tags(addr)] + [addr.get("name") or ""])
    if EXCHANGE_RE.search(text):
        return "exchange"
    if addr.get("is_contract"):
        impl = " ".join((i.get("name") or "") for i in addr.get("implementations") or [])
        return "safe" if re.search(r"safe", impl + " " + (addr.get("name") or ""), re.I) else "contract"
    return "wallet"


def amount(raw: str | None, decimals: str | int | None) -> float:
    try:
        return int(raw or 0) / 10 ** int(decimals or 18)
    except (TypeError, ValueError):
        return 0.0


def tradable_token(t: dict) -> bool:
    price = float(t.get("exchange_rate") or 0)
    mcap = float(t.get("circulating_market_cap") or 0)
    name = f"{t.get('symbol') or ''} {t.get('name') or ''}"
    return bool(price and mcap) and not SKIP_TOKEN.search(name) and not (0.97 <= price <= 1.03)         and (t.get("symbol") or "").upper() not in SKIP_SYMBOLS


# ---------- Storage ----------

class WhaleStore:
    def __init__(self, path):
        self.path = str(path)
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS whale_tokens (
                token TEXT PRIMARY KEY, symbol TEXT, name TEXT, decimals INTEGER, icon TEXT,
                price REAL, mcap REAL, supply REAL, holders INTEGER, updated_at TEXT, holders_at TEXT);
            CREATE TABLE IF NOT EXISTS whale_token_prices (
                token TEXT NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY (token, ts));
            CREATE TABLE IF NOT EXISTS whales (
                address TEXT PRIMARY KEY, label TEXT, kind TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS whale_balances (
                address TEXT NOT NULL, token TEXT NOT NULL, ts TEXT NOT NULL, balance REAL NOT NULL,
                share REAL, PRIMARY KEY (address, token, ts));
            CREATE TABLE IF NOT EXISTS whale_transfers (
                tx TEXT NOT NULL, log_index INTEGER NOT NULL, address TEXT NOT NULL, token TEXT NOT NULL,
                symbol TEXT, direction TEXT NOT NULL, amount REAL NOT NULL, usd REAL,
                counterparty TEXT, cp_label TEXT, cp_kind TEXT, ts TEXT NOT NULL,
                PRIMARY KEY (tx, log_index, address));
            CREATE TABLE IF NOT EXISTS whale_sync (
                address TEXT NOT NULL, token TEXT NOT NULL, synced_at TEXT NOT NULL, PRIMARY KEY (address, token));
            CREATE TABLE IF NOT EXISTS address_labels (
                address TEXT PRIMARY KEY, label TEXT, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS whale_signals (
                token TEXT PRIMARY KEY, flagged_at TEXT NOT NULL, price REAL NOT NULL, score INTEGER NOT NULL);
            """)
            try:  # databases from before multi-chain support
                c.execute("ALTER TABLE whale_tokens ADD COLUMN chain TEXT NOT NULL DEFAULT 'ethereum'")
            except sqlite3.OperationalError:
                pass

    def _conn(self):
        return sqlite3.connect(self.path)

    def _rows(self, sql: str, *args) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]

    # tokens
    def save_tokens(self, tokens: list[dict], ts: str, chain: str = CHAIN) -> None:
        with self._conn() as c:
            for t in tokens:
                key = tkey(chain, t["address_hash"])
                c.execute("""INSERT INTO whale_tokens (token, symbol, name, decimals, icon, price, mcap, supply, holders,
                               updated_at, chain)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                             ON CONFLICT(token) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
                               decimals=excluded.decimals, icon=excluded.icon, price=excluded.price, mcap=excluded.mcap,
                               supply=excluded.supply, holders=excluded.holders, updated_at=excluded.updated_at""",
                          (key, t.get("symbol"), t.get("name"), int(t.get("decimals") or 18),
                           t.get("icon_url"), float(t["exchange_rate"]), float(t["circulating_market_cap"]),
                           amount(t.get("total_supply"), t.get("decimals")), int(t.get("holders_count") or 0), ts, chain))
                c.execute("INSERT OR REPLACE INTO whale_token_prices VALUES (?, ?, ?)",
                          (key, ts, float(t["exchange_rate"])))

    def tokens(self) -> dict[str, dict]:
        return {t["token"]: t for t in self._rows("SELECT * FROM whale_tokens")}

    def mark_holders(self, token: str, ts: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE whale_tokens SET holders_at = ? WHERE token = ?", (ts, token))

    def token_prices(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list] = {}
        for r in self._rows("SELECT token, ts, price FROM whale_token_prices ORDER BY token, ts"):
            out.setdefault(r["token"], []).append((r["ts"], r["price"]))
        return out

    # whales & labels
    def upsert_whale(self, address: str, label: str, kind: str, ts: str, manual: bool = False) -> None:
        with self._conn() as c:
            c.execute("""INSERT INTO whales VALUES (?, ?, ?, ?, ?)
                         ON CONFLICT(address) DO UPDATE SET label = COALESCE(NULLIF(excluded.label, ''), whales.label),
                           kind = CASE WHEN whales.manual = 1 THEN whales.kind ELSE excluded.kind END,
                           manual = MAX(whales.manual, excluded.manual)""",
                      (norm(address), label, kind, int(manual), ts))

    def delete_whale(self, address: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM whales WHERE address = ?", (norm(address),))

    def whales(self) -> dict[str, dict]:
        out = {}
        for w in self._rows("SELECT * FROM whales"):
            if re.search(r"safe|proxy", w["label"] or "", re.I):
                w["label"] = ""  # stored before label_of learned to skip contract-type names
            out[w["address"]] = w
        return out

    def save_label(self, address: str, label: str, kind: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO address_labels VALUES (?, ?, ?)", (norm(address), label, kind))

    def labels(self) -> dict[str, dict]:
        return {r["address"]: r for r in self._rows("SELECT * FROM address_labels")}

    # balances & transfers
    def save_balance(self, address: str, token: str, ts: str, balance: float, share: float | None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO whale_balances VALUES (?, ?, ?, ?, ?)",
                      (norm(address), token, ts, balance, share))

    def balances(self) -> dict[tuple[str, str], list[tuple[str, float, float | None]]]:
        out: dict[tuple, list] = {}
        for r in self._rows("SELECT * FROM whale_balances ORDER BY ts"):
            out.setdefault((r["address"], r["token"]), []).append((r["ts"], r["balance"], r["share"]))
        return out

    def save_transfers(self, rows: list[tuple]) -> None:
        with self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO whale_transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def transfers(self, since: str = "") -> list[dict]:
        return self._rows("SELECT * FROM whale_transfers WHERE ts >= ? ORDER BY ts DESC", since)

    def mark_synced(self, address: str, token: str, ts: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO whale_sync VALUES (?, ?, ?)", (norm(address), token, ts))

    def synced(self) -> dict[tuple[str, str], str]:
        return {(r["address"], r["token"]): r["synced_at"] for r in self._rows("SELECT * FROM whale_sync")}

    # signals
    def signals(self) -> dict[str, dict]:
        return {s["token"]: s for s in self._rows("SELECT * FROM whale_signals")}

    def flag(self, token: str, ts: str, price: float, score: int) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO whale_signals VALUES (?, ?, ?, ?)", (token, ts, price, score))


# ---------- Transfers -> rows ----------

def transfer_rows(items: list[dict], whale: str, labels: dict[str, dict], chain: str = CHAIN) -> list[tuple]:
    rows = []
    for x in items:
        tok = x.get("token") or {}
        frm, to = x.get("from") or {}, x.get("to") or {}
        if norm(to.get("hash", "")) == whale:
            direction, cp = "in", frm
        elif norm(frm.get("hash", "")) == whale:
            direction, cp = "out", to
        else:
            continue
        total = x.get("total") or {}
        qty = amount(total.get("value"), total.get("decimals") or tok.get("decimals"))
        price = float(tok.get("exchange_rate") or 0)
        cp_addr = norm(cp.get("hash") or "")
        known = labels.get(cp_addr)
        cp_kind = known["kind"] if known else kind_of_address(cp)
        cp_label = (known or {}).get("label") or label_of(cp)
        rows.append((x.get("transaction_hash") or x.get("tx_hash"), int(x.get("log_index") or 0), whale,
                     tkey(chain, tok.get("address_hash") or tok.get("address") or ""), tok.get("symbol"),
                     direction, qty, qty * price if price else None, cp_addr, cp_label, cp_kind,
                     (x.get("timestamp") or "")[:19] + "Z"))
    return rows


def describe_transfer(t: dict) -> tuple[str, str]:
    """(label, tone) - tone: up (accumulation), down (likely selling), flat."""
    if t["direction"] == "in":
        if t["cp_kind"] == "exchange":
            return "Wypłata z giełdy: akumulacja na własnym portfelu", "up"
        if t["cp_kind"] == "contract":
            return "Zakup przez DEX / kontrakt", "up"
        return "Otrzymał od innego portfela", "flat"
    if t["cp_kind"] == "exchange":
        return "Wpłata na giełdę: możliwa sprzedaż", "down"
    if t["cp_kind"] == "contract":
        return "Sprzedaż przez DEX / kontrakt", "down"
    return "Wysłał do innego portfela", "flat"


# ---------- Analysis ----------

def position_stats(transfers: list[dict], price: float, now: datetime) -> dict:
    """Net market flow of one whale in one token over 7/30 days.

    Only flows with an exchange or a contract (DEX, aggregator) count as buying or
    selling; wallet-to-wallet moves are usually the same owner shuffling funds or OTC,
    so they are tracked separately and left out of the net.
    """
    out = {"in7": 0.0, "out7": 0.0, "in30": 0.0, "out30": 0.0, "buys30": 0, "sells30": 0,
           "cex_in30": 0.0, "cex_out30": 0.0, "moved30": 0.0, "last": None}
    d7, d30 = crypto.iso(now - timedelta(days=7)), crypto.iso(now - timedelta(days=30))
    for t in transfers:
        out["last"] = max(out["last"] or "", t["ts"])
        if t["ts"] < d30:
            continue
        if t["cp_kind"] not in ("exchange", "contract"):
            out["moved30"] += t["amount"]
            continue
        sign = t["direction"]
        out[f"{sign}30"] += t["amount"]
        if t["ts"] >= d7:
            out[f"{sign}7"] += t["amount"]
        if sign == "in":
            out["buys30"] += 1
            if t["cp_kind"] == "exchange":
                out["cex_out30"] += t["amount"]     # withdrawn from an exchange to the whale
        else:
            out["sells30"] += 1
            if t["cp_kind"] == "exchange":
                out["cex_in30"] += t["amount"]      # deposited to an exchange
    out["net7"], out["net30"] = out["in7"] - out["out7"], out["in30"] - out["out30"]
    out["net7_usd"], out["net30_usd"] = out["net7"] * price, out["net30"] * price
    out["accumulating"] = out["net30"] > 0 and out["buys30"] >= 2 and out["cex_in30"] == 0
    out["distributing"] = out["net30"] < 0 and (out["cex_in30"] > 0 or out["sells30"] >= 2)
    return out


def token_score(stats: list[dict], mcap: float) -> tuple[int, list[tuple[str, str]], list[str]]:
    acc = [s for s in stats if s["accumulating"]]
    dist = [s for s in stats if s["distributing"]]
    net30 = sum(s["net30_usd"] for s in stats)
    net7 = sum(s["net7_usd"] for s in stats)
    cex_out = sum(s["cex_out30"] * s["price"] for s in stats)
    cex_in = sum(s["cex_in30"] * s["price"] for s in stats)
    score, reasons, factors = 20, [], []

    def add(key, pts, why):
        nonlocal score
        score += pts
        factors.append(key)
        reasons.append(("+" if pts > 0 else "-" if pts < 0 else "·", why))

    if len(acc) >= 3:
        add("acc3", 30, f"{len(acc)} wieloryby konsekwentnie dokupują (min. 2 zakupy z giełdy lub DEX w 30 dni, bez wpłat na giełdę).")
    elif len(acc) == 2:
        add("acc2", 20, "Dwa wieloryby konsekwentnie dokupują.")
    elif len(acc) == 1:
        add("acc1", 10, "Jeden wieloryb konsekwentnie dokupuje.")
    if dist:
        add("dist", -12 * min(3, len(dist)), f"{len(dist)} wieloryb(y) wyprzedaje, w tym wpłaty na giełdę.")
    if mcap and net30 > 0:
        pct = net30 / mcap
        if pct >= 0.005:
            add("flow_big", 20, f"Napływ netto od wielorybów ${net30 / 1e6:,.1f} mln w 30 dni, czyli {pct:.2%} kapitalizacji.")
        elif pct >= 0.001:
            add("flow_mid", 10, f"Napływ netto od wielorybów ${net30 / 1e6:,.1f} mln w 30 dni ({pct:.2%} kap.).")
    elif net30 < 0:
        add("outflow", -10, f"Odpływ netto od wielorybów ${abs(net30) / 1e6:,.1f} mln w 30 dni.")
    if net7 > 0 and net30 > 0:
        add("recent", 10, "Akumulacja trwa też w ostatnim tygodniu.")
    if cex_out > cex_in and cex_out > 0:
        add("cex_out", 10, f"Wieloryby wypłacają z giełd (${cex_out / 1e6:,.1f} mln) na własne portfele, czyli raczej trzymają.")
    elif cex_in > cex_out:
        add("cex_in", -10, f"Wieloryby wpłacają na giełdy (${cex_in / 1e6:,.1f} mln), więc możliwa sprzedaż.")
    return max(0, min(100, score)), reasons, factors


def price_at_or_after(points: list[tuple[str, float]], ts: str) -> float | None:
    return next((p for t, p in points if t >= ts), None)


def signal_performance(sig: dict, points: list[tuple[str, float]], now: datetime) -> dict:
    out = {"now": None, "h": {}}
    if not points:
        return out
    out["now"] = points[-1][1] / sig["price"] - 1
    t0 = crypto.parse_iso(sig["flagged_at"])
    for days, _ in HORIZONS:
        target = t0 + timedelta(days=days)
        if target <= now:
            p = price_at_or_after(points, crypto.iso(target))
            if p:
                out["h"][days] = p / sig["price"] - 1
    return out


def build(store: WhaleStore, now: datetime | None = None, include_treasury: bool = False) -> dict:
    """Everything the whale pages need, computed from stored data."""
    now = now or crypto.utcnow()
    tokens, whales, balances = store.tokens(), store.whales(), store.balances()
    transfers = store.transfers(crypto.iso(now - timedelta(days=90)))
    by_pair: dict[tuple, list] = {}
    for t in transfers:
        by_pair.setdefault((t["address"], t["token"]), []).append(t)

    positions = []
    for (addr, tok), snaps in balances.items():
        w, t = whales.get(addr), tokens.get(tok)
        if not w or not t or (t["symbol"] or "").upper() in SKIP_SYMBOLS:
            continue
        if w["kind"] in HIDDEN_KINDS and not include_treasury:
            continue
        ts, bal, share = snaps[-1]
        st = position_stats(by_pair.get((addr, tok), []), t["price"], now)
        first = snaps[0]
        positions.append({**st, "address": addr, "token": tok, "symbol": t["symbol"], "price": t["price"],
                          "balance": bal, "usd": bal * t["price"], "share": share, "whale": w,
                          "snap_change": bal / first[1] - 1 if first[1] and len(snaps) > 1 else None,
                          "snap_since": first[0]})

    prices, flagged = store.token_prices(), store.signals()
    token_rows = []
    for tok, t in tokens.items():
        stats = [p for p in positions if p["token"] == tok]
        if not stats or (t["symbol"] or "").upper() in SKIP_SYMBOLS:
            continue
        score, reasons, factors = token_score(stats, t["mcap"])
        level, label = signals.level_of(score)
        if score >= SIGNAL_SCORE and tok not in flagged:
            store.flag(tok, crypto.iso(now), t["price"], score)
            flagged = store.signals()
        sig = flagged.get(tok)
        pts = prices.get(tok, [])
        chain, raw = split_key(tok)
        token_rows.append({
            **t, "chain": chain, "address": raw, "chain_label": CHAINS.get(chain, {}).get("label", chain),
            "explorer": explorer_for(tok), "score": score, "level": level, "label": label, "reasons": reasons, "factors": factors,
            "whales": len(stats), "acc": sum(s["accumulating"] for s in stats),
            "dist": sum(s["distributing"] for s in stats),
            "whale_usd": sum(s["usd"] for s in stats),
            "net7_usd": sum(s["net7_usd"] for s in stats), "net30_usd": sum(s["net30_usd"] for s in stats),
            "signal": sig, "perf": signal_performance(sig, pts, now) if sig else None,
            "ch7": (t["price"] / p7 - 1) if pts and pts[0][0] <= crypto.iso(now - timedelta(days=6, hours=20))
                   and (p7 := price_at_or_after(pts, crypto.iso(now - timedelta(days=7)))) else None,
            "positions": sorted(stats, key=lambda s: -s["usd"]),
        })
    token_rows.sort(key=lambda r: (r["score"], r["net30_usd"]), reverse=True)

    whale_rows = []
    for addr, w in whales.items():
        mine = [p for p in positions if p["address"] == addr]
        if not mine and not w["manual"]:
            continue
        last = max((p["last"] or "" for p in mine), default="")
        whale_rows.append({**w, "usd": sum(p["usd"] for p in mine), "tokens": len(mine),
                           "acc": [p["symbol"] for p in mine if p["accumulating"]],
                           "dist": [p["symbol"] for p in mine if p["distributing"]],
                           "net30_usd": sum(p["net30_usd"] for p in mine), "last": last or None,
                           "positions": sorted(mine, key=lambda p: -p["usd"])})
    whale_rows.sort(key=lambda w: (not w["manual"], -w["usd"]))

    big = []
    for t in transfers:
        if (t["usd"] or 0) >= BIG_TX_USD and t["address"] in whales:
            text, tone = describe_transfer(t)
            big.append({**t, "text": text, "tone": tone, "whale": whales[t["address"]],
                        "explorer": explorer_for(t["token"])})
    return {"tokens": token_rows, "whales": whale_rows, "big": big, "positions": positions}


# ---------- Background runner ----------

class WhaleScanner:
    def __init__(self, store: WhaleStore, client_factory):
        self.store = store
        self.client_factory = client_factory
        self.lock = threading.Lock()
        self.running = False
        self.last_run: str | None = None
        self.last_error: str | None = None
        self.last_new = 0

    def run_once(self, now: datetime | None = None) -> None:
        if not self.lock.acquire(blocking=False):
            return
        self.running = True
        try:
            now = now or crypto.utcnow()
            before = set(self.store.whales())
            errors = []
            for chain in active_chains():
                if chain == "ethereum":
                    budget = (UNIVERSE, HOLDERS_PER_RUN, TRANSFERS_PER_RUN)
                elif CHAINS[chain].get("helius"):
                    budget = (SOLANA_UNIVERSE, SOLANA_HOLDERS, SOLANA_TRANSFERS)
                else:
                    budget = (EXTRA_UNIVERSE, EXTRA_HOLDERS, EXTRA_TRANSFERS)
                try:
                    self._run_chain(self._client(chain), chain, now, *budget)
                except Exception as exc:  # one chain failing (rate limit, outage) must not stop the others
                    errors.append(f"{chain}: {safety.safe_error(exc)}")
            self.last_new = len(set(self.store.whales()) - before)
            build(self.store, now)  # flags new accumulation signals at today's price
            self.last_error = "; ".join(errors) or None
        except Exception as exc:
            self.last_error = safety.safe_error(exc)
        finally:
            self.last_run = datetime.now().strftime("%H:%M:%S")
            self.running = False
            self.lock.release()

    def _client(self, chain: str):
        try:
            return self.client_factory(chain)
        except TypeError:  # factories that take no chain (tests, single-chain setups)
            return self.client_factory()

    def _run_chain(self, client, chain: str, now: datetime, universe_n: int, holders_n: int,
                   transfers_n: int) -> None:
        ts = crypto.iso(now)
        # 1. Universe + price snapshot (also the price history used to score whale signals).
        universe = [t for t in client.tokens(pages=max(1, universe_n // 50 + 2)) if tradable_token(t)][:universe_n]
        self.store.save_tokens(universe, ts, chain)
        tokens = self.store.tokens()
        whales_before = set(self.store.whales())
        in_universe = {tkey(chain, u["address_hash"]) for u in universe}

        # 2. Top holders of the least recently refreshed tokens.
        order = sorted((t for t in tokens.values() if t["token"] in in_universe), key=lambda t: t["holders_at"] or "")
        for t in order[:holders_n]:
            raw = split_key(t["token"])[1]
            try:
                holders = client.holders(raw)
            except Exception:
                continue
            # Several wallets with the exact same balance = vesting / team allocation, not traders.
            sizes: dict[float, int] = {}
            for h in holders:
                if kind_of_address(h.get("address") or {}) in ("wallet", "safe"):
                    v = round(amount(h.get("value"), t["decimals"]))
                    sizes[v] = sizes.get(v, 0) + 1
            for h in holders:
                addr = h.get("address") or {}
                a = norm(addr.get("hash") or "")
                bal = amount(h.get("value"), t["decimals"])
                kind = kind_of_address(addr)
                if a in BURN or a == raw:
                    continue
                if kind in ("exchange", "contract"):
                    self.store.save_label(a, label_of(addr), kind)  # teaches counterparty classification
                    continue
                share = bal / t["supply"] if t["supply"] else None
                if bal * t["price"] < WHALE_MIN_USD and a not in whales_before:
                    continue
                if (share is not None and share >= TREASURY_SHARE) or (bal >= 1 and sizes.get(round(bal), 0) >= 3):
                    kind = "treasury"
                self.store.upsert_whale(a, label_of(addr), kind, ts)
                self.store.save_balance(a, t["token"], ts, bal, share)
            self.store.mark_holders(t["token"], ts)
        if CHAINS[chain].get("helius"):
            self._mark_custody(chain)

        # 3. Transfers of the biggest unsynced whale positions on this chain (how the position is being built).
        whales, synced, labels = self.store.whales(), self.store.synced(), self.store.labels()
        stale = crypto.iso(now - timedelta(hours=TRANSFER_SYNC_HOURS))
        latest: dict[tuple, float] = {}
        for (a, tok), snaps in self.store.balances().items():
            if a in whales and whales[a]["kind"] not in HIDDEN_KINDS and tok in tokens and split_key(tok)[0] == chain:
                latest[(a, tok)] = snaps[-1][1] * tokens[tok]["price"]
        todo = sorted((k for k in latest if synced.get(k, "") < stale), key=lambda k: -latest[k])
        everything = "*" if chain == "ethereum" else f"*{chain}"   # sync marker for manual wallets
        manual = [(a, everything) for a, w in whales.items()
                  if w["manual"] and synced.get((a, everything), "") < stale]
        for a, tok in (manual + todo)[:transfers_n]:
            if tok == everything:
                self._refresh_portfolio(client, a, ts, tokens, chain)
            try:
                items = client.transfers(a, None if tok == everything else split_key(tok)[1])
            except Exception:
                continue
            self.store.save_transfers(transfer_rows(items, a, labels, chain))
            self.store.mark_synced(a, tok, ts)

    def _mark_custody(self, chain: str) -> None:
        """No free exchange labels on Solana: a wallet among the top holders of many tokens is custody."""
        counts: dict[str, set] = {}
        for (a, tok) in self.store.balances():
            if split_key(tok)[0] == chain:
                counts.setdefault(a, set()).add(tok)
        whales = self.store.whales()
        for a, toks in counts.items():
            w = whales.get(a)
            if w and not w["manual"] and len(toks) >= CUSTODY_TOKENS and w["kind"] != "custody":
                self.store.upsert_whale(a, w["label"] or "", "custody", w["first_seen"])
                self.store.save_label(a, w["label"] or "giełda / custody", "exchange")

    def _refresh_portfolio(self, client, address: str, ts: str, tokens: dict, chain: str = CHAIN) -> None:
        try:
            items = client.portfolio(address)
        except Exception:
            return
        for item in items:
            tok = tkey(chain, (item.get("token") or {}).get("address_hash") or "")
            if tok in tokens:
                self.store.save_balance(address, tok, ts, amount(item.get("value"), tokens[tok]["decimals"]), None)

    def add_wallet(self, address: str, label: str) -> None:
        """Manually tracked wallet (e.g. a known fund from Arkham / X): pull its holdings right away."""
        address = norm(address)
        ts = crypto.iso(crypto.utcnow())
        tokens = self.store.tokens()
        for i, chain in enumerate(active_chains()):
            try:
                client = self._client(chain)
            except Exception:
                continue
            if i == 0:
                try:
                    label = label or label_of(client.address(address))
                except Exception:
                    pass
                self.store.upsert_whale(address, label, "manual", ts, manual=True)
            self._refresh_portfolio(client, address, ts, tokens, chain)

    def trigger(self) -> None:
        threading.Thread(target=self.run_once, daemon=True).start()

    def loop(self, interval_s: int) -> None:
        while True:
            self.run_once()
            time.sleep(interval_s)


"""Headlines explaining a crypto move: Google News RSS search (free, no key).

CryptoPanic dropped its free API in 2026, so news comes from a Google News search
for the project's name, limited to the last few days. Answers "why is this moving?"
on the coin page and in Telegram alerts.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

RSS_URL = "https://news.google.com/rss/search"
MAX_ITEMS = 6
# A headline counts if it carries the ticker in capitals, or names the coin next to crypto
# words. Names like "Edel", "Sea" or "Magic" are also people and plain words, so price-move
# words ("surges", "jumps") only count for longer, distinctive names like "Starknet".
CRYPTO_WORDS = re.compile(r"crypto|token|coin\b|coins\b|altcoin|memecoin|blockchain|defi|airdrop|unlock|staking|"
                          r"mainnet|testnet|on-?chain|web3|nft|dex\b|tvl|layer[- ]?[12]|\bl[12]\b|ethereum|solana|"
                          r"bitcoin|binance|coinbase|upbit|bybit|okx|listing|delist|whale", re.I)
MOVE_WORDS = re.compile(r"surge|soar|jump|rall|plunge|crash|pump|dump|tumble|spike|slump|rebound|breakout", re.I)
DISTINCTIVE_LEN = 6
JUNK = re.compile(r"^convert\b|price prediction|\bto (usd|eur|aud|gbp|inr|cad|jpy)\b|exchange rate|"
                  r"how to buy|where to buy|live price|price today|\(\w+/usd\)", re.I)


class NewsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (insider-radar)"
        self._last = 0.0

    def search(self, name: str, symbol: str, days: int = 3) -> list[dict]:
        wait = 1.0 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(RSS_URL, timeout=20, params={
            "q": query(name, symbol, days), "hl": "en-US", "gl": "US", "ceid": "US:en"})
        self._last = time.monotonic()
        resp.raise_for_status()
        return parse(resp.content, name, symbol)


def query(name: str, symbol: str, days: int) -> str:
    # Quoted name + "crypto" keeps "Sea", "Amp", "Magic" from matching unrelated news.
    return f'"{name}" (crypto OR token OR {symbol.upper()}) when:{days}d'


def parse(xml_bytes: bytes, name: str, symbol: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    name_re = re.compile(rf"\b{re.escape(name)}\b", re.I)
    ticker_re = re.compile(rf"(?<![A-Za-z]){re.escape(symbol.upper())}\b") if len(symbol) >= 3 else None
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        named = name_re.search(title)
        about_coin = (ticker_re and ticker_re.search(title)) or (named and CRYPTO_WORDS.search(title)) or \
            (named and len(name) >= DISTINCTIVE_LEN and MOVE_WORDS.search(title))
        if not about_coin or JUNK.search(title):  # the search also matches article bodies
            continue
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            ts = ""
        out.append({"title": title, "source": source, "url": it.findtext("link") or "", "ts": ts})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:MAX_ITEMS]

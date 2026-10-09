"""Error text that is safe to store and publish.

Scanner errors end up in scan_status.json (public data branch) and on the public
Pages site. requests puts the full URL - query string included - into exception
messages, so an API key passed as ?apikey=... would leak. Strip URLs' query strings
and anything that looks like a key.
"""

from __future__ import annotations

import re

import requests

KEY_PATTERNS = [
    re.compile(r"proapi_[A-Za-z0-9_\-]+"),                      # Blockscout PRO
    re.compile(r"CG-[A-Za-z0-9]{10,}"),                           # CoinGecko
    re.compile(r"(?i)(api[_-]?key|apikey|token|crumb)=[^&\s]+"),  # any key-looking parameter
]


def scrub(text: str) -> str:
    text = re.sub(r"(https?://[^\s?]+)\?\S*", r"\1", text)  # drop query strings from URLs
    for pattern in KEY_PATTERNS:
        text = pattern.sub("***", text)
    return text


def safe_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        url = exc.response.url.split("?")[0]
        return scrub(f"HTTP {exc.response.status_code} ({url})")
    return scrub(f"{type(exc).__name__}: {exc}")


def extract_key(value: str, pattern: str = r"proapi_[A-Za-z0-9_\-]+") -> str:
    """Accept a secret pasted as a whole example URL: keep just the key part."""
    found = re.search(pattern, value or "")
    return found.group(0) if found else (value or "").strip()

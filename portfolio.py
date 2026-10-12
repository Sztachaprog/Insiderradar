"""Paper portfolio: the user's capital split into stocks / leveraged crypto / spot crypto, each part
trading the app's own signals with simple, honest sizing.

collect() turns the app's signals into one trade format. simulate() is the reference implementation
of the sizing rules; the page runs the same rules in the browser (static/portfolio.js), because the
capital and the split are the viewer's own settings, kept in their browser. tests/test_portfolio.py
checks that both give the same numbers.

Trade: {"b": bucket, "src": source key, "name": source name, "sym": symbol, "in": ISO entry,
        "out": ISO exit or None while open, "ret": return so far / final (after fees, before leverage),
        "sl": stop for leveraged trades, "score": signal score, "url": link}
"""

from __future__ import annotations

from datetime import timedelta

import crypto
import strategy

BUCKETS = ("stocks", "lev", "spot")
STOCK_HOLD_SESSIONS = 20         # ~1 month, the longest horizon the stock results track
RADAR_HOLD_H = 168               # spot radar moves: 7 days
WHALE_HOLD_D = 30                # whale spot signals: 30 days
SPOT_FEE = 0.002                 # round trip on spot crypto
STOCK_FEE = 0.002                # round trip incl. currency conversion
US_CLOSE = "T20:00:00Z"          # stock prices are daily closes

DEFAULTS = {
    "capital": 10000, "split": {"stocks": 60, "lev": 30, "spot": 10},
    "lev": strategy.LEVERAGE, "risk": 2,                # % of the leveraged part lost when one stop hits
    "maxPos": {"stocks": 10, "spot": 5}, "minScore": {"stocks": 0, "spot": 0},
    "strategies": None, "start": "",
    "spotSources": ["whales"],        # the spot radar averages a loss; the viewer can switch it on
}


def collect(stock_rows: list[dict], radar_rows: list[dict], whale_tokens: list[dict],
            lev_positions: list[dict], created: dict[str, str], now) -> list[dict]:
    """created: saved strategy key -> creation time (their live record starts there)."""
    out = []
    for r in stock_rows:
        perf, base_date = r.get("perf"), r.get("base_date")
        if not perf or not base_date or perf.get("now") is None:
            continue
        path = perf["path"]
        done = len(path) > STOCK_HOLD_SESSIONS
        out.append({"b": "stocks", "src": "insiders", "name": "Insiderzy", "sym": r["ticker"],
                    "in": base_date + US_CLOSE, "out": path[STOCK_HOLD_SESSIONS][0] + US_CLOSE if done else None,
                    "ret": (path[STOCK_HOLD_SESSIONS][1] if done else perf["now"]) - STOCK_FEE,
                    "sl": None, "score": r["signal"]["score"], "url": r.get("url", "")})
    for r in radar_rows:
        perf = r.get("perf") or {}
        if r.get("origin", "live") != "live" or perf.get("now") is None:
            continue
        h = (perf.get("h") or {}).get(RADAR_HOLD_H)
        out.append({"b": "spot", "src": "radar", "name": "Radar krypto", "sym": r["symbol"], "in": r["detected_at"],
                    "out": crypto.iso(crypto.parse_iso(r["detected_at"]) + timedelta(hours=RADAR_HOLD_H)) if h else None,
                    "ret": (h["ret"] if h else perf["now"]) - SPOT_FEE, "sl": None, "score": r["signal"]["score"],
                    "url": r.get("url", "")})
    for t in whale_tokens:
        sig, perf = t.get("signal"), t.get("perf") or {}
        if not sig or perf.get("now") is None:
            continue
        h = (perf.get("h") or {}).get(WHALE_HOLD_D)
        out.append({"b": "spot", "src": "whales", "name": "Wieloryby", "sym": t["symbol"] or "?", "in": sig["flagged_at"],
                    "out": crypto.iso(crypto.parse_iso(sig["flagged_at"]) + timedelta(days=WHALE_HOLD_D))
                    if h is not None else None,
                    "ret": (h if h is not None else perf["now"]) - SPOT_FEE, "sl": None, "score": t.get("score"),
                    "url": t.get("url", "")})
    for t in lev_positions:
        if t["row"].get("origin", "live") != "live" or t["net"] is None or t["status"] not in ("open", "closed"):
            continue
        if t["entry_at"] < created.get(t["strategy_key"], ""):
            continue       # a saved strategy trades live only from its creation
        out.append({"b": "lev", "src": t["strategy_key"], "name": t["strategy_name"], "sym": t["row"]["symbol"],
                    "in": t["entry_at"], "out": t["exit_at"] if t["status"] == "closed" else None, "ret": t["net"],
                    "sl": t["full"]["sl"], "score": t["row"]["signal"]["score"], "url": t.get("url", "")})
    out.sort(key=lambda t: (t["in"], t["b"], t["src"], t["sym"]))
    return out


def _allowed(t: dict, s: dict) -> bool:
    if t["ret"] is None or t["in"] < s["start"]:
        return False
    if t["b"] == "lev":
        return s["strategies"] is None or t["src"] in s["strategies"]
    if t["b"] == "spot" and t["src"] not in s["spotSources"]:
        return False
    return (t["score"] or 0) >= s["minScore"][t["b"]]


def simulate(trades: list[dict], settings: dict, now_iso: str) -> dict:
    """Run every bucket through its trades in time order. Leveraged: the position is sized so a stop
    loses `risk`% of the bucket, margin = position / leverage, a loss never exceeds the margin
    (liquidation), one position per token. Stocks / spot: equal slots of bucket / maxPos.
    A signal that finds no free money (or slot) is skipped, as it would be for real."""
    s = {**DEFAULTS, **settings}
    result = {"buckets": {}, "open": [], "closed": []}
    for b in BUCKETS:
        cap = s["capital"] * s["split"][b] / 100
        cash, open_, closed, skipped, curve = cap, [], [], 0, []

        def close_until(t_iso):
            nonlocal cash
            due = sorted((p for p in open_ if p["out"] is not None and p["out"] <= t_iso),
                         key=lambda p: (p["out"], p["in"], p["src"], p["sym"]))
            for p in due:
                open_.remove(p)
                cash += p["margin"] + p["pnl"]
                closed.append(p)
                curve.append([p["out"], cash + sum(q["margin"] for q in open_)])

        for t in (t for t in trades if t["b"] == b and _allowed(t, s)):
            close_until(t["in"])
            equity = cash + sum(p["margin"] for p in open_)
            if b == "lev":
                if any(p["sym"] == t["sym"] for p in open_):
                    skipped += 1
                    continue
                size = equity * s["risk"] / 100 / t["sl"]
                margin = size / s["lev"]
                pnl = max(size * t["ret"], -margin)
            else:
                if len(open_) >= s["maxPos"][b]:
                    skipped += 1
                    continue
                size = margin = equity / s["maxPos"][b]
                pnl = margin * t["ret"]
            if margin <= 0 or margin > cash + 1e-9:
                skipped += 1
                continue
            cash -= margin
            open_.append({**t, "size": size, "margin": margin, "pnl": pnl})
        close_until(now_iso)
        committed = sum(p["margin"] for p in open_)
        unrealized = sum(p["pnl"] for p in open_)
        wins = [p for p in closed if p["pnl"] > 0]
        result["buckets"][b] = {
            "start": cap, "equity": cash + committed + unrealized, "cash": cash, "committed": committed,
            "realized": sum(p["pnl"] for p in closed), "unrealized": unrealized, "trades": len(closed),
            "open": len(open_), "skipped": skipped, "win_rate": len(wins) / len(closed) if closed else None,
            "curve": curve,
        }
        result["open"] += open_
        result["closed"] += closed
    return result

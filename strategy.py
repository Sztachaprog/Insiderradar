"""Paper-trading lab for crypto day trades on radar events.

A strategy = entry filter (what kind of move, how big a token, which score) + side
(long or short) + exit rules (take-profit, stop-loss, max hold). Each matching event is
simulated on the stored price path from the moment of detection: enter at the detection
price, exit at the first bar that hits TP/SL or when the hold time runs out, minus fees.

Paths are snapshots (every scan, ~15 min) plus hourly history for gaps, so intraday
spikes between snapshots are invisible: TP fills at the TP level (a resting limit
order), SL fills at the observed price (a stop can slip), which keeps results conservative.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta

import crypto

# Defaults for every parameter; a strategy only overrides what it cares about.
DEFAULTS = {
    "triggers": [],          # any of these triggers ([] = all)
    "exclude": [],           # none of these
    "min_score": 0, "max_score": 100,
    "min_mcap": 0, "max_mcap": 0,             # 0 = no limit
    "min_ch24": None, "max_ch24": None,       # 24h change at detection, e.g. 0.25 = +25%
    "min_vol": None, "max_vol": None,         # volume / market cap at detection
    "tp": 0.10, "sl": 0.05, "hold_h": 24,
    "side": "long",          # "short": profit when the price falls (TP below the entry, SL above)
    "tps": None,             # take-profit ladder, e.g. [0.05, 0.10, 0.20]; None = the single tp
    "parts": None,           # share closed at each level, e.g. [0.2, 0.4, 0.4]; None = equal parts
    "be": True,              # ladder: after TP1 the stop moves to the entry
    "trail": True,           # ladder: after TP2 the stop moves to TP1 (after TP3 to TP2, ...)
    "cex_only": False,       # only tokens trading on Binance: leverage exists only there, and so do clean candles
}
# Parameters added after trades were journaled: left out of the version while at their default,
# so adding one never re-versions (and empties) the journals of existing strategies.
ADDED_LATER = ("side", "tps", "parts", "be", "trail", "cex_only")
REFERENCE_ONLY = {"all"}   # baseline "buy every signal": compared against, not listed as positions
FEE = 0.005      # round trip: ~0.1% exchange fee per side + slippage on small caps
LEVERAGE = 3     # the most the user trades with: results are also shown per $ of margin at this leverage
MAX_SL = 0.20    # liquidation at 3x is ~30% away; a wider stop could be beaten to it by a gap
STAKE = 100      # $ per trade for the equity curve

PRESETS = [
    ("all", "Wszystko (punkt odniesienia)",
     "Kupuje każdy wykryty ruch. Każda inna strategia powinna wypaść lepiej niż ta.", {}),
    ("accumulation", "Akumulacja wolumenu",
     "Obrót rośnie (0,6–2× kapitalizacji), a cena jeszcze stoi (|24h| < 15%). Gra na to, że ruch dopiero nadejdzie.",
     {"triggers": ["volume"], "min_vol": 0.6, "max_vol": 2, "min_ch24": -0.15, "max_ch24": 0.15}),
    ("rebound", "Odbicie po zrzucie",
     "Token spadł o 25% lub więcej w 24h. Gra na odbicie technicznie wyprzedanego tokena.",
     {"triggers": ["dump24"], "max_mcap": 500e6, "tp": 0.12, "sl": 0.08}),
    ("chase", "Gonienie pompy (kontrolna)",
     "Kupuje to, co już urosło o 25% lub więcej. Pierwsze dane sugerują, że to traci, a ta strategia ma to potwierdzić albo obalić.",
     {"triggers": ["pump24"], "tp": 0.15, "sl": 0.07}),
    ("high_score", "Wysoka ocena",
     "Tylko ruchy z oceną potencjału 65+. Sprawdza, czy ocena sama w sobie coś daje.",
     {"min_score": 65}),
    ("micro", "Mikro-capy bez pompy",
     "Tokeny poniżej $30 mln, bez skrajnego wolumenu (< 2× kap.) i bez parabol (24h < +50%).",
     {"max_mcap": 30e6, "max_vol": 2, "max_ch24": 0.5, "tp": 0.15, "sl": 0.07}),
    ("fade_pump", "Short po pompie",
     "Odwrotność gonienia pompy: gra na spadek tokena, który urósł o 25% lub więcej w 24h. "
     "Kupowanie takich ruchów systematycznie traci, więc sprawdzamy, czy zarabia granie przeciwko nim.",
     {"triggers": ["pump24"], "side": "short", "tp": 0.10, "sl": 0.10}),
]

# Leveraged strategies trade only Binance tokens: DEX-only tokens can't be traded with leverage, and their
# data (sparse or mismatched pools, rug pulls between scans) made up the worst "trades".
PRESETS = [(k, n, d, {**p, "cex_only": True}) for k, n, d, p in PRESETS]

# Every preset also runs with profit taking, on the same entries, so both exit plans meet the same signals.
TWIN_SUFFIX = "_tp3"
PROFIT_TAKING_PARTS = [0.2, 0.4, 0.4]


def with_profit_taking(key: str, name: str, desc: str, params: dict) -> tuple:
    """TP1 / TP2 / TP3 at 1x / 2x / 3x the stop, closing 20 / 40 / 40% of the position;
    the stop moves to the entry after TP1 and to TP1 after TP2."""
    sl = {**DEFAULTS, **params}["sl"]
    return (key + TWIN_SUFFIX, name + " + branie zysków",
            "Te same wejścia co strategia bez dopisku, ale zysk brany po kawałku: 20% na TP1 (1× stop), 40% na TP2 (2×), "
            "reszta na TP3 (3×). Po TP1 stop idzie na wejście, po TP2 na TP1.",
            {**params, "tps": [round(sl, 4), round(2 * sl, 4), round(3 * sl, 4)], "parts": PROFIT_TAKING_PARTS})


PRESETS += [with_profit_taking(*p) for p in list(PRESETS) if p[0] not in REFERENCE_ONLY | {"chase"}]

FORM_FIELDS = {  # name: (cast, scale applied to the form value)
    "min_score": (int, 1), "max_score": (int, 1),
    "min_mcap": (float, 1e6), "max_mcap": (float, 1e6),
    "min_ch24": (float, 0.01), "max_ch24": (float, 0.01),
    "min_vol": (float, 1), "max_vol": (float, 1),
    "tp": (float, 0.01), "sl": (float, 0.01), "hold_h": (int, 1),
}
SIDES = {"long": "long (wzrost)", "short": "short (spadek)"}


def params_from_form(form) -> dict:
    """Form uses human units: mcap in $ mln, changes/TP/SL in %."""
    p = {}
    for name, (cast, scale) in FORM_FIELDS.items():
        raw = (form.get(name) or "").strip().replace(",", ".")
        if raw:
            try:
                value = float(raw) * scale
            except ValueError:
                continue
            p[name] = int(value) if cast is int else value
    for name in ("triggers", "exclude"):
        vals = form.getlist(name) if hasattr(form, "getlist") else form.get(name, [])
        if vals:
            p[name] = [v for v in vals if v in crypto.TRIGGER_LABELS]
    if form.get("side") == "short":
        p["side"] = "short"
    if form.get("cex_only"):
        p["cex_only"] = True
    if "sl" in p:
        p["sl"] = min(p["sl"], MAX_SL)   # a wider stop would be beaten by liquidation at our leverage
    return p


def form_values(params: dict) -> dict:
    """Inverse of params_from_form, to prefill the editor."""
    p = {**DEFAULTS, **params}
    out = {}
    for name, (cast, scale) in FORM_FIELDS.items():
        v = p.get(name)
        if v is None or (name in ("min_mcap", "max_mcap") and not v):
            out[name] = ""
        else:
            out[name] = f"{v / scale:g}" if cast is float else str(v)
    out["triggers"], out["exclude"], out["side"] = p["triggers"], p["exclude"], p["side"]
    out["cex_only"] = p["cex_only"]
    return out


def describe(params: dict) -> list[str]:
    p = {**DEFAULTS, **params}
    parts = (["SHORT"] if p["side"] == "short" else []) + (["tylko Binance"] if p["cex_only"] else [])
    if p["triggers"]:
        parts.append("ruch: " + " lub ".join(crypto.TRIGGER_LABELS[t] for t in p["triggers"]))
    if p["exclude"]:
        parts.append("bez: " + ", ".join(crypto.TRIGGER_LABELS[t] for t in p["exclude"]))
    if p["min_score"] or p["max_score"] < 100:
        parts.append(f"ocena {p['min_score']}–{p['max_score']}")
    if p["min_mcap"] or p["max_mcap"]:
        parts.append(f"kap. {p['min_mcap'] / 1e6:g}–{p['max_mcap'] / 1e6:g} mln $" if p["max_mcap"]
                     else f"kap. ≥ {p['min_mcap'] / 1e6:g} mln $")
    if p["min_ch24"] is not None or p["max_ch24"] is not None:
        lo = f"{p['min_ch24']:+.0%}" if p["min_ch24"] is not None else "−∞"
        hi = f"{p['max_ch24']:+.0%}" if p["max_ch24"] is not None else "+∞"
        parts.append(f"24h {lo} … {hi}")
    if p["min_vol"] is not None or p["max_vol"] is not None:
        parts.append(f"wolumen {p['min_vol'] or 0:g}–{p['max_vol'] if p['max_vol'] is not None else '∞'}× kap.")
    up, down = ("−", "+") if p["side"] == "short" else ("+", "−")
    if p["tps"]:
        tps, shares = ladder(p)
        moves = (["po TP1 SL na wejście"] if p["be"] else []) + (["po TP2 na TP1"] if p["trail"] and len(tps) > 2 else [])
        parts.append("TP " + " / ".join(f"{up}{x:.0%}" for x in tps) + " (" + "/".join(f"{x * 100:.0f}" for x in shares)
                     + "%)" + f" · SL {down}{p['sl']:.0%}" + (" (" + ", ".join(moves) + ")" if moves else "")
                     + f" · max {p['hold_h']} h")
    else:
        parts.append(f"TP {up}{p['tp']:.0%} · SL {down}{p['sl']:.0%} · max {p['hold_h']} h")
    return parts


def levels(entry: float, params: dict) -> tuple[float, float]:
    """(take-profit price, stop-loss price) for a position opened at entry."""
    p = {**DEFAULTS, **params}
    tp = ladder(p)[0][-1]                       # with a ladder: the last target
    if p["side"] == "short":
        return entry * (1 - tp), entry * (1 + p["sl"])
    return entry * (1 + tp), entry * (1 - p["sl"])


def matches(row: dict, params: dict) -> bool:
    """row: a crypto view row (build_crypto_rows) - everything known at detection time."""
    p = {**DEFAULTS, **params}
    if p["cex_only"] and row.get("venue") != "binance":
        return False
    trig = set(row["triggers"])
    if p["triggers"] and not trig & set(p["triggers"]):
        return False
    if trig & set(p["exclude"]):
        return False
    score = row["signal"]["score"]
    if not p["min_score"] <= score <= p["max_score"]:
        return False
    if row["mcap"] < p["min_mcap"] or (p["max_mcap"] and row["mcap"] > p["max_mcap"]):
        return False
    if p["min_ch24"] is not None and row["ch24"] < p["min_ch24"]:
        return False
    if p["max_ch24"] is not None and row["ch24"] > p["max_ch24"]:
        return False
    vol = row["vol_ratio"] or 0
    if p["min_vol"] is not None and vol < p["min_vol"]:
        return False
    if p["max_vol"] is not None and vol > p["max_vol"]:
        return False
    return True


def _bars(points: list[tuple]):
    """Candles are (ts, open, high, low, close); price snapshots (ts, price) become flat bars."""
    for p in points:
        yield (p[0], p[1], p[1], p[1], p[1], False) if len(p) == 2 else (*p[:5], True)


def simulate(row: dict, points: list[tuple], params: dict, now, fee: float = FEE) -> dict:
    """Enter at the price recorded at detection, then walk the price path.

    Exits: a ladder of take-profits (tps) closes part of the position at each level (parts); after TP1
    the stop moves to the entry (be), after TP2 to TP1 (trail), and so on. A single tp is a ladder of
    one level closing everything. Long: a level is hit when a bar's high reaches it, the stop when its
    low does; short: the mirror image. Within one bar the stop is checked first (the cautious reading);
    a stop moved by a take-profit applies from the next bar. A bar that opens beyond a level (a gap)
    fills at its open. Otherwise the rest is closed when the hold time ends.
    peak / trough are the price extremes during the trade, whichever the side."""
    p = {**DEFAULTS, **params}
    short = p["side"] == "short"
    d = -1 if short else 1
    t0 = crypto.parse_iso(row["detected_at"])
    deadline = t0 + timedelta(hours=p["hold_h"])
    base = row["base_price"]
    tps, parts = ladder(p)
    targets = [base * (1 + d * x) for x in tps]
    stop = base * (1 - d * p["sl"])
    trade = {"row": row, "entry": base, "entry_at": row["detected_at"], "exit": None, "exit_at": None,
             "reason": None, "gross": None, "net": None, "status": "open", "peak": 0.0, "trough": 0.0,
             "source": "candles" if points and len(points[0]) > 2 else "snapshots", "side": p["side"],
             "tp_hits": 0}
    realized, left = 0.0, 1.0           # return already banked by partial exits, share still open
    last = None

    def finish(price: float, ts: str, reason: str) -> dict:
        gross = realized + left * d * (price / base - 1)
        trade.update(exit=price, exit_at=ts, reason=reason, status="closed", gross=gross, net=gross - fee)
        return trade

    def time_exit(after: tuple | None):
        # Sell the rest at the deadline: use the bar closest to it, if one is within 2 h.
        near = timedelta(hours=2)
        if last and crypto.parse_iso(last[0]) >= deadline - near:
            return finish(last[1], last[0], "czas")
        if after and crypto.parse_iso(after[0]) - deadline <= near:
            return finish(after[1], after[0], "czas")
        trade["status"] = "no_data"
        return trade

    for ts, o, h, low, c, is_candle in _bars(points):
        if ts <= row["detected_at"]:
            continue
        if crypto.parse_iso(ts) > deadline:
            return time_exit((ts, o))
        trade["peak"] = max(trade["peak"], h / base - 1)
        trade["trough"] = min(trade["trough"], low / base - 1)
        if (h >= stop) if short else (low <= stop):
            fill = (max(o, stop) if short else min(o, stop)) if is_candle else (h if short else low)
            hits = trade["tp_hits"]
            return finish(fill, ts, f"stop po TP{hits}" if hits else "stop-loss")
        while trade["tp_hits"] < len(targets):
            k = trade["tp_hits"]
            level = targets[k]
            if not ((low <= level) if short else (h >= level)):
                break
            fill = (min(o, level) if short else max(o, level)) if is_candle else level
            realized += parts[k] * d * (fill / base - 1)
            left -= parts[k]
            trade["tp_hits"] = k + 1
            if k + 1 == len(targets):
                left = 0.0
                return finish(fill, ts, "take-profit")
            if p["be"] and k == 0:
                stop = base                                   # nothing to lose any more
            if p["trail"] and k >= 1:
                stop = targets[k - 1]                         # after TP2 the stop sits at TP1, and so on
        last = (ts, c)
    if now >= deadline:
        return time_exit(None)
    if last:  # still running: mark to market
        trade["gross"] = realized + left * d * (last[1] / base - 1)
        trade["net"] = trade["gross"] - fee
    return trade


def ladder(params: dict) -> tuple[list[float], list[float]]:
    """(take-profit levels, share of the position closed at each); one level closes everything."""
    p = {**DEFAULTS, **params}
    tps = list(p["tps"] or [p["tp"]])
    parts = list(p["parts"] or [1 / len(tps)] * len(tps))
    return tps, parts


def gross_return(trade: dict, price: float) -> float:
    """Return of the position at this price before fees (a short gains when the price falls)."""
    move = price / trade["entry"] - 1
    return -move if trade.get("side") == "short" else move


def backtest(rows: list[dict], prices: dict, params: dict, now, fee: float = FEE, stake: float = STAKE,
             paths: dict | None = None) -> dict:
    """paths: signal id -> candles (preferred); signals without candles fall back to price snapshots."""
    paths = paths or {}
    trades = [simulate(r, paths.get(r["id"]) or prices.get(r["coin_id"], []), params, now, fee)
              for r in rows if matches(r, params)]
    return summarize(trades, stake)


def summarize(trades: list[dict], stake: float = STAKE) -> dict:
    closed = sorted([t for t in trades if t["status"] == "closed"], key=lambda t: t["exit_at"])
    open_ = [t for t in trades if t["status"] == "open"]
    nets = [t["net"] for t in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
    equity, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in closed:
        equity += stake * t["net"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        curve.append((t["exit_at"], round(equity, 2)))
    return {
        "trades": sorted(trades, key=lambda t: t["entry_at"], reverse=True),
        "matched": len(trades), "closed": len(closed), "open": len(open_),
        "no_data": sum(t["status"] == "no_data" for t in trades),
        "win_rate": len(wins) / len(nets) if nets else None,
        "avg": sum(nets) / len(nets) if nets else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "pnl": equity, "max_dd": dd, "curve": curve,
        "worst": min(nets) if nets else None,
        "open_pnl": sum(stake * t["net"] for t in open_ if t["net"] is not None),
        "tp_hits": sum(t["reason"] == "take-profit" for t in closed),
        "sl_hits": sum(t["reason"] == "stop-loss" for t in closed),
    }


# ---------- Trade journal ----------
# Every simulated trade is written down. Once a trade is final it never changes, even if the
# strategy's rules are edited later (new rules = new version = a separate journal).

def version(params: dict) -> str:
    full = {**DEFAULTS, **params}
    for k in ADDED_LATER:
        if full[k] == DEFAULTS[k]:
            del full[k]
    return hashlib.sha1(json.dumps(full, sort_keys=True).encode()).hexdigest()[:8]


class Journal:
    COLUMNS = ("strategy", "version", "event_id", "entry_at", "entry", "exit_at", "exit", "reason", "gross",
               "net", "peak", "trough", "status", "source", "final", "updated_at")

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS sim_trades (
                strategy TEXT NOT NULL, version TEXT NOT NULL, event_id INTEGER NOT NULL,
                entry_at TEXT, entry REAL, exit_at TEXT, exit REAL, reason TEXT, gross REAL, net REAL,
                peak REAL, trough REAL, status TEXT, source TEXT, final INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT, PRIMARY KEY (strategy, version, event_id))""")

    def rows(self, strategy: str | None = None, ver: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM sim_trades", []
        if strategy:
            sql += " WHERE strategy = ? AND version = ?"
            args = [strategy, ver]
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]

    def delete(self, strategy: str) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM sim_trades WHERE strategy = ?", (strategy,))

    def record(self, strategy: str, params: dict, trades: list[dict], is_final) -> int:
        """Upsert trades that are not final yet; is_final(trade) decides when a trade freezes."""
        ver = version(params)
        frozen = {r["event_id"] for r in self.rows(strategy, ver) if r["final"]}
        now = crypto.iso(crypto.utcnow())
        rows = [(strategy, ver, t["row"]["id"], t["entry_at"], t["entry"], t["exit_at"], t["exit"], t["reason"],
                 t["gross"], t["net"], t["peak"], t["trough"], t["status"], t["source"], int(is_final(t)), now)
                for t in trades if t["row"]["id"] not in frozen]
        with sqlite3.connect(self.path) as c:
            c.executemany(f"INSERT OR REPLACE INTO sim_trades VALUES ({', '.join('?' * len(self.COLUMNS))})", rows)
        return len(rows)


def journal_trades(rows: list[dict], events: dict[int, dict]) -> list[dict]:
    """Journal rows back into the trade dicts the pages and summarize() use."""
    out = []
    for r in rows:
        if r["event_id"] in events:
            out.append({**r, "row": events[r["event_id"]]})
    return out

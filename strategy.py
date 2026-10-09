"""Paper-trading lab for crypto day trades on radar events.

A strategy = entry filter (what kind of move, how big a token, which score) + exit
rules (take-profit, stop-loss, max hold). Each matching event is simulated on the
stored price path from the moment of detection: buy at the detection price, sell at
the first snapshot that hits TP/SL or when the hold time runs out, minus fees.

Paths are snapshots (every scan, ~15 min) plus hourly history for gaps, so intraday
spikes between snapshots are invisible: TP fills at the TP level (a resting limit
order), SL fills at the observed price (a stop can slip), which keeps results conservative.
"""

from __future__ import annotations

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
}
FEE = 0.005      # round trip: ~0.1% exchange fee per side + slippage on small caps
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
]

FORM_FIELDS = {  # name: (cast, scale applied to the form value)
    "min_score": (int, 1), "max_score": (int, 1),
    "min_mcap": (float, 1e6), "max_mcap": (float, 1e6),
    "min_ch24": (float, 0.01), "max_ch24": (float, 0.01),
    "min_vol": (float, 1), "max_vol": (float, 1),
    "tp": (float, 0.01), "sl": (float, 0.01), "hold_h": (int, 1),
}


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
    out["triggers"], out["exclude"] = p["triggers"], p["exclude"]
    return out


def describe(params: dict) -> list[str]:
    p = {**DEFAULTS, **params}
    parts = []
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
    parts.append(f"TP +{p['tp']:.0%} · SL −{p['sl']:.0%} · max {p['hold_h']} h")
    return parts


def matches(row: dict, params: dict) -> bool:
    """row: a crypto view row (build_crypto_rows) - everything known at detection time."""
    p = {**DEFAULTS, **params}
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


def simulate(row: dict, points: list[tuple[str, float]], params: dict, now, fee: float = FEE) -> dict:
    p = {**DEFAULTS, **params}
    t0 = crypto.parse_iso(row["detected_at"])
    deadline = t0 + timedelta(hours=p["hold_h"])
    base = row["base_price"]
    trade = {"row": row, "entry": base, "entry_at": row["detected_at"], "exit": None, "exit_at": None,
             "reason": None, "gross": None, "net": None, "status": "open", "peak": 0.0, "trough": 0.0}
    last = None

    def time_exit(after: tuple[str, float] | None):
        # Sell at the deadline: use the snapshot closest to it, if one is within 2 h.
        near = timedelta(hours=2)
        if last and crypto.parse_iso(last[0]) >= deadline - near:
            return _close(trade, last[1], last[0], "czas", fee)
        if after and crypto.parse_iso(after[0]) - deadline <= near:
            return _close(trade, after[1], after[0], "czas", fee)
        trade["status"] = "no_data"
        return trade

    for ts, price in points:
        if ts <= row["detected_at"]:
            continue
        if crypto.parse_iso(ts) > deadline:
            return time_exit((ts, price))
        r = price / base - 1
        trade["peak"], trade["trough"] = max(trade["peak"], r), min(trade["trough"], r)
        if r >= p["tp"]:
            return _close(trade, base * (1 + p["tp"]), ts, "take-profit", fee)
        if r <= -p["sl"]:
            return _close(trade, price, ts, "stop-loss", fee)
        last = (ts, price)
    if now >= deadline:
        return time_exit(None)
    if last:  # still running: mark to market
        trade["gross"] = last[1] / base - 1
        trade["net"] = trade["gross"] - fee
    return trade


def _close(trade: dict, price: float, ts: str, reason: str, fee: float) -> dict:
    trade.update(exit=price, exit_at=ts, reason=reason, status="closed",
                 gross=price / trade["entry"] - 1)
    trade["net"] = trade["gross"] - fee
    return trade


def backtest(rows: list[dict], prices: dict, params: dict, now, fee: float = FEE, stake: float = STAKE) -> dict:
    trades = [simulate(r, prices.get(r["coin_id"], []), params, now, fee) for r in rows if matches(r, params)]
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
        "open_pnl": sum(stake * t["net"] for t in open_ if t["net"] is not None),
        "tp_hits": sum(t["reason"] == "take-profit" for t in closed),
        "sl_hits": sum(t["reason"] == "stop-loss" for t in closed),
    }

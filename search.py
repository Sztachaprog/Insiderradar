"""Strategy search: try many entry filters x exit rules on every signal with candles, and keep
only the rules that hold up on signals they were not chosen on.

Walk-forward check: signals are split by time. Rules are ranked on the older 60% ("train") and
then judged on the newer 40% ("test"), which played no part in choosing them. A rule passes when
it is positive on both with enough trades. Testing ~100k combinations finds lucky ones by chance,
which is exactly what the test split is there to expose.

Speed: exits are not simulated bar by bar for every combination. Per signal, the running
high / low are precomputed once, so "first bar that reaches this level" is a binary search.
tests/test_search.py checks that this gives exactly what strategy.simulate gives.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import timedelta

import crypto
import strategy

SIDES = ("long", "short")
TPS = (0.05, 0.08, 0.10, 0.15, 0.20)
SLS = (0.03, 0.05, 0.07, 0.10, 0.15, 0.20)   # up to strategy.MAX_SL: wider loses to liquidation at 3x
HOLDS = (4, 12, 24, 48)
EXITS = list(itertools.product(SIDES, TPS, SLS, HOLDS))
# When to enter: at detection, or once the move turns 4% (see strategy.find_entry). Limit orders on a pullback
# and a 2% turn were tried too: inconsistent between history and live, and every extra option lets the search
# fit the past by chance more often (passing rules fell from 36% to 20%).
ENTRY_VARIANTS = (("now", 0.0), ("confirm", 0.04))
ENTRY_WAIT_H = 6
COMBOS = [(v, k) for v in range(len(ENTRY_VARIANTS)) for k in range(len(EXITS))]

FILTERS = {
    "triggers": [[], ["pump24"], ["dump24"], ["volume"], ["move1h"], ["trending"]],
    "mcap": [(0, 0), (0, 30e6), (30e6, 300e6), (300e6, 0)],
    "ch24": [(None, None), (None, -0.10), (-0.10, 0.10), (0.10, None)],
    "score": [(0, 100), (50, 100), (65, 100)],
    "vol": [(None, None), (None, 0.5), (0.5, None)],
}

TRAIN_SHARE = 0.6
MIN_TRAIN = 30        # trades on the train part before a rule is considered at all
MIN_TEST = 15         # trades on the test part before a rule can pass
TOP = 25              # rules kept in the results (best per entry filter, ranked on train)
AUTO_MAX = 6          # passing rules tracked live at most at once ("Auto: ..." strategies)
AUTO_PER_RUN = 2      # new ones per search, so similar rules from one lucky stretch don't fill every slot
SAME_IDEA = 0.8       # same side and >= 80% of their signals in common (of both together): one idea, TP/SL aside
AUTO_PREFIX = "Auto: "
# Small caps can gap straight through a stop. A rule whose worst trade would wipe out most of the margin
# at our leverage (-25% at 3x = -75% of margin, close to liquidation) never passes, however good on average.
SAFE_WORST = -0.75 / strategy.LEVERAGE


def filter_params() -> list[dict]:
    out = []
    for trig, (mc0, mc1), (c0, c1), (s0, s1), (v0, v1) in itertools.product(*FILTERS.values()):
        p = {"cex_only": True}                  # leverage, and clean candles, exist only for Binance tokens
        if trig:
            p["triggers"] = trig
        if mc0 or mc1:
            p.update(min_mcap=mc0, max_mcap=mc1)
        if c0 is not None or c1 is not None:
            p.update(min_ch24=c0, max_ch24=c1)
        if s0:
            p["min_score"] = s0
        if v0 is not None or v1 is not None:
            p.update(min_vol=v0, max_vol=v1)
        out.append(p)
    return out


class Path:
    """One signal's candles after detection, with running extremes for fast level lookups."""

    def __init__(self, row: dict, bars: list[tuple]):
        self.row, self.base, self.t0 = row, row["base_price"], row["detected_at"]
        bars = [b for b in bars if b[0] > self.t0]
        self.ts = [b[0] for b in bars]
        self.open = [b[1] for b in bars]
        self.close = [b[4] for b in bars]
        self.runmax = list(itertools.accumulate((b[2] for b in bars), max))            # non-decreasing
        self.neg_runmin = list(itertools.accumulate((-b[3] for b in bars), max))       # non-decreasing

    def _first_high(self, level: float) -> int:
        return bisect_left(self.runmax, level)          # first bar whose high reached the level

    def _first_low(self, level: float) -> int:
        return bisect_left(self.neg_runmin, -level)     # first bar whose low reached the level

    def outcome(self, side: str, tp: float, sl: float, hold_h: int, now, fee: float) -> float | None:
        """Net return, the same as strategy.simulate on candles; None while open or without data."""
        base = self.base
        deadline_dt = crypto.parse_iso(self.t0) + timedelta(hours=hold_h)
        end = bisect_right(self.ts, crypto.iso(deadline_dt))          # bars [0, end) are within the hold
        if side == "short":
            tp_level, sl_level = base * (1 - tp), base * (1 + sl)
            i_sl, i_tp = self._first_high(sl_level), self._first_low(tp_level)
        else:
            tp_level, sl_level = base * (1 + tp), base * (1 - sl)
            i_sl, i_tp = self._first_low(sl_level), self._first_high(tp_level)
        if i_sl < end and i_sl <= i_tp:        # stop first; a bar reaching both counts as a stop
            price = max(self.open[i_sl], sl_level) if side == "short" else min(self.open[i_sl], sl_level)
        elif i_tp < end:
            price = min(self.open[i_tp], tp_level) if side == "short" else max(self.open[i_tp], tp_level)
        else:                                  # time exit: the bar nearest the deadline, within 2 h
            near = timedelta(hours=2)
            if end < len(self.ts) or now >= deadline_dt:
                if end and crypto.parse_iso(self.ts[end - 1]) >= deadline_dt - near:
                    price = self.close[end - 1]
                elif end < len(self.ts) and crypto.parse_iso(self.ts[end]) - deadline_dt <= near:
                    price = self.open[end]
                else:
                    return None                # no data around the deadline
            else:
                return None                    # still running
        move = price / base - 1
        return (-move if side == "short" else move) - fee


def _stats(nets: list[float], sl: float) -> dict:
    """lcb: average minus one standard error (penalises small / noisy samples).
    per_risk: lcb in units of the stop (R), so a wide stop must earn proportionally more."""
    n = len(nets)
    if not n:
        return {"n": 0, "avg": None, "win_rate": None, "lcb": None, "per_risk": None, "worst": None}
    avg = sum(nets) / n
    sd = (sum((x - avg) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    lcb = avg - sd / n ** 0.5
    return {"n": n, "avg": avg, "win_rate": sum(x > 0 for x in nets) / n, "lcb": lcb,
            "per_risk": lcb / sl, "worst": min(nets)}


def entry_params(v: int) -> dict:
    mode, pct = ENTRY_VARIANTS[v]
    return {} if mode == "now" else {"entry": mode, "entry_pct": pct, "entry_wait_h": ENTRY_WAIT_H}


def entry_path(row: dict, bars: list[tuple], side: str, v: int, now) -> Path | None:
    """The signal's path as seen from the entry of variant v (None: the order was never filled)."""
    e = strategy.find_entry(row, bars, {"side": side, **entry_params(v)}, now)
    if isinstance(e, str):
        return None
    if e[0] == row["detected_at"]:
        return Path(row, bars)
    return Path({**row, "detected_at": e[0], "base_price": e[1]}, bars)


def run(rows: list[dict], paths: dict[int, list], now, fee: float = strategy.FEE) -> dict:
    """rows: crypto view rows (live + history); paths: signal id -> candles. Only Binance tokens."""
    rows = sorted((r for r in rows if paths.get(r["id"]) and r.get("venue") == "binance"),
                  key=lambda r: r["detected_at"])
    # Net result of every entry x exit combination for every signal (None = no fill / open / no data).
    table = []
    for r in rows:
        views = {(side, v): entry_path(r, paths[r["id"]], side, v, now)
                 for side in SIDES for v in range(len(ENTRY_VARIANTS))}
        table.append([(view.outcome(*EXITS[k], now, fee) if (view := views[(EXITS[k][0], v)]) else None)
                      for v, k in COMBOS])
    done = [i for i, outs in enumerate(table) if any(o is not None for o in outs)]
    cut = rows[done[int(len(done) * TRAIN_SHARE)]]["detected_at"] if done else ""
    train_ids = [i for i in done if rows[i]["detected_at"] < cut]
    test_ids = [i for i in done if rows[i]["detected_at"] >= cut]

    best_per_filter, seen = [], set()
    for fp in sorted(filter_params(), key=len):       # simplest filters first: they win ties
        tr = [i for i in train_ids if strategy.matches(rows[i], fp)]
        if len(tr) < MIN_TRAIN:
            continue
        te = [i for i in test_ids if strategy.matches(rows[i], fp)]
        if (key := (tuple(tr), tuple(te))) in seen:
            continue                                   # an extra condition that filtered nothing out
        seen.add(key)
        best = None
        for c, (v, k) in enumerate(COMBOS):
            nets = [x for i in tr if (x := table[i][c]) is not None]
            if len(nets) < MIN_TRAIN:
                continue
            s_tr = _stats(nets, EXITS[k][2])
            if best is None or s_tr["per_risk"] > best[0]["per_risk"]:
                best = (s_tr, c)
        if best:
            s_tr, c = best
            v, k = COMBOS[c]
            side, tp, sl, h = EXITS[k]
            s_te = _stats([x for i in te if (x := table[i][c]) is not None], sl)
            params = {**fp, **entry_params(v), "tp": tp, "sl": sl, "hold_h": h,
                      **({"side": "short"} if side == "short" else {})}
            safe = s_tr["worst"] > SAFE_WORST and (not s_te["n"] or s_te["worst"] > SAFE_WORST)
            best_per_filter.append({"params": params, "train": s_tr, "test": s_te, "safe": safe,
                                    "passed": bool(s_tr["avg"] > 0 and s_te["n"] >= MIN_TEST and s_te["avg"] > 0
                                                   and safe)})
    best_per_filter.sort(key=lambda r: -r["train"]["per_risk"])
    return {"ran_at": crypto.iso(now), "signals": len(done), "train": len(train_ids), "test": len(test_ids),
            "split_at": cut, "tried": len(filter_params()) * len(COMBOS), "rules": best_per_filter[:TOP],
            "train_positive": sum(r["train"]["avg"] > 0 for r in best_per_filter),
            "passed": sum(r["passed"] for r in best_per_filter)}


# ---------- storage + live tracking of passing rules ----------

class SearchStore:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS strategy_search (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT)")

    def save(self, result: dict) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR REPLACE INTO strategy_search VALUES (1, ?)", (json.dumps(result),))

    def latest(self) -> dict | None:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT data FROM strategy_search WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None


def auto_name(params: dict) -> str:
    """Short name: side, kind of move, token size (the full rules are shown next to it anyway)."""
    p = {**strategy.DEFAULTS, **params}
    parts = ["short" if p["side"] == "short" else "long"]
    parts += [crypto.TRIGGER_LABELS[t] for t in p["triggers"]] or ["każdy ruch"]
    if p["min_mcap"] or p["max_mcap"]:
        parts.append(f"{p['min_mcap'] / 1e6:g}–{p['max_mcap'] / 1e6:g} mln $" if p["max_mcap"]
                     else f"≥ {p['min_mcap'] / 1e6:g} mln $")
    if p["entry"] != "now":
        parts.append(f"wejście {strategy.ENTRIES[p['entry']]}")
    return AUTO_PREFIX + ", ".join(parts)


def same_idea(a: dict, b: dict, rows: list[dict]) -> bool:
    """Same side and (nearly) the same signals: a different take-profit or stop is not a new idea."""
    if {**strategy.DEFAULTS, **a}["side"] != {**strategy.DEFAULTS, **b}["side"]:
        return False
    sa = {r["id"] for r in rows if strategy.matches(r, a)}
    sb = {r["id"] for r in rows if strategy.matches(r, b)}
    if not sa or not sb:
        return strategy.version(a) == strategy.version(b)
    return len(sa & sb) / len(sa | sb) >= SAME_IDEA     # a broad rule merely containing a narrow one is new


def upgrade_autos(cstore: crypto.CryptoStore) -> int:
    """Auto strategies saved before the Binance-only rule get it too."""
    n = 0
    for s in cstore.strategies():
        if s["name"].startswith(AUTO_PREFIX) and not s["params"].get("cex_only"):
            cstore.update_strategy(s["id"], {**s["params"], "cex_only": True})
            n += 1
    return n


def drop_duplicate_autos(cstore: crypto.CryptoStore, journal: strategy.Journal, rows: list[dict]) -> list[str]:
    """Remove auto strategies that repeat an older one (or a preset); the oldest keeps its live record."""
    kept = [p for k, _, _, p in strategy.PRESETS if k not in strategy.REFERENCE_ONLY]
    removed = []
    for s in cstore.strategies():                      # oldest first
        if not s["name"].startswith(AUTO_PREFIX):
            kept.append(s["params"])
        elif any(same_idea(s["params"], k, rows) for k in kept):
            cstore.delete_strategy(s["id"])
            journal.delete(f"c{s['id']}")
            removed.append(s["name"])
        else:
            kept.append(s["params"])
    return removed


def track_passing(result: dict, cstore: crypto.CryptoStore, rows: list[dict]) -> list[str]:
    """Save the best passing rules as strategies, so the scans journal them live from now on
    (the only test no search can fool). One strategy per idea, never more than AUTO_MAX at once."""
    tracked = [p for k, _, _, p in strategy.PRESETS if k not in strategy.REFERENCE_ONLY] + \
              [s["params"] for s in cstore.strategies()]
    room = min(AUTO_PER_RUN, AUTO_MAX - sum(s["name"].startswith(AUTO_PREFIX) for s in cstore.strategies()))
    added = []
    for r in result["rules"]:
        if room <= 0:
            break
        if r["passed"] and not any(same_idea(r["params"], p, rows) for p in tracked):
            name = auto_name(r["params"])
            cstore.save_strategy(name, r["params"])
            tracked.append(r["params"])
            added.append(name)
            room -= 1
    return added

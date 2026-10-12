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
SLS = (0.03, 0.05, 0.07, 0.10)
HOLDS = (4, 12, 24, 48)
EXITS = list(itertools.product(SIDES, TPS, SLS, HOLDS))

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
AUTO_PREFIX = "Auto: "


def filter_params() -> list[dict]:
    out = []
    for trig, (mc0, mc1), (c0, c1), (s0, s1), (v0, v1) in itertools.product(*FILTERS.values()):
        p = {}
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


def _stats(nets: list[float]) -> dict:
    n = len(nets)
    if not n:
        return {"n": 0, "avg": None, "win_rate": None, "lcb": None}
    avg = sum(nets) / n
    sd = (sum((x - avg) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"n": n, "avg": avg, "win_rate": sum(x > 0 for x in nets) / n,
            "lcb": avg - sd / n ** 0.5}       # average minus one standard error: penalises small/noisy samples


def run(rows: list[dict], paths: dict[int, list], now, fee: float = strategy.FEE) -> dict:
    """rows: crypto view rows (live + history); paths: signal id -> candles."""
    sigs = sorted((Path(r, paths[r["id"]]) for r in rows if paths.get(r["id"])), key=lambda p: p.t0)
    # Net result of every exit rule for every signal (None = open / no data).
    table = [[p.outcome(side, tp, sl, h, now, fee) for side, tp, sl, h in EXITS] for p in sigs]
    done = [i for i, outs in enumerate(table) if any(o is not None for o in outs)]
    cut = sigs[done[int(len(done) * TRAIN_SHARE)]].t0 if done else ""
    train_ids = [i for i in done if sigs[i].t0 < cut]
    test_ids = [i for i in done if sigs[i].t0 >= cut]

    best_per_filter, seen = [], set()
    for fp in sorted(filter_params(), key=len):       # simplest filters first: they win ties
        tr = [i for i in train_ids if strategy.matches(sigs[i].row, fp)]
        if len(tr) < MIN_TRAIN:
            continue
        te = [i for i in test_ids if strategy.matches(sigs[i].row, fp)]
        if (key := (tuple(tr), tuple(te))) in seen:
            continue                                   # an extra condition that filtered nothing out
        seen.add(key)
        best = None
        for k, (side, tp, sl, h) in enumerate(EXITS):
            s_tr = _stats([table[i][k] for i in tr if table[i][k] is not None])
            if s_tr["n"] < MIN_TRAIN:
                continue
            if best is None or s_tr["lcb"] > best[0]["lcb"]:
                best = (s_tr, k)
        if best:
            s_tr, k = best
            side, tp, sl, h = EXITS[k]
            s_te = _stats([table[i][k] for i in te if table[i][k] is not None])
            params = {**fp, "tp": tp, "sl": sl, "hold_h": h, **({"side": "short"} if side == "short" else {})}
            best_per_filter.append({"params": params, "train": s_tr, "test": s_te,
                                    "passed": bool(s_tr["avg"] > 0 and s_te["n"] >= MIN_TEST and s_te["avg"] > 0)})
    best_per_filter.sort(key=lambda r: -r["train"]["lcb"])
    return {"ran_at": crypto.iso(now), "signals": len(done), "train": len(train_ids), "test": len(test_ids),
            "split_at": cut, "tried": len(filter_params()) * len(EXITS), "rules": best_per_filter[:TOP],
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
    return AUTO_PREFIX + ", ".join(parts)


def track_passing(result: dict, cstore: crypto.CryptoStore) -> list[str]:
    """Save the best passing rules as strategies, so the scans journal them live from now on
    (the only test no search can fool). Never more than AUTO_MAX auto strategies at once."""
    have = {strategy.version(s["params"]) for s in cstore.strategies()} | \
           {strategy.version(p) for _, _, _, p in strategy.PRESETS}
    room = min(AUTO_PER_RUN, AUTO_MAX - sum(s["name"].startswith(AUTO_PREFIX) for s in cstore.strategies()))
    added = []
    for r in result["rules"]:
        if room <= 0:
            break
        if r["passed"] and strategy.version(r["params"]) not in have:
            name = auto_name(r["params"])
            cstore.save_strategy(name, r["params"])
            have.add(strategy.version(r["params"]))
            added.append(name)
            room -= 1
    return added

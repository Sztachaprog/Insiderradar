"""Does the score predict returns? Score buckets, per-factor effect and suggested weights.

Shared by stocks and crypto: each item is {"score", "factors", "ret"} where ret is
the realised return at the chosen horizon.
"""

from __future__ import annotations

MIN_FACTOR_N = 5        # below this a factor's effect is noise
MIN_APPLY_N = 30        # total sample needed before suggested weights can be applied
MAX_STEP = 15           # max points a single calibration moves a weight
BUCKETS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = _avg(rx), _avg(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** .5
    vy = sum((b - my) ** 2 for b in ry) ** .5
    return cov / (vx * vy) if vx and vy else None


def calibrate(items: list[dict], factors: dict[str, tuple[int, str]], weights: dict[str, float],
              scale: float = 100) -> dict:
    """scale: points per 1.0 of return difference (100 -> +5% better returns = +5 points)."""
    items = [i for i in items if i["ret"] is not None and i["score"] is not None]
    rets = [i["ret"] for i in items]
    avg_all = _avg(rets)

    buckets = []
    for lo, hi in BUCKETS:
        rs = [i["ret"] for i in items if lo <= i["score"] < hi]
        buckets.append({"label": f"{lo}–{min(hi, 100)}", "n": len(rs), "avg": _avg(rs),
                        "win_rate": sum(r > 0 for r in rs) / len(rs) if rs else None})

    rows = []
    for key, (default, label) in factors.items():
        with_ = [i["ret"] for i in items if key in i["factors"]]
        without = [i["ret"] for i in items if key not in i["factors"]]
        current = weights.get(key, default)
        row = {"key": key, "label": label, "weight": current, "default": default, "n": len(with_),
               "avg_with": _avg(with_), "avg_without": _avg(without), "diff": None,
               "verdict": "za mało danych", "suggested": current}
        if len(with_) >= MIN_FACTOR_N and without:
            diff = row["avg_with"] - row["avg_without"]
            row["diff"] = diff
            row["verdict"] = "pomaga" if diff > 0.02 else "szkodzi" if diff < -0.02 else "bez wpływu"
            # Nudge toward the observed effect, damped by sample size so 5 trades move it less than 50.
            confidence = min(1.0, len(with_) / 30)
            step = max(-MAX_STEP, min(MAX_STEP, diff * scale * confidence))
            row["suggested"] = round(current + step)
        rows.append(row)
    rows.sort(key=lambda r: (r["n"] < MIN_FACTOR_N, -(abs(r["diff"]) if r["diff"] is not None else 0)))

    return {"n": len(items), "avg": avg_all, "buckets": buckets, "factors": rows,
            "spearman": spearman([i["score"] for i in items], rets),
            "can_apply": len(items) >= MIN_APPLY_N,
            "changes": {r["key"]: r["suggested"] for r in rows if r["suggested"] != r["weight"]}}


def verdict(spearman_rho: float | None) -> str:
    if spearman_rho is None:
        return "Za mało danych, żeby ocenić skuteczność."
    if spearman_rho >= 0.3:
        return "Ocena dobrze przewiduje wynik: wyższa ocena idzie w parze z wyższym zyskiem."
    if spearman_rho >= 0.1:
        return "Ocena trochę pomaga, ale związek jest słaby."
    if spearman_rho > -0.1:
        return "Na razie ocena nie przewiduje wyniku. Warto skalibrować wagi."
    return "Ocena działa odwrotnie, niż powinna. Skalibruj wagi."


# ---------- Weight overrides (sqlite) ----------

def ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS weights (
        scope TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (scope, key))""")


def load_weights(conn, scope: str) -> dict[str, float]:
    return dict(conn.execute("SELECT key, value FROM weights WHERE scope = ?", (scope,)))


def save_weights(conn, scope: str, weights: dict | None) -> None:
    """None resets the scope to the built-in defaults."""
    if weights is None:
        conn.execute("DELETE FROM weights WHERE scope = ?", (scope,))
        return
    conn.executemany("INSERT OR REPLACE INTO weights VALUES (?, ?, ?)",
                     [(scope, k, float(v)) for k, v in weights.items()])

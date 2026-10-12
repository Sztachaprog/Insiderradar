"""BTC session model in the ICT / "Smart Money Concepts" style, after XPreay's "Preay model"
(video "BITCOIN - SKUTECZNA STRATEGIA DAY-TRADINGOWA (BACKTEST)", 2026-01-09, and its slides).

The author trades it partly by judgement ("hybrid"); this is its mechanical core, so it can be
tested on years of data instead of the 7 trades shown in the video. Every NY trading day:

1. Asian session range (ASR): high / low of 19:00-24:00 New York time.
2. New York midnight open (00:00): below it we look for longs, above it for shorts (premium / discount).
3. Kill zone 01:00-05:00 NY (London open): price sweeps the ASR high (-> short) or low (-> long).
4. Market structure shift on 5-minute candles: a candle closes (body) beyond the last swing
   (3-candle fractal) before the sweep's extreme, in the opposite direction. Entry at that close.
5. Stop at the extreme of the manipulation leg, take-profit at a fixed 1:2 risk/reward.
   Exit at 16:00 NY if neither is hit. One trade a day; Tuesday-Thursday are the author's best days.

Results are in R (multiples of the risk taken) after fees, like the author counts them.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import candles

NY = ZoneInfo("America/New_York")
SYMBOL = "BTCUSDT"
RES = candles.RES_5M
HISTORY_DAYS = 730
CALLS_PER_RUN = 60             # 1,000 candles per call; the 2-year history fills over a few scans
FEE = 0.0015                   # round trip on BTC perpetual futures: taker fees + slippage
LIVE_SINCE = "2026-10-13"      # added to the app on this day: trades from here on are the live record

DEFAULTS = {
    "rr": 2.0,                 # take-profit at 2 x the risk
    "days": [1, 2, 3],         # Tuesday, Wednesday, Thursday (Monday = 0)
    "window": [1, 5],          # sweep and entry between 01:00 and 05:00 NY
    "bias": True,              # shorts only above the midnight open, longs only below it
    "min_risk": 0.001,         # a stop closer than 0.1% is noise (fees alone would be 1.5 R)
    "max_risk": 0.03,
    "exit_hour": 16,           # close what is still open at 16:00 NY
}
PRESETS = [
    ("ict_classic", "ICT: sweep Azji + MSS (wt–czw)",
     "Model z filmu: zebranie płynności sesji azjatyckiej między 01:00 a 05:00 NY, zmiana struktury na 5M, "
     "SL na skraju manipulacji, TP 1:2. Tylko wtorek–czwartek.", {}),
    ("ict_all_days", "ICT: sweep Azji + MSS (pon–pt)", "Ten sam model w każdy dzień roboczy.",
     {"days": [0, 1, 2, 3, 4]}),
    ("ict_aplus", "ICT A+: wejście 02:00–04:00", "Węższe okno, które autor uważa za setupy A+ (wt–czw).",
     {"window": [2, 4]}),
]


# ---------- data ----------

class BarStore:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS ict_bars (
                ts INTEGER PRIMARY KEY, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL) WITHOUT ROWID""")

    def save(self, rows: list[tuple]) -> None:
        with sqlite3.connect(self.path) as c:
            c.executemany("INSERT OR REPLACE INTO ict_bars VALUES (?, ?, ?, ?, ?)", [r[:5] for r in rows])

    def last_ts(self) -> int | None:
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT MAX(ts) FROM ict_bars").fetchone()[0]

    def bars(self) -> list[tuple]:
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT ts, o, h, l, c FROM ict_bars ORDER BY ts").fetchall()


def update(store: BarStore, client, now: datetime, calls: int = CALLS_PER_RUN) -> int:
    """Append 5-minute BTC candles up to now (the history fills over a few runs)."""
    end = int(now.timestamp()) // RES * RES            # only closed candles
    last = store.last_ts()
    start = last + RES if last else end - HISTORY_DAYS * 86400
    added = 0
    for _ in range(calls):
        if start >= end:
            break
        chunk_end = min(end, start + 1000 * RES)
        rows = client.binance(SYMBOL, RES, start, chunk_end)
        store.save(rows)
        added += len(rows)
        start = chunk_end
    return added


# ---------- the model ----------

def _by_day(bars: list[tuple]) -> dict[date, list[tuple]]:
    """NY date -> that day's candles as (ts, o, h, l, c, hour, minute)."""
    out: dict[date, list[tuple]] = {}
    for ts, o, h, low, c in bars:
        t = datetime.fromtimestamp(ts, timezone.utc).astimezone(NY)
        out.setdefault(t.date(), []).append((ts, o, h, low, c, t.hour, t.minute))
    return out


def _fractal_before(seq: list[tuple], k: int, low: bool) -> float | None:
    """The last 3-candle swing low (or high) that formed before candle k."""
    for m in range(k - 1, 0, -1):
        if low and seq[m][3] < seq[m - 1][3] and seq[m][3] < seq[m + 1][3]:
            return seq[m][3]
        if not low and seq[m][2] > seq[m - 1][2] and seq[m][2] > seq[m + 1][2]:
            return seq[m][2]
    return None


def day_trade(day: date, days: dict, params: dict, now_ts: int) -> dict | None:
    p = {**DEFAULTS, **params}
    if day.weekday() not in p["days"]:
        return None
    today = days.get(day)
    prev = days.get(day - timedelta(days=1))
    if not today or not prev or today[0][5] != 0 or today[0][6] != 0:
        return None                                   # need the 00:00 candle and the evening before
    asr = [b for b in prev if b[5] >= 19]
    if len(asr) < 50:                                 # 60 candles in a complete 19:00-24:00 range
        return None
    asr_hi, asr_lo = max(b[2] for b in asr), min(b[3] for b in asr)
    midnight = today[0][1]
    w0, w1 = p["window"]
    seq = asr + [b for b in today if b[5] < w1]
    side, ext = None, None
    entry = None
    for k, b in enumerate(seq):
        if k < len(asr) or not w0 <= b[5] < w1:      # sweeps count only inside today's kill zone
            continue
        if side is None:
            up = b[2] > asr_hi and (not p["bias"] or b[2] > midnight)
            down = b[3] < asr_lo and (not p["bias"] or b[3] < midnight)
            if up and down:
                return None                           # both sides swept in one candle: no read
            if up or down:
                side, ext = ("short" if up else "long"), k
            continue
        if side == "short":
            if b[2] > seq[ext][2]:
                ext = k                               # the manipulation leg is still extending
                continue
            ref = _fractal_before(seq, ext, low=True)
            if ref is not None and b[4] < ref:
                entry, stop, at = b[4], seq[ext][2], b
                break
        else:
            if b[3] < seq[ext][3]:
                ext = k
                continue
            ref = _fractal_before(seq, ext, low=False)
            if ref is not None and b[4] > ref:
                entry, stop, at = b[4], seq[ext][3], b
                break
    if entry is None:
        return None
    risk = abs(stop - entry) / entry
    if not p["min_risk"] <= risk <= p["max_risk"]:
        return None
    target = entry - p["rr"] * (stop - entry)         # same formula both ways: stop is on the other side
    trade = {"day": day.isoformat(), "side": side, "entry_ts": at[0] + RES, "entry": entry, "stop": stop,
             "target": target, "risk": risk, "exit_ts": None, "exit": None, "reason": None, "ret": None, "r": None}
    short = side == "short"
    last = None
    for b in today:
        if b[0] <= at[0]:
            continue
        if b[5] >= p["exit_hour"]:
            break
        o, h, low = b[1], b[2], b[3]
        if (h >= stop) if short else (low <= stop):
            return _close(trade, max(o, stop) if short else min(o, stop), b[0], "stop-loss")
        if (low <= target) if short else (h >= target):
            return _close(trade, min(o, target) if short else max(o, target), b[0], "take-profit")
        last = b
    exit_at = int(datetime(day.year, day.month, day.day, p["exit_hour"], tzinfo=NY).timestamp())
    if now_ts < exit_at:                              # still running today
        if last:
            trade["ret"] = _ret(trade, last[4])
            trade["r"] = trade["ret"] / risk
        return trade
    if last is None:
        return None
    return _close(trade, last[4], last[0] + RES, "czas")


def _ret(trade: dict, price: float) -> float:
    move = price / trade["entry"] - 1
    return (-move if trade["side"] == "short" else move) - FEE


def _close(trade: dict, price: float, ts: int, reason: str) -> dict:
    trade.update(exit=price, exit_ts=ts, reason=reason, ret=_ret(trade, price))
    trade["r"] = trade["ret"] / trade["risk"]
    return trade


def backtest(bars: list[tuple], params: dict, now: datetime) -> list[dict]:
    days = _by_day(bars)
    now_ts = int(now.timestamp())
    return [t for d in sorted(days) if (t := day_trade(d, days, params, now_ts))]


def summarize(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["exit_ts"]]
    rs = [t["r"] for t in closed]
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in closed:
        eq += t["r"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        curve.append((t["exit_ts"], round(eq, 2)))
    streak = worst_streak = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        worst_streak = max(worst_streak, streak)
    by_weekday = {}
    for t in closed:
        wd = date.fromisoformat(t["day"]).weekday()
        by_weekday.setdefault(wd, []).append(t["r"])
    return {"n": len(closed), "open": len(trades) - len(closed),
            "win_rate": sum(r > 0 for r in rs) / len(rs) if rs else None,
            "avg_r": sum(rs) / len(rs) if rs else None, "total_r": sum(rs),
            "avg_ret": sum(t["ret"] for t in closed) / len(closed) if closed else None,
            "avg_risk": sum(t["risk"] for t in closed) / len(closed) if closed else None,
            "max_dd_r": dd, "worst_streak": worst_streak, "curve": curve,
            "by_weekday": {wd: (len(v), sum(v) / len(v)) for wd, v in sorted(by_weekday.items())},
            "longs": sum(t["side"] == "long" for t in closed), "shorts": sum(t["side"] == "short" for t in closed)}

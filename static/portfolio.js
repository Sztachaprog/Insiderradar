// Paper portfolio simulation: a line-by-line port of portfolio.simulate() (Python), so the page can use
// the viewer's own settings. tests/test_portfolio.py checks that both give the same numbers.
function simulatePortfolio(trades, settings, nowIso, defaults) {
  const s = Object.assign({}, defaults, settings);
  const result = { buckets: {}, open: [], closed: [] };
  const sumMargin = list => list.reduce((a, p) => a + p.margin, 0);
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const allowed = t => {
    if (t.ret === null || t.ret === undefined || t.in < s.start) return false;
    if (t.b === "lev") return s.strategies === null || s.strategies === undefined || s.strategies.includes(t.src);
    if (t.b === "spot" && !s.spotSources.includes(t.src)) return false;
    return (t.score || 0) >= s.minScore[t.b];
  };
  for (const b of ["stocks", "lev", "spot"]) {
    const cap = s.capital * s.split[b] / 100;
    let cash = cap, skipped = 0;
    const open = [], closed = [], curve = [];
    const closeUntil = tIso => {
      const due = open.filter(p => p.out !== null && p.out !== undefined && p.out <= tIso)
        .sort((x, y) => cmp(x.out, y.out) || cmp(x.in, y.in) || cmp(x.src, y.src) || cmp(x.sym, y.sym));
      for (const p of due) {
        open.splice(open.indexOf(p), 1);
        cash += p.margin + p.pnl;
        closed.push(p);
        curve.push([p.out, cash + sumMargin(open)]);
      }
    };
    for (const t of trades) {
      if (t.b !== b || !allowed(t)) continue;
      closeUntil(t.in);
      const equity = cash + sumMargin(open);
      let size, margin, pnl;
      if (b === "lev") {
        if (open.some(p => p.sym === t.sym)) { skipped += 1; continue; }
        size = equity * s.risk / 100 / t.sl;
        margin = size / s.lev;
        pnl = Math.max(size * t.ret, -margin);
      } else {
        if (open.length >= s.maxPos[b]) { skipped += 1; continue; }
        size = margin = equity / s.maxPos[b];
        pnl = margin * t.ret;
      }
      if (margin <= 0 || margin > cash + 1e-9) { skipped += 1; continue; }
      cash -= margin;
      open.push(Object.assign({}, t, { size, margin, pnl }));
    }
    closeUntil(nowIso);
    const committed = sumMargin(open);
    const unrealized = open.reduce((a, p) => a + p.pnl, 0);
    const wins = closed.filter(p => p.pnl > 0);
    result.buckets[b] = {
      start: cap, equity: cash + committed + unrealized, cash, committed,
      realized: closed.reduce((a, p) => a + p.pnl, 0), unrealized, trades: closed.length,
      open: open.length, skipped, win_rate: closed.length ? wins.length / closed.length : null, curve,
    };
    result.open.push(...open);
    result.closed.push(...closed);
  }
  return result;
}

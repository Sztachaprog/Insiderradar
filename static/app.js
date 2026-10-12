// Mobile: the sidebar opens with the ☰ button (no hover on touch screens).
(() => {
  const side = document.getElementById("side"), scrim = document.getElementById("scrim");
  const toggle = open => { side.classList.toggle("open", open); scrim.classList.toggle("show", open); };
  document.getElementById("menu-btn").addEventListener("click", () => toggle(!side.classList.contains("open")));
  scrim.addEventListener("click", () => toggle(false));
})();

// Click any column header to sort. Numbers understand $, %, mln/mld/bln/tys, the minus sign "−" and arrows;
// cells can carry data-sort (e.g. ISO time) to override what is shown. Empty cells ("—") always go last.
(() => {
  const UNITS = { bln: 1e12, mld: 1e9, mln: 1e6, tys: 1e3, k: 1e3 };
  const NUM = /^[\s▲▼↑↓+]*(-|−)?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(bln|mld|mln|tys\.?|k)?(?![\dA-Za-z])/i;
  const value = td => {
    if (!td) return null;
    const raw = (td.dataset.sort ?? td.innerText).trim();
    if (!raw || raw === "—" || raw === "-") return null;
    if (/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw;                 // ISO dates: compare as text
    const m = raw.replace(/\u2212/g, "-").match(NUM);
    if (!m) return raw.toLowerCase();
    const n = parseFloat(m[2].replace(/,/g, "")) * (UNITS[(m[3] || "").replace(".", "").toLowerCase()] || 1);
    return m[1] ? -n : n;
  };
  document.querySelectorAll("table").forEach(table => {
    if (!table.tHead || !table.tBodies.length) return;
    [...table.tHead.rows[0].cells].forEach((th, col) => {
      if ("nosort" in th.dataset) return;
      th.classList.add("sortable");
      th.addEventListener("click", e => {
        e.preventDefault();
        const body = table.tBodies[0], rows = [...body.rows];
        const vals = rows.map(r => value(r.cells[col]));
        const numeric = vals.some(v => typeof v === "number");
        const asc = th.classList.contains("sorted-desc") || (!th.className.includes("sorted") && !numeric);
        [...table.tHead.rows[0].cells].forEach(h => h.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(asc ? "sorted-asc" : "sorted-desc");
        const order = rows.map((r, i) => i).sort((i, j) => {
          const a = vals[i], b = vals[j];
          if (a === null || b === null) return a === null ? (b === null ? 0 : 1) : -1;
          const cmp = typeof a === typeof b ? (a < b ? -1 : a > b ? 1 : 0) : (typeof a === "number" ? -1 : 1);
          return asc ? cmp : -cmp;
        });
        order.forEach(i => body.appendChild(rows[i]));
      });
    });
  });
})();

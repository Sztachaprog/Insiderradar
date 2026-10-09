"""Heuristic read of *why* an insider bought: conviction buy, fund move or a placement.

The score is a sum of named factors. Each factor has a weight (points); weights can
be recalibrated from tracked results (see calibration.py), so every factor that fired
is returned too.
"""

from __future__ import annotations

import re

PLACEMENT_PATTERNS = [
    r"private placement", r"securities purchase agreement", r"subscription agreement",
    r"share purchase agreement with the issuer", r"purchase agreement with the (company|issuer)",
    r"registered direct", r"underwrit", r"public offering", r"\bpipe\b", r"directly from the (company|issuer)",
    r"rights offering", r"at[- ]the[- ]market offering",
]
OPEN_MARKET_PATTERNS = [r"multiple transactions at prices ranging", r"weighted average"]
ENTITY_RE = re.compile(r"\b(llc|l\.?p\.?|ltd|limited|inc|corp|fund|capital|partners|holdings|trust|"
                       r"management|advisors|group|pty|gmbh|s\.a\.|plc)\b", re.I)

KINDS = {
    "placement": ("Emisja / umowa ze spółką",
                  "Akcje kupione bezpośrednio od spółki (emisja prywatna, PIPE albo oferta). "
                  "Spółka pozyskuje kapitał, często po cenie z dyskontem. To słabszy sygnał niż zakup na rynku."),
    "fund": ("Fundusz / podmiot",
             "Kupuje fundusz albo spółka holdingowa, a nie osoba z zarządu. "
             "To może być zwykłe zarządzanie portfelem, a nie wiedza z wnętrza firmy."),
    "insider": ("Zakup insidera na rynku",
                "Członek zarządu albo rady kupuje za własne pieniądze na otwartym rynku. "
                "To klasycznie najsilniejszy sygnał, bo powodów do kupna jest jeden: wiara we wzrost."),
    "sale": ("Sprzedaż",
             "Insiderzy sprzedają z wielu powodów (podatki, dywersyfikacja, zakupy prywatne), "
             "więc pojedyncza sprzedaż mówi mniej niż zakup."),
}

# key: (default points, label used in calibration)
FACTORS = {
    "role_ceo": (25, "Kupuje CEO / prezes"),
    "role_cfo": (22, "Kupuje CFO"),
    "role_officer": (15, "Kupuje członek zarządu"),
    "role_director": (10, "Kupuje członek rady"),
    "role_10pct": (5, "Kupuje 10% udziałowiec"),
    "value_large": (15, "Kwota ≥ $1 mln"),
    "value_mid": (10, "Kwota $250k–1 mln"),
    "value_small": (4, "Kwota < $250k"),
    "mcap_huge": (-10, "≥ 5% kapitalizacji"),
    "mcap_high": (15, "0,5–5% kapitalizacji"),
    "mcap_mid": (8, "0,1–0,5% kapitalizacji"),
    "small_cap": (0, "Mała spółka < $300 mln"),
    "cluster3": (25, "Klaster 3+ insiderów"),
    "cluster2": (18, "Klaster 2 insiderów"),
    "plan": (-20, "Plan 10b5-1"),
    "placement": (-30, "Emisja / umowa ze spółką"),
    "fund": (-10, "Fundusz / podmiot"),
    "open_market": (5, "Potwierdzone zakupy na rynku"),
    "below_insider": (5, "Kurs poniżej ceny insidera"),
    "above_insider": (-5, "Kurs > 15% nad ceną insidera"),
    "penny": (-5, "Akcja < $2"),
    "after_drop": (10, "Zakup po spadku ≥ 30% od szczytu"),
    "near_low": (8, "Zakup blisko dołka 52-tyg."),
    "after_rally": (-5, "Zakup po rajdzie ≥ 30% w miesiąc"),
    "history_good": (10, "Insider z dobrą historią"),
    "history_bad": (-10, "Insider ze słabą historią"),
    "repeat": (5, "Insider kupował wcześniej"),
}
DEFAULT_WEIGHTS = {k: v[0] for k, v in FACTORS.items()}


def _role_factor(row: dict) -> tuple[str, str]:
    title = row["role"].lower()
    if re.search(r"\b(ceo|chief executive|president|chair)", title):
        return "role_ceo", "Kupuje CEO, prezes albo przewodniczący. Najlepiej zna perspektywy firmy."
    if re.search(r"\b(cfo|chief financial)", title):
        return "role_cfo", "Kupuje CFO. Zna finanse firmy od środka."
    if title.startswith("director"):  # role lists the officer title first, so this is a non-officer
        return "role_director", "Kupuje członek rady (director), który ma mniej bieżącej wiedzy niż zarząd."
    if title == "10% owner":
        return "role_10pct", "To tylko 10% udziałowiec, a nie członek zarządu."
    return "role_officer", "Kupuje członek zarządu."


def level_of(score: int) -> tuple[str, str]:
    level = "strong" if score >= 60 else "medium" if score >= 35 else "weak"
    return level, {"strong": "Silny", "medium": "Umiarkowany", "weak": "Słaby"}[level]


def analyze(row: dict, footnotes: str | None, market_cap: float | None, price_now: float | None,
            context: dict | None = None, history: dict | None = None, weights: dict | None = None) -> dict:
    """row: a view row from build_rows (role, owner, value, avg_price, cluster, plan, code)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    text = (footnotes or "").lower()
    reasons: list[tuple[str, str]] = []  # (sign "+"/"-"/"·", text)
    factors: list[str] = []
    score = 0

    def add(key: str, why: str | None = None) -> None:
        nonlocal score
        pts = round(w[key])
        score += pts
        factors.append(key)
        if why:
            reasons.append(("+" if pts > 0 else "-" if pts < 0 else "·", why))

    if row["code"] == "S":
        kind = "sale"
    elif any(re.search(p, text) for p in PLACEMENT_PATTERNS):
        kind = "placement"
    elif ENTITY_RE.search(row["owner"]):
        kind = "fund"
    else:
        kind = "insider"

    if kind == "sale":
        return {"score": None, "label": "—", "level": "none", "kind": kind, "factors": [],
                "kind_label": KINDS[kind][0], "kind_text": KINDS[kind][1], "reasons": []}

    add(*_role_factor(row))

    value = row["value"]
    if value >= 1_000_000:
        add("value_large", f"Duża kwota: ${value:,.0f}.")
    elif value >= 250_000:
        add("value_mid", f"Znacząca kwota: ${value:,.0f}.")
    else:
        add("value_small")

    if market_cap:
        pct = value / market_cap
        row["pct_mcap"] = pct
        if pct >= 0.05:
            add("mcap_huge", f"Zakup to aż {pct:.0%} kapitalizacji. Tyle rzadko kupuje się na rynku, "
                             "więc to raczej emisja albo przejęcie kontroli.")
        elif pct >= 0.005:
            add("mcap_high", f"Zakup to {pct:.2%} kapitalizacji spółki, czyli bardzo dużo.")
        elif pct >= 0.001:
            add("mcap_mid", f"Zakup to {pct:.2%} kapitalizacji spółki.")
        if market_cap < 300e6:
            add("small_cap", f"Mała spółka (${market_cap / 1e6:,.0f} mln): większy potencjał, ale i większe ryzyko.")

    if row["cluster"] >= 3:
        add("cluster3", f"Klaster: kupuje {row['cluster']} różnych insiderów naraz.")
    elif row["cluster"] == 2:
        add("cluster2", "Klaster: kupuje dwóch różnych insiderów.")

    if row["plan"]:
        add("plan", "Transakcja z planu 10b5-1, czyli zaplanowana z góry.")
    if kind == "placement":
        add("placement", "W przypisach jest umowa ze spółką albo emisja, a nie zakup na rynku.")
    elif kind == "fund":
        add("fund", "Kupującym jest podmiot (fundusz albo spółka), a nie osoba.")
    if any(re.search(p, text) for p in OPEN_MARKET_PATTERNS):
        add("open_market", "Przypisy potwierdzają zakupy na rynku, w wielu transakcjach.")

    if price_now and row["avg_price"]:
        diff = price_now / row["avg_price"] - 1
        if diff <= -0.03:
            add("below_insider", f"Kurs jest {abs(diff):.0%} poniżej ceny insidera, więc da się wejść taniej niż on.")
        elif diff >= 0.15:
            add("above_insider", f"Kurs już {diff:.0%} powyżej ceny insidera. Część ruchu mogła już się odbyć.")
    if price_now is not None and price_now < 2:
        add("penny", f"Akcja groszowa (${price_now:.2f}): duża zmienność i niska płynność.")

    if context:
        if context["from_high"] <= -0.30:
            add("after_drop", f"Kupuje po spadku o {abs(context['from_high']):.0%} od szczytu 52-tyg. "
                              "Insiderzy kupujący w dołku to jeden z najlepszych sygnałów.")
        if context["from_low"] <= 0.10:
            add("near_low", f"Kurs był tylko {context['from_low']:.0%} nad dołkiem 52-tyg.")
        if context["month_before"] is not None and context["month_before"] >= 0.30:
            add("after_rally", f"Przed zakupem kurs urósł o {context['month_before']:.0%} w miesiąc, "
                               "więc to raczej gonienie ruchu.")

    if history and history.get("count"):
        h3 = history["summary"].get(63) or {}
        if h3.get("n", 0) >= 2 and h3["avg"] >= 0.10:
            add("history_good", f"Poprzednie zakupy tej osoby ({h3['n']}) dawały średnio "
                                f"{h3['avg']:+.0%} po 3 mies.")
        elif h3.get("n", 0) >= 2 and h3["avg"] <= -0.10:
            add("history_bad", f"Poprzednie zakupy tej osoby ({h3['n']}) traciły średnio "
                               f"{h3['avg']:+.0%} po 3 mies.")
        else:
            add("repeat", f"Ta osoba kupowała już wcześniej ({history['count']}× w ciągu 3 lat).")

    score = max(0, min(100, score))
    level, label = level_of(score)
    return {"score": score, "label": label, "level": level, "kind": kind, "factors": factors,
            "kind_label": KINDS[kind][0], "kind_text": KINDS[kind][1], "reasons": reasons}

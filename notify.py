"""Telegram alerts for strong signals (sent from the GitHub Actions scan).

    TELEGRAM_BOT_TOKEN  bot token from @BotFather
    TELEGRAM_CHAT_ID    your chat id (getUpdates after messaging the bot)

Each alert has a stable key and is sent at most once (table `notifications`). On the
very first run nothing old is sent: everything current is marked as seen and a short
"connected" message goes out, so enabling the bot does not flood the chat.
"""

from __future__ import annotations

import html
import os
import sqlite3
import time
from datetime import datetime

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_PER_RUN = int(os.environ.get("NOTIFY_MAX_PER_RUN", "8"))
STOCK_MIN = int(os.environ.get("NOTIFY_STOCK_MIN", "60"))          # insider buy score
CRYPTO_MIN = int(os.environ.get("NOTIFY_CRYPTO_MIN", "70"))        # crypto move score
WHALE_TX_USD = float(os.environ.get("NOTIFY_WHALE_TX_USD", "5e6"))  # single whale transfer


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ["TELEGRAM_BOT_TOKEN"].strip()
        self.chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"].strip()
        self.session = requests.Session()

    def send(self, text: str) -> None:
        resp = self.session.post(API.format(token=self.token), timeout=20, data={
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if resp.status_code == 429:  # rate limited: Telegram says how long to wait
            time.sleep(min(30, resp.json().get("parameters", {}).get("retry_after", 5)))
            resp = self.session.post(API.format(token=self.token), timeout=20, data={
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if not resp.ok:  # never echo the URL: it contains the token
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.json().get('description', '')}")


class Outbox:
    """Which alerts were already sent."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")

    def sent(self) -> set[str]:
        with sqlite3.connect(self.path) as c:
            return {r[0] for r in c.execute("SELECT key FROM notifications")}

    def mark(self, keys: list[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(self.path) as c:
            c.executemany("INSERT OR IGNORE INTO notifications VALUES (?, ?)", [(k, now) for k in keys])


# ---------- alert builders (pure: rows in, (key, text) out) ----------

def esc(v) -> str:
    return html.escape(str(v), quote=False)


def money(v: float) -> str:
    for div, suf in [(1e9, " mld"), (1e6, " mln"), (1e3, " tys.")]:
        if abs(v) >= div:
            return f"${v / div:,.1f}{suf}"
    return f"${v:,.0f}"


def link(url: str | None, text: str) -> str:
    return f'<a href="{html.escape(url)}">{esc(text)}</a>' if url else esc(text)


def stock_alerts(rows: list[dict], site: str) -> list[tuple[str, str]]:
    out = []
    for r in rows:
        sig = r["signal"]
        if r["code"] != "P" or sig["score"] is None or sig["score"] < STOCK_MIN or sig["kind"] != "insider":
            continue
        reasons = "\n".join(f"• {esc(t)}" for s, t in sig["reasons"][:4] if s == "+")
        xtb = r.get("xtb") or {}
        buy = link(xtb.get("stock") or xtb.get("cfd"), "Kup na XTB ↗") if xtb else "brak na XTB"
        out.append((f"stock:{r['accession']}",
                    f"🟦 <b>Insider kupuje: {esc(r['ticker'])}</b> · ocena {sig['score']}/100\n"
                    f"{esc(r['issuer'])}\n{esc(r['owner'])} ({esc(r['role'])}) za <b>{money(r['value'])}</b> "
                    f"po ${r['avg_price']:,.2f}\n{reasons}\n"
                    f"{link(f'{site}/filing/{r['accession']}', 'Szczegóły')} · {buy}"))
    return out


def crypto_alerts(rows: list[dict], site: str, strategy_hits: dict[int, list[str]]) -> list[tuple[str, str]]:
    """Strong crypto moves, plus any move that a saved/preset strategy would enter right now."""
    out = []
    for r in rows:
        sig = r["signal"]
        strategies = strategy_hits.get(r["id"], [])
        if sig["score"] < CRYPTO_MIN and not strategies:
            continue
        news = r.get("news") or []
        lines = [f"🩷 <b>{esc(r['symbol'])}</b> ({esc(r['name'])}) · {esc(sig['kind_label'])} · ocena {sig['score']}/100",
                 f"24h {r['ch24']:+.0%} · wolumen {(r['vol_ratio'] or 0):.0%} kap. · kap. {money(r['mcap'])}"]
        if strategies:
            lines.append("Pasuje do strategii: " + ", ".join(esc(s) for s in strategies))
        lines += [f"• {esc(t)}" for s, t in sig["reasons"][:3]]
        if news:
            lines.append("📰 " + link(news[0]["url"], news[0]["title"][:120]))
        lines.append(link(f"{site}/krypto/{r['id']}", "Szczegóły") +
                     (" · " + link(r["xtb"], "CFD na XTB ↗") if r.get("xtb") else ""))
        out.append((f"crypto:{r['id']}", "\n".join(lines)))
    return out


def whale_alerts(data: dict, site: str) -> list[tuple[str, str]]:
    out = []
    for t in data["tokens"]:
        if t.get("signal"):
            reasons = "\n".join(f"• {esc(x)}" for s, x in t["reasons"][:4] if s == "+")
            out.append((f"whale_signal:{t['token']}",
                        f"🐋 <b>Sygnał spot: {esc(t['symbol'])}</b> ({esc(t.get('chain_label', ''))}) · ocena {t['score']}/100\n"
                        f"Akumuluje {t['acc']} wielorybów, napływ 30 dni {money(t['net30_usd'])}\n{reasons}\n"
                        + link(f"{site}/wieloryby/token/{t['token']}", "Szczegóły")
                        + (" · " + link(t["xtb"], "CFD na XTB ↗") if t.get("xtb") else "")))
    for b in data["big"]:
        if (b["usd"] or 0) >= WHALE_TX_USD and b["tone"] in ("up", "down"):
            who = b["whale"].get("label") or f"{b['address'][:6]}…{b['address'][-4:]}"
            out.append((f"whale_tx:{b['tx']}:{b['address']}",
                        f"{'🐋▲' if b['tone'] == 'up' else '🐋▼'} <b>{esc(b['symbol'])} {money(b['usd'])}</b>: {esc(b['text'])}\n"
                        f"Portfel {esc(who)} · {link(b.get('explorer', 'https://etherscan.io') + '/tx/' + b['tx'], 'transakcja')}"
                        f" · {link(f'{site}/wieloryby/portfel/{b['address']}', 'portfel')}"))
    return out


def deliver(alerts: list[tuple[str, str]], outbox: Outbox, telegram, max_per_run: int = MAX_PER_RUN) -> dict:
    """Send unsent alerts (oldest first in the given order). First run = baseline, nothing old is sent."""
    sent = outbox.sent()
    fresh = [(k, t) for k, t in alerts if k not in sent]
    if not sent:
        outbox.mark([k for k, _ in alerts] + ["__baseline__"])
        telegram.send("✅ <b>Insider Radar podłączony.</b>\nOd teraz dostaniesz tu nowe silne sygnały: zakupy insiderów, "
                      "ruchy krypto pasujące do strategii i ruchy wielorybów.")
        return {"sent": 0, "baseline": len(alerts), "skipped": 0}
    batch, skipped = fresh[:max_per_run], fresh[max_per_run:]
    for key, text in batch:
        telegram.send(text)
        outbox.mark([key])
    if skipped:  # don't let a backlog spam the chat later: mark it seen, say how many were skipped
        outbox.mark([k for k, _ in skipped])
        telegram.send(f"…i jeszcze {len(skipped)} sygnałów w tym skanie. Zobacz stronę.")
    return {"sent": len(batch), "baseline": 0, "skipped": len(skipped)}

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news  # noqa: E402
import notify  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def stock_row(acc="a-1", score=70, kind="insider", code="P", xtb=None):
    return {"accession": acc, "code": code, "ticker": "ACME", "issuer": "Acme & Sons <Corp>", "owner": "Doe John",
            "role": "CEO", "value": 1_500_000, "avg_price": 12.5, "xtb": xtb or {},
            "signal": {"score": score, "kind": kind, "reasons": [("+", "Kupuje CEO"), ("-", "minus")]}}


def test_stock_alerts_filter_and_escape():
    alerts = notify.stock_alerts([stock_row(), stock_row("a-2", score=40), stock_row("a-3", kind="fund"),
                                  stock_row("a-4", code="S")], "https://x.github.io/r")
    assert [k for k, _ in alerts] == ["stock:a-1"]
    text = alerts[0][1]
    assert "Acme &amp; Sons &lt;Corp&gt;" in text and "$1.5 mln" in text and "Kupuje CEO" in text
    assert "minus" not in text and 'href="https://x.github.io/r/filing/a-1"' in text and "brak na XTB" in text
    with_xtb = notify.stock_alerts([stock_row(xtb={"stock": "https://www.xtb.com/pl/akcje/acme"})], "s")[0][1]
    assert "Kup na XTB" in with_xtb


def crypto_row(rid=1, score=50, news_items=None):
    return {"id": rid, "symbol": "MOON", "name": "Moon", "ch24": 0.4, "vol_ratio": 0.8, "mcap": 20e6, "xtb": None,
            "news": news_items or [], "signal": {"score": score, "kind_label": "Pompa", "reasons": [("+", "Wolumen")]}}


def test_crypto_alerts_need_score_or_strategy_and_include_news():
    rows = [crypto_row(1, 80), crypto_row(2, 40), crypto_row(3, 40, [{"title": "Moon lists on X", "url": "https://n"}])]
    alerts = dict(notify.crypto_alerts(rows, "s", {3: ["Akumulacja wolumenu"]}))
    assert set(alerts) == {"crypto:1", "crypto:3"}
    assert "Akumulacja wolumenu" in alerts["crypto:3"] and "Moon lists on X" in alerts["crypto:3"]


def test_first_delivery_is_a_silent_baseline_then_only_new(tmp_path):
    box, tg = notify.Outbox(tmp_path / "n.db"), FakeTelegram()
    old = [("a", "A"), ("b", "B")]
    assert notify.deliver(old, box, tg) == {"sent": 0, "baseline": 2, "skipped": 0}
    assert len(tg.messages) == 1 and "podłączony" in tg.messages[0]
    assert notify.deliver(old + [("c", "C")], box, tg)["sent"] == 1 and tg.messages[-1] == "C"
    assert notify.deliver(old + [("c", "C")], box, tg)["sent"] == 0                 # never twice


def test_backlog_is_capped_and_marked(tmp_path):
    box, tg = notify.Outbox(tmp_path / "n.db"), FakeTelegram()
    notify.deliver([], box, tg)
    many = [(f"k{i}", f"T{i}") for i in range(12)]
    assert notify.deliver(many, box, tg, max_per_run=3) == {"sent": 3, "baseline": 0, "skipped": 9}
    assert "jeszcze 9" in tg.messages[-1]
    assert notify.deliver(many, box, tg, max_per_run=3)["sent"] == 0


def test_whale_alerts():
    data = {"tokens": [{"token": "0xa", "symbol": "LINK", "chain_label": "Ethereum", "score": 70, "acc": 3,
                        "net30_usd": 8e6, "reasons": [("+", "3 wieloryby dokupują")], "signal": {"price": 1}, "xtb": None},
                       {"token": "0xb", "symbol": "UNI", "signal": None}],
            "big": [{"usd": 6e6, "tone": "up", "tx": "0xt", "address": "0x" + "1" * 40, "symbol": "LINK",
                     "text": "Wypłata z giełdy", "whale": {"label": ""}, "explorer": "https://etherscan.io"},
                    {"usd": 9e6, "tone": "flat", "tx": "0xu", "address": "0x2", "symbol": "LINK", "text": "x",
                     "whale": {}, "explorer": ""}]}
    keys = [k for k, _ in notify.whale_alerts(data, "s")]
    assert keys == ["whale_signal:0xa", "whale_tx:0xt:0x" + "1" * 40]


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Starknet Jumps 20% After L1 Plan - Decrypt</title><source>Decrypt</source>
  <link>https://news.google.com/a</link><pubDate>Fri, 09 Oct 2026 16:36:03 GMT</pubDate></item>
<item><title>Bitcoin hits a record - CoinDesk</title><source>CoinDesk</source>
  <link>https://news.google.com/b</link><pubDate>Fri, 09 Oct 2026 17:00:00 GMT</pubDate></item>
<item><title>STRK unlock recalculated - CryptoTicker</title><source>CryptoTicker</source>
  <link>https://news.google.com/c</link><pubDate>Fri, 09 Oct 2026 18:47:10 GMT</pubDate></item>
</channel></rss>"""


def test_news_parse_keeps_headlines_about_the_coin():
    items = news.parse(RSS, "Starknet", "STRK")
    assert [i["title"] for i in items] == ["STRK unlock recalculated", "Starknet Jumps 20% After L1 Plan"]
    assert items[0]["source"] == "CryptoTicker" and items[0]["ts"] == "2026-10-09T18:47:10Z"
    assert news.query("Sea", "SE", 3) == '"Sea" (crypto OR token OR SE) when:3d'


def test_telegram_error_never_contains_token(monkeypatch):
    class Resp:
        status_code, ok = 401, False

        def json(self):
            return {"description": "Unauthorized"}

    tg = notify.Telegram("123:SECRET", "42")
    monkeypatch.setattr(tg.session, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError) as err:
        tg.send("x")
    assert "SECRET" not in str(err.value) and "401" in str(err.value)

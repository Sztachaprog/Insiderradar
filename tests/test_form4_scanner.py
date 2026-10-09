import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import form4_scanner as fs  # noqa: E402

FEED = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - ACME CORP (0000111111) (Issuer)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/111111/000222222226000001/0002222222-26-000001-index.htm"/>
  </entry>
  <entry>
    <title>4 - DOE JOHN (0002222222) (Reporting)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/2222222/000222222226000001/0002222222-26-000001-index.htm"/>
  </entry>
  <entry>
    <title>4 - BETA INC (0000333333) (Issuer)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/333333/000333333326000007/0000333333-26-000007-index.htm"/>
  </entry>
</feed>"""


def form4(code="P", shares="10000", price="25.50", officer="1", director="0",
          title="Chief Executive Officer", plan="0", ticker="acme", second_tx=""):
    return f"""<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <aff10b5One>{plan}</aff10b5One>
  <issuer>
    <issuerCik>0000111111</issuerCik>
    <issuerName>ACME CORP</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0002222222</rptOwnerCik><rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{director}</isDirector>
      <isOfficer>{officer}</isOfficer>
      <officerTitle>{title}</officerTitle>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-10-07</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    {second_tx}
  </nonDerivativeTable>
</ownershipDocument>"""


def parse(**kw):
    return fs.parse_form4(form4(**kw), "0002222222-26-000001")


def test_feed_dedupes_issuer_and_reporting_entries():
    assert fs.parse_feed(FEED) == [
        ("111111", "0002222222-26-000001"),
        ("333333", "0000333333-26-000007"),
    ]


def test_extracts_xml_from_full_submission():
    txt = f"<SEC-DOCUMENT>\n<TYPE>4\n<XML>\n<?xml version='1.0'?>\n{form4()}\n</XML>\n</SEC-DOCUMENT>"
    xml = fs.extract_ownership_xml(txt)
    assert xml.startswith("<ownershipDocument>") and xml.endswith("</ownershipDocument>")


def test_extract_returns_none_without_ownership_doc():
    assert fs.extract_ownership_xml("<SEC-DOCUMENT>nothing</SEC-DOCUMENT>") is None


def test_parses_core_fields():
    f = parse()
    assert (f.ticker, f.owner, f.role) == ("ACME", "Doe John", "Chief Executive Officer")
    assert f.total_value("P") == pytest.approx(255_000)


def test_true_false_flags_are_accepted():
    f = parse(officer="false", director="true", title="")
    assert f.role == "Director"


def test_price_as_footnote_only_counts_as_zero():
    xml = form4().replace("<transactionPricePerShare><value>25.50</value></transactionPricePerShare>",
                          '<transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>')
    f = fs.parse_form4(xml, "x")
    assert f.transactions[0].price == 0 and f.total_value("P") == 0


def test_sums_multiple_purchases():
    extra = """<nonDerivativeTransaction>
      <transactionDate><value>2026-10-08</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>2000</value></transactionShares>
      <transactionPricePerShare><value>25</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>"""
    assert parse(second_tx=extra).total_value("P") == pytest.approx(305_000)


@pytest.mark.parametrize("kwargs, expected", [
    ({}, True),                                      # CEO buys $255k
    ({"shares": "1000"}, False),                     # $25.5k, below threshold
    ({"code": "S"}, False),                          # sale, not a buy
    ({"code": "A"}, False),                          # stock award
    ({"officer": "0", "director": "0"}, False),      # not an insider
    ({"plan": "1"}, False),                          # pre-scheduled 10b5-1 trade
])
def test_filter_buys(kwargs, expected):
    assert fs.matches(parse(**kwargs), "P", 100_000, insiders_only=True, skip_10b5_1=True) is expected


def test_zero_threshold_still_requires_matching_code():
    assert not fs.matches(parse(code="S"), "P", 0, insiders_only=True, skip_10b5_1=True)


def test_flags_can_be_relaxed():
    f = parse(officer="0", director="0", plan="1")
    assert fs.matches(f, "P", 100_000, insiders_only=False, skip_10b5_1=False)


def test_state_roundtrip(tmp_path):
    path = tmp_path / "seen.json"
    fs.save_seen(path, {"b", "a"})
    assert json.loads(path.read_text()) == ["a", "b"]
    assert fs.load_seen(path) == {"a", "b"}
    assert fs.load_seen(tmp_path / "missing.json") == set()


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, **params):
        self.urls.append(url)
        if "browse-edgar" in url:
            return FEED
        return self.responses[url]


def test_scan_end_to_end_skips_seen_and_records_new():
    acme_url = "https://www.sec.gov/Archives/edgar/data/111111/000222222226000001/0002222222-26-000001.txt"
    client = FakeClient({acme_url: f"<XML>{form4()}</XML>"})
    seen = {"0000333333-26-000007"}

    filings = fs.scan(client, pages=1, seen=seen)

    assert [f.ticker for f in filings] == ["ACME"]
    assert "0002222222-26-000001" in seen
    assert not any("333333" in u and u.endswith(".txt") for u in client.urls)


def test_main_requires_user_agent(monkeypatch, capsys):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert fs.main([]) == 2
    assert "SEC_USER_AGENT" in capsys.readouterr().err

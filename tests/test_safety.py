import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci  # noqa: E402
import safety  # noqa: E402
import whales  # noqa: E402

LEAK = ("HTTPError: 402 Client Error: Payment Required for url: "
        "https://api.blockscout.com/8453/api/v2/tokens?type=ERC-20&apikey=proapi_SECRET-123_x")


def test_scrub_removes_query_strings_and_keys():
    out = safety.scrub(LEAK + " and CG-abcdefghijklmnop and token=xyz")
    assert "proapi_" not in out and "SECRET" not in out and "CG-abc" not in out and "xyz" not in out
    assert out.startswith("HTTPError: 402") and "api.blockscout.com/8453/api/v2/tokens" in out


def test_safe_error_for_http_errors_keeps_status_only():
    resp = requests.Response()
    resp.status_code = 402
    resp.url = "https://api.blockscout.com/137/api/v2/tokens?apikey=proapi_SECRET"
    err = requests.HTTPError("402 Client Error for url: " + resp.url, response=resp)
    assert safety.safe_error(err) == "HTTP 402 (https://api.blockscout.com/137/api/v2/tokens)"
    assert "SECRET" not in safety.safe_error(RuntimeError("boom ?apikey=proapi_SECRET"))


def test_key_pasted_as_whole_url_still_works(monkeypatch):
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "https://api.blockscout.com/1/api/v2/x?apikey=proapi_k-1_A")
    assert whales.api_key() == "proapi_k-1_A"
    assert whales.BlockscoutClient("base").params == {"apikey": "proapi_k-1_A"}


def test_old_status_with_leaked_key_is_scrubbed(tmp_path, monkeypatch):
    status = tmp_path / "scan_status.json"
    status.write_text('{"whales": {"last_error": "%s", "last_new": 3}}' % LEAK)
    monkeypatch.setattr(ci, "STATUS", status)
    data = ci.read_status()
    assert "SECRET" not in data["whales"]["last_error"] and data["whales"]["last_new"] == 3

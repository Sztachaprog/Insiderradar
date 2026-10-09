import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci  # noqa: E402


@pytest.mark.parametrize("content, expected", [
    (None, {}),                                   # first run: no file on the data branch
    ("", {}),                                     # empty file (the bug that failed the first CI run)
    ("not json", {}),
    ("[1, 2]", {}),
    ('{"crypto": {"last_run": "19:46:00"}}', {"crypto": {"last_run": "19:46:00"}}),
])
def test_read_status_tolerates_missing_or_broken_file(tmp_path, monkeypatch, content, expected):
    status = tmp_path / "scan_status.json"
    if content is not None:
        status.write_text(content)
    monkeypatch.setattr(ci, "STATUS", status)
    assert ci.read_status() == expected


def test_export_step_restores_status_and_ignores_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FORM4_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(ci.webapp, "DB_PATH", tmp_path / "t.db")
    status = tmp_path / "scan_status.json"
    status.write_text('{"crypto": {"last_run": "19:46:00", "last_new": 3}, "bogus": {"x": 1}}')
    monkeypatch.setattr(ci, "STATUS", status)
    parts = ci.scan(set(), force_whales=False)
    assert parts[5].last_run == "19:46:00" and parts[5].last_new == 3

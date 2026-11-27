"""UI dashboard and output format tests."""

from __future__ import annotations

import io

from sitecheck.results import format_line, OutputWriter
from sitecheck.ui import Dashboard, Theme, fmt_duration, terminal_size, read_key
from sitecheck.runner import Runner


def test_format_line_fields():
    line = format_line("https://a.com", "https://b.com/", 200, "VALID", "HTTP 200", "http://p:1", 0)
    parts = line.split("\t")
    assert parts[0] == "https://a.com"
    assert parts[1] == "https://b.com/"
    assert parts[2] == "200"
    assert parts[3] == "VALID"
    assert parts[4] == "HTTP 200"
    assert parts[5] == "http://p:1"
    assert "T" in parts[6]  # timestamp


def test_format_line_sanitizes_reason():
    line = format_line("u", "u", None, "ERROR", "bad\treason\nhere", None, 0)
    assert "\tbad reason here\t" in line


def test_writer_routes_categories(tmp_path):
    w = OutputWriter(str(tmp_path))
    r_valid = Runner  # placeholder to keep import used
    from sitecheck.checker import CheckResult

    w.write_result(CheckResult("https://a.com", "https://a.com/", "VALID", "VALID", 200, "ok"))
    w.write_result(CheckResult("https://b.com", "https://b.com/", "DEAD", "DEAD", 404, "404"))
    w.write_result(CheckResult("https://c.com", "https://c.com/", "ERROR", "ERROR", 500, "500"))
    w.write_result(CheckResult("https://d.com", "https://d.com/", "DEFERRED", "DEFERRED", None, "proxy down", deferred=True))
    w.close()
    names = {p.name for p in tmp_path.iterdir()}
    assert {"success.txt", "dead.txt", "errors.txt", "proxy_failures.txt"} <= names
    # deferred URL must NOT appear in any website category file
    for name in ("success.txt", "dead.txt", "errors.txt"):
        content = (tmp_path / name).read_text()
        assert "https://d.com" not in content
    assert "https://d.com" in (tmp_path / "proxy_failures.txt").read_text()


def test_dashboard_rewrites_only_changed_lines():
    buf = io.StringIO()
    dash = Dashboard(Theme(True), out=buf)
    dash.start()
    snap = {
        "version": "1.0.0", "mode": "simple", "preset": "balanced",
        "paused": False, "processed": 10, "total": 100, "remaining": 90,
        "by_category": {"VALID": 10}, "elapsed": 5.0, "eta": 45.0,
        "cpm": 100.0, "avg_cpm": 100.0, "peak_cpm": 120.0,
        "active_workers": 4, "proxy_failures": 0, "error": None,
    }
    buf.seek(0)
    buf.truncate(0)
    dash.render(snap, Theme(True))
    first = buf.getvalue()
    assert "Progress" in first
    # identical frame: nothing rewritten
    buf.seek(0)
    buf.truncate(0)
    dash.render(snap, Theme(True))
    assert buf.getvalue() == ""
    # only the changed lines get rewritten
    snap2 = dict(snap, processed=11)
    buf.seek(0)
    buf.truncate(0)
    dash.render(snap2, Theme(True))
    changed = buf.getvalue()
    assert "11" in changed
    assert "State" not in changed
    dash.finish()


def test_fmt_duration():
    assert fmt_duration(0) == "00:00:00"
    assert fmt_duration(3725) == "01:02:05"
    assert fmt_duration(float("inf")) == "--:--:--"


def test_read_key_non_tty_returns_none():
    import sys

    if not sys.stdin.isatty():
        assert read_key(0.01) is None


def test_terminal_size():
    cols, rows = terminal_size()
    assert cols > 0 and rows > 0

"""
Tests for src.dashboard.data — Phase 11B.

The data layer is the dashboard's only contact with the filesystem. If
these break, the page won't render correctly; if these are robust, the
server stays up even when STATUS.json or the log file are missing,
malformed, or being rotated.
"""

import json
import pathlib
import time

import pytest

from src.dashboard.data import (
    DEFAULT_STALE_AFTER_S,
    build_view,
    read_status,
    status_age_seconds,
    tail_lines,
)


# ---------------------------------------------------------------------------
# read_status
# ---------------------------------------------------------------------------


def test_read_status_returns_dict_for_valid_file(tmp_path: pathlib.Path):
    p = tmp_path / "STATUS.json"
    p.write_text(json.dumps({"env_mode": "testnet", "uptime_seconds": 5.0}))
    assert read_status(p) == {"env_mode": "testnet", "uptime_seconds": 5.0}


def test_read_status_returns_none_for_missing_file(tmp_path: pathlib.Path):
    assert read_status(tmp_path / "does_not_exist.json") is None


def test_read_status_returns_none_for_invalid_json(tmp_path: pathlib.Path):
    p = tmp_path / "STATUS.json"
    p.write_text("{not valid json")
    assert read_status(p) is None


def test_read_status_returns_none_for_truncated_file(tmp_path: pathlib.Path):
    p = tmp_path / "STATUS.json"
    p.write_text('{"env_mode": "testnet"')  # missing closing brace
    assert read_status(p) is None


# ---------------------------------------------------------------------------
# status_age_seconds
# ---------------------------------------------------------------------------


def test_status_age_seconds_returns_none_when_file_missing(tmp_path: pathlib.Path):
    assert status_age_seconds(tmp_path / "missing.json") is None


def test_status_age_seconds_reflects_recent_mtime(tmp_path: pathlib.Path):
    p = tmp_path / "STATUS.json"
    p.write_text("{}")
    age = status_age_seconds(p, now=time.time() + 3.0)
    assert age is not None
    # mtime is "just now", we've forwarded clock 3s, so age ≈ 3s
    assert 2.0 <= age <= 4.5


def test_status_age_seconds_clamps_to_zero_for_future_mtime(tmp_path: pathlib.Path):
    """
    If the file mtime is somehow newer than `now` (clock skew, NFS), we
    return 0 rather than a negative age — the template formats it as a
    duration.
    """
    p = tmp_path / "STATUS.json"
    p.write_text("{}")
    age = status_age_seconds(p, now=time.time() - 100.0)
    assert age == 0.0


# ---------------------------------------------------------------------------
# tail_lines
# ---------------------------------------------------------------------------


def test_tail_lines_returns_empty_for_missing_file(tmp_path: pathlib.Path):
    assert tail_lines(tmp_path / "missing.log", 10) == []


def test_tail_lines_returns_empty_for_empty_file(tmp_path: pathlib.Path):
    p = tmp_path / "log.txt"
    p.write_text("")
    assert tail_lines(p, 10) == []


def test_tail_lines_returns_empty_when_n_is_zero_or_negative(tmp_path: pathlib.Path):
    p = tmp_path / "log.txt"
    p.write_text("a\nb\nc\n")
    assert tail_lines(p, 0) == []
    assert tail_lines(p, -5) == []


def test_tail_lines_returns_all_lines_when_n_exceeds_file(tmp_path: pathlib.Path):
    p = tmp_path / "log.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    assert tail_lines(p, 50) == ["alpha", "beta", "gamma"]


def test_tail_lines_returns_last_n_lines(tmp_path: pathlib.Path):
    p = tmp_path / "log.txt"
    body = "\n".join(f"line-{i:03d}" for i in range(200)) + "\n"
    p.write_text(body)
    out = tail_lines(p, 5)
    assert out == ["line-195", "line-196", "line-197", "line-198", "line-199"]


def test_tail_lines_handles_no_trailing_newline(tmp_path: pathlib.Path):
    p = tmp_path / "log.txt"
    p.write_text("first\nsecond\nlast-without-newline")
    assert tail_lines(p, 2) == ["second", "last-without-newline"]


def test_tail_lines_handles_long_lines_spanning_chunks(tmp_path: pathlib.Path):
    """
    A single line >> chunk size must still be emitted intact. This stresses
    the backwards-read assembly code.
    """
    p = tmp_path / "log.txt"
    long_line = "x" * 12_000   # spans ~3 chunks of 4096
    p.write_text(f"first\n{long_line}\nlast\n")
    out = tail_lines(p, 3)
    assert out == ["first", long_line, "last"]


def test_tail_lines_replaces_invalid_utf8(tmp_path: pathlib.Path):
    """
    Mangled bytes in the log must not crash the dashboard. Replacement
    char (�) is fine — operator can still read surrounding context.
    """
    p = tmp_path / "log.txt"
    p.write_bytes(b"good line\nbad: \xff\xfe text\nfinal\n")
    out = tail_lines(p, 3)
    assert out[0] == "good line"
    assert out[2] == "final"
    # Middle line still readable around the mangled bytes
    assert "text" in out[1]


# ---------------------------------------------------------------------------
# build_view
# ---------------------------------------------------------------------------


def _sample_status() -> dict:
    return {
        "env_mode": "testnet",
        "uptime_seconds": 305.0,
        "utc_day": "2026-04-29",
        "per_symbol": {
            "BTCUSDT": {"placed_total": 4, "filled_total": 0,
                        "recentered_total": 0, "blocked_by_risk": 0,
                        "paused_by_liquidity": 0, "paused_by_sentiment": 0},
        },
        "risk": {
            "halted": False, "halt_reason": None, "open_order_count": 4,
            "realized_pnl_24h": 0.0, "daily_loss_limit_usd": 500.0,
        },
    }


def test_build_view_with_no_snapshot():
    view = build_view(
        status=None, status_age_s=None, log_lines=[],
        halt_path_exists=False,
    )
    assert view["snapshot_present"] is False
    assert view["snapshot_age_seconds"] is None
    assert view["snapshot_is_stale"] is False
    assert view["status"] == {}
    assert view["halted"] is False
    assert view["halt_sentinel_present"] is False
    assert view["log_lines"] == []
    assert view["log_line_count"] == 0


def test_build_view_with_fresh_snapshot():
    s = _sample_status()
    view = build_view(
        status=s, status_age_s=4.7,
        log_lines=["one", "two"], halt_path_exists=False,
    )
    assert view["snapshot_present"] is True
    assert view["snapshot_age_seconds"] == 4.7
    assert view["snapshot_is_stale"] is False
    assert view["status"] == s
    assert view["halted"] is False
    assert view["log_line_count"] == 2


def test_build_view_marks_stale_when_age_exceeds_threshold():
    s = _sample_status()
    view = build_view(
        status=s, status_age_s=DEFAULT_STALE_AFTER_S + 1,
        log_lines=[], halt_path_exists=False,
    )
    assert view["snapshot_is_stale"] is True


def test_build_view_pulls_halted_from_risk_block():
    s = _sample_status()
    s["risk"]["halted"] = True
    s["risk"]["halt_reason"] = "kill switch file present: runtime/HALT"
    view = build_view(
        status=s, status_age_s=1.0, log_lines=[],
        halt_path_exists=True,
    )
    assert view["halted"] is True
    assert view["halt_sentinel_present"] is True


def test_build_view_tolerates_status_missing_risk_block():
    """A bot that crashed before its first heartbeat may have written nothing
    or a partial dict. View still renders."""
    view = build_view(
        status={"env_mode": "testnet"}, status_age_s=1.0,
        log_lines=[], halt_path_exists=False,
    )
    assert view["halted"] is False
    assert view["status"] == {"env_mode": "testnet"}

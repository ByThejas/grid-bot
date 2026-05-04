"""
Tests for the early-startup HALT-sentinel check in grid_runner.

Background — Phase 11D dev session on 2026-05-04 surfaced a
boot-halt-restart storm: when /halt creates runtime/HALT and the bot
exits, systemd's `Restart=always` brings it right back. The runner
constructs the alerter, fires [BOOT], the risk monitor sees the
sentinel, fires [HALT], shutdown fires [STOP], and systemd loops the
whole thing every ~5 seconds. Result: ~50 Telegram alerts per minute
until the operator notices.

The fix is two-sided:
  1. Runner: bail out before any alerter / task setup if the sentinel
     is present. Exit code 3 (specific to this case).
  2. systemd unit: RestartPreventExitStatus=3 so systemd respects the
     deliberate stop and doesn't restart.

This file tests side 1 — the runner-side abort behaviour.
"""

import pathlib

import pytest

from src.grid.grid_runner import (
    _EXIT_HALT_SENTINEL_PRESENT,
    _abort_if_halt_sentinel_present,
)


def test_abort_exits_with_code_3_when_sentinel_present(tmp_path: pathlib.Path):
    """
    With a HALT file present, the helper must call sys.exit with the
    well-known code 3. Without that exit code, systemd's
    RestartPreventExitStatus=3 directive can't distinguish this case
    from a normal crash.
    """
    sentinel = tmp_path / "HALT"
    sentinel.touch()

    with pytest.raises(SystemExit) as exc_info:
        _abort_if_halt_sentinel_present(sentinel)

    assert exc_info.value.code == _EXIT_HALT_SENTINEL_PRESENT
    assert _EXIT_HALT_SENTINEL_PRESENT == 3   # contract: must stay 3 for systemd


def test_abort_returns_silently_when_sentinel_absent(tmp_path: pathlib.Path):
    """
    Sentinel missing → function returns without raising. This is the
    happy-path case on every normal startup.
    """
    sentinel = tmp_path / "HALT"   # not created
    # Should NOT raise. If it does, pytest will surface the exception.
    result = _abort_if_halt_sentinel_present(sentinel)
    assert result is None


def test_abort_treats_directory_as_present_too(tmp_path: pathlib.Path):
    """
    If runtime/HALT happens to be a directory (rare but possible if the
    operator did `mkdir runtime/HALT` by mistake), pathlib.Path.exists()
    returns True and we should still bail out. This documents the
    intended behaviour: any kind of presence at that path is "halted".
    """
    sentinel_as_dir = tmp_path / "HALT"
    sentinel_as_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        _abort_if_halt_sentinel_present(sentinel_as_dir)
    assert exc_info.value.code == _EXIT_HALT_SENTINEL_PRESENT


def test_abort_logs_path_in_error_message(tmp_path: pathlib.Path, capsys):
    """
    The stderr message must include the sentinel's path so the operator
    knows exactly what to remove. We capture stderr (capsys) and assert
    on it; without this hint the operator sees a cryptic "exit code 3"
    in journalctl and has no idea what to do.
    """
    sentinel = tmp_path / "some_unique_name_HALT"
    sentinel.touch()

    with pytest.raises(SystemExit):
        _abort_if_halt_sentinel_present(sentinel)

    captured = capsys.readouterr()
    assert "some_unique_name_HALT" in captured.err
    assert "Remove it" in captured.err   # actionable hint included

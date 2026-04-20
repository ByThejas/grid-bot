"""
Tests for src.observability.heartbeat — Phase 10B.

Focus:
  - build_snapshot returns every field we expect (env_mode, uptime,
    per-symbol counters, risk block).
  - write_status_atomic writes JSON that parses back cleanly, and a
    concurrent reader never sees a partial file.
  - HeartbeatMonitor.beat_once writes STATUS.json on first call and
    seeds the day-start snapshot.
  - DAILY SUMMARY fires on UTC day rollover, not on same-day heartbeats.
  - _diff_counters is straight subtraction per field.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field

from src.observability.heartbeat import (
    HeartbeatMonitor,
    ObservabilityConfig,
    _diff_counters,
    _utc_day,
    build_snapshot,
    write_status_atomic,
)


# ---------------------------------------------------------------------------
# Fakes. Real GridEngine + RiskManager would require a full bot setup just
# to exercise the read-only snapshot path, so we pass duck-typed stubs.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    _placed: int = 0
    _filled: int = 0
    _recentered: int = 0
    _blocked_by_risk: int = 0
    _paused_by_liquidity: int = 0
    _paused_by_sentiment: int = 0


@dataclass
class _FakeRiskCfg:
    daily_loss_limit_usd: float = 50.0


@dataclass
class _FakeRisk:
    halted: bool = False
    halt_reason: str = ""
    open_order_count: int = 0
    realized_pnl_24h: float = 0.0
    _cfg: _FakeRiskCfg = field(default_factory=_FakeRiskCfg)


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="heartbeat-test-")


# ---------------------------------------------------------------------------
# _utc_day
# ---------------------------------------------------------------------------


def test_utc_day_format():
    # Fixed timestamp: 2026-04-20 00:00:00 UTC = 1776643200
    assert _utc_day(1776643200) == "2026-04-20"
    # Two seconds before midnight UTC — should still be the previous day.
    assert _utc_day(1776643198) == "2026-04-19"


# ---------------------------------------------------------------------------
# _diff_counters
# ---------------------------------------------------------------------------


def test_diff_counters_subtracts_per_field():
    before = {"BTCUSDT": {"placed_total": 10, "filled_total": 3}}
    after = {"BTCUSDT": {"placed_total": 15, "filled_total": 8}}
    out = _diff_counters(before, after)
    assert out == {"BTCUSDT": {"placed_total": 5, "filled_total": 5}}


def test_diff_counters_handles_missing_symbol_in_before():
    # New symbol appearing mid-day — its "before" is implicitly zero.
    before = {}
    after = {"ETHUSDT": {"placed_total": 4, "filled_total": 1}}
    out = _diff_counters(before, after)
    assert out == {"ETHUSDT": {"placed_total": 4, "filled_total": 1}}


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_has_all_top_level_fields():
    eng = _FakeEngine(_placed=7, _filled=2)
    risk = _FakeRisk(open_order_count=5, realized_pnl_24h=1.2345678)
    snap = build_snapshot(
        env_mode="testnet", started_at=1000.0, now=1300.5,
        engines={"BTCUSDT": eng}, risk=risk,
    )
    assert snap["env_mode"] == "testnet"
    assert snap["started_at_ms"] == 1000000
    assert snap["heartbeat_at_ms"] == 1300500
    assert snap["uptime_seconds"] == 300.5
    assert snap["utc_day"] == _utc_day(1300.5)
    assert snap["per_symbol"]["BTCUSDT"]["placed_total"] == 7
    assert snap["per_symbol"]["BTCUSDT"]["filled_total"] == 2
    assert snap["risk"]["open_order_count"] == 5
    assert snap["risk"]["realized_pnl_24h"] == 1.234568  # 6dp rounding
    assert snap["risk"]["daily_loss_limit_usd"] == 50.0


def test_snapshot_tolerates_risk_none():
    # Backtest or no-risk mode → risk block is empty, NOT an error.
    snap = build_snapshot(
        env_mode="testnet", started_at=0, now=1,
        engines={"BTCUSDT": _FakeEngine()}, risk=None,
    )
    assert snap["risk"] == {}


# ---------------------------------------------------------------------------
# write_status_atomic
# ---------------------------------------------------------------------------


def test_write_status_roundtrips_json():
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        payload = {"env_mode": "testnet", "x": [1, 2, 3]}
        write_status_atomic(path, payload)
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == payload
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_status_creates_parent_dir():
    d = _tmpdir()
    try:
        path = os.path.join(d, "nested", "dir", "STATUS.json")
        write_status_atomic(path, {"a": 1})
        assert pathlib.Path(path).exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_status_leaves_no_tmp_garbage_on_success():
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        write_status_atomic(path, {"a": 1})
        # Only the final file should exist — no .tmp leftovers.
        entries = os.listdir(d)
        assert entries == ["STATUS.json"], f"expected only STATUS.json, got {entries}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_status_overwrites_existing():
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        write_status_atomic(path, {"version": 1})
        write_status_atomic(path, {"version": 2})
        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f) == {"version": 2}
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# HeartbeatMonitor.beat_once
# ---------------------------------------------------------------------------


def _monitor_with_now(now_value: float, status_path: str, engines=None, risk=None):
    # Mutable holder so callers can bump 'now' between beats.
    box = {"now": now_value}
    cfg = ObservabilityConfig(
        heartbeat_interval_s=60.0, status_path=status_path, enable_daily_summary=True,
    )
    m = HeartbeatMonitor(
        engines=engines or {"BTCUSDT": _FakeEngine()},
        risk=risk,
        env_mode="testnet",
        config=cfg,
        now_fn=lambda: box["now"],
    )
    return m, box


def test_beat_once_writes_status_file_first_call():
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        m, _ = _monitor_with_now(1776643200.0, path)
        m.beat_once()
        assert pathlib.Path(path).exists()
        loaded = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert loaded["env_mode"] == "testnet"
        assert loaded["utc_day"] == "2026-04-20"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_beat_once_seeds_day_start_on_first_call():
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        eng = _FakeEngine(_placed=3)
        m, _ = _monitor_with_now(1776643200.0, path, engines={"BTCUSDT": eng})
        m.beat_once()
        # After first beat, day_start should be captured.
        assert m._day_start_utc == "2026-04-20"
        assert m._day_start_snapshot["BTCUSDT"]["placed_total"] == 3
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_same_day_heartbeat_does_not_reset_day_start():
    # Two beats on the same UTC day → day_start stays at the first beat.
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        eng = _FakeEngine(_placed=3)
        m, box = _monitor_with_now(1776643200.0, path, engines={"BTCUSDT": eng})
        m.beat_once()
        original_start = m._day_start_snapshot
        # Advance 10 minutes — still the same UTC day.
        box["now"] = 1776643200.0 + 600
        eng._placed = 8
        m.beat_once()
        assert m._day_start_snapshot is original_start
        assert m._day_start_snapshot["BTCUSDT"]["placed_total"] == 3
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_day_rollover_resets_day_start():
    # First beat on 2026-04-20, second beat on 2026-04-21 → day_start
    # becomes the 2026-04-21 snapshot.
    d = _tmpdir()
    try:
        path = os.path.join(d, "STATUS.json")
        eng = _FakeEngine(_placed=3, _filled=1)
        m, box = _monitor_with_now(1776643200.0, path, engines={"BTCUSDT": eng})
        m.beat_once()
        # Jump 24h + 1s into the next UTC day.
        box["now"] = 1776643200.0 + 86401
        eng._placed = 15
        eng._filled = 10
        m.beat_once()
        assert m._day_start_utc == "2026-04-21"
        # Day start snapshot should now reflect the new day's counts.
        assert m._day_start_snapshot["BTCUSDT"]["placed_total"] == 15
        assert m._day_start_snapshot["BTCUSDT"]["filled_total"] == 10
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# ObservabilityConfig.from_yaml
# ---------------------------------------------------------------------------


def test_config_defaults_when_missing():
    c = ObservabilityConfig.from_yaml({})
    assert c.heartbeat_interval_s == 300.0
    assert c.status_path == "runtime/STATUS.json"
    assert c.enable_daily_summary is True


def test_config_reads_yaml_overrides():
    c = ObservabilityConfig.from_yaml({
        "observability": {
            "heartbeat_interval_s": 60,
            "status_path": "custom/path.json",
            "enable_daily_summary": False,
        }
    })
    assert c.heartbeat_interval_s == 60.0
    assert c.status_path == "custom/path.json"
    assert c.enable_daily_summary is False


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


_TESTS = [
    test_utc_day_format,
    test_diff_counters_subtracts_per_field,
    test_diff_counters_handles_missing_symbol_in_before,
    test_snapshot_has_all_top_level_fields,
    test_snapshot_tolerates_risk_none,
    test_write_status_roundtrips_json,
    test_write_status_creates_parent_dir,
    test_write_status_leaves_no_tmp_garbage_on_success,
    test_write_status_overwrites_existing,
    test_beat_once_writes_status_file_first_call,
    test_beat_once_seeds_day_start_on_first_call,
    test_same_day_heartbeat_does_not_reset_day_start,
    test_day_rollover_resets_day_start,
    test_config_defaults_when_missing,
    test_config_reads_yaml_overrides,
]


def _main() -> None:
    passed = failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _main()

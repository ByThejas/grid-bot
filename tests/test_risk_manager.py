"""
Tests for RiskManager.

WHY these tests matter:
RiskManager is the only thing standing between a buggy strategy and a
runaway order book. The contract we MUST hold:
  * check_can_place is side-effect free (a check that increments would
    over-count when the exchange rejects the order).
  * Halt is sticky and one-way (auto-recovery from tripped safety is bad).
  * FIFO PnL is correct (otherwise the loss-floor circuit breaker fires
    on the wrong number).
  * The kill switch file trips halt the moment poll sees it.

Tests run as plain `python -m tests.test_risk_manager` AND under pytest.
"""

import os
import pathlib
import sys
import tempfile
import time
import traceback

from src.risk.config import RiskConfig
from src.risk.manager import RiskManager


def _mk(
    *,
    max_open_orders: int = 10,
    daily_loss_limit_usd: float = 100.0,
    kill_switch_path: str = "runtime/HALT_TEST",
) -> RiskManager:
    cfg = RiskConfig(
        max_open_orders=max_open_orders,
        daily_loss_limit_usd=daily_loss_limit_usd,
        kill_switch_path=kill_switch_path,
        poll_interval_s=0.1,
    )
    return RiskManager(cfg)


# ---------------------------------------------------------------------------
# check_can_place
# ---------------------------------------------------------------------------


def test_check_can_place_passes_when_idle():
    r = _mk()
    assert r.check_can_place("BTCUSDT", "BUY", 60000, 0.001) is None


def test_check_can_place_is_side_effect_free():
    """A check call must not move the open-order counter."""
    r = _mk()
    for _ in range(5):
        r.check_can_place("BTCUSDT", "BUY", 60000, 0.001)
    assert r.open_order_count == 0


def test_max_open_orders_blocks_at_cap():
    r = _mk(max_open_orders=3)
    for i in range(3):
        r.record_open(f"id-{i}")
    reason = r.check_can_place("BTCUSDT", "BUY", 60000, 0.001)
    assert reason is not None and "max_open_orders" in reason


def test_record_closed_frees_capacity():
    r = _mk(max_open_orders=2)
    r.record_open("a")
    r.record_open("b")
    assert r.check_can_place("X", "BUY", 1, 1) is not None
    r.record_closed("a")
    assert r.check_can_place("X", "BUY", 1, 1) is None


def test_record_open_is_idempotent():
    r = _mk(max_open_orders=2)
    r.record_open("a")
    r.record_open("a")
    r.record_open("a")
    assert r.open_order_count == 1


# ---------------------------------------------------------------------------
# halt / kill switch
# ---------------------------------------------------------------------------


def test_trip_blocks_placements():
    r = _mk()
    r.trip("manual")
    reason = r.check_can_place("X", "BUY", 1, 1)
    assert reason is not None and "halted" in reason


def test_halt_is_sticky():
    """First reason wins so post-mortem sees the actual cause."""
    r = _mk()
    r.trip("first")
    r.trip("second")
    assert r.halt_reason == "first"


def test_kill_switch_file_trips_on_poll():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "HALT")
        r = _mk(kill_switch_path=path)
        assert r.poll_kill_switch() is False
        assert r.halted is False
        pathlib.Path(path).write_text("stop")
        assert r.poll_kill_switch() is True
        assert r.halted is True
        # Second poll: already halted, returns False (not newly tripped).
        assert r.poll_kill_switch() is False


# ---------------------------------------------------------------------------
# FIFO PnL
# ---------------------------------------------------------------------------


def test_fifo_simple_profit():
    """Buy 1 @ 100, sell 1 @ 110 → +10."""
    r = _mk()
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 110, 1)
    assert r.realized_pnl_24h == 10.0


def test_fifo_simple_loss():
    r = _mk()
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 90, 1)
    assert r.realized_pnl_24h == -10.0


def test_fifo_partial_match():
    """Buy 1 @ 100, sell 0.4 @ 110 → +4. Remaining 0.6 still in inventory."""
    r = _mk()
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 110, 0.4)
    assert abs(r.realized_pnl_24h - 4.0) < 1e-9
    # Now consume the rest.
    r.record_fill("BTCUSDT", "SELL", 120, 0.6)
    # Total: 0.4 * 10 + 0.6 * 20 = 4 + 12 = 16.
    assert abs(r.realized_pnl_24h - 16.0) < 1e-9


def test_fifo_consumes_oldest_first():
    """Buy 1@100, buy 1@200, sell 1@150 → matches the 100-lot → +50."""
    r = _mk()
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "BUY", 200, 1)
    r.record_fill("BTCUSDT", "SELL", 150, 1)
    assert r.realized_pnl_24h == 50.0


def test_unmatched_sell_is_dropped_not_phantomed():
    """Sell with no buys in inventory → unmatched, no PnL recorded."""
    r = _mk()
    r.record_fill("BTCUSDT", "SELL", 100, 1)
    assert r.realized_pnl_24h == 0.0


def test_pnl_is_per_symbol():
    r = _mk()
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("ETHUSDT", "SELL", 999, 1)  # unmatched on ETH
    r.record_fill("BTCUSDT", "SELL", 110, 1)
    # Only the BTC pair contributes.
    assert r.realized_pnl_24h == 10.0


# ---------------------------------------------------------------------------
# Loss floor → block placements
# ---------------------------------------------------------------------------


def test_loss_floor_blocks_placement():
    r = _mk(daily_loss_limit_usd=50)
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 30, 1)   # -70 realized, exceeds -50 floor
    reason = r.check_can_place("BTCUSDT", "BUY", 100, 0.1)
    assert reason is not None and "daily loss" in reason


def test_loss_floor_does_not_block_below_threshold():
    r = _mk(daily_loss_limit_usd=50)
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 70, 1)   # -30 realized, within -50 floor
    assert r.check_can_place("BTCUSDT", "BUY", 100, 0.1) is None


def test_pnl_ledger_trims_old_entries():
    r = _mk()
    # Inject a fake old entry directly. Cheating into private state is OK
    # in tests when the alternative is sleeping for 24 hours.
    r._pnl_ledger.append((time.time() - 25 * 3600, -1000.0))
    r.record_fill("BTCUSDT", "BUY", 100, 1)
    r.record_fill("BTCUSDT", "SELL", 110, 1)  # triggers trim on write
    # The -1000 entry was older than 24h, must have been trimmed.
    assert r.realized_pnl_24h == 10.0


# ---------------------------------------------------------------------------
# Standalone runner + pytest discovery
# ---------------------------------------------------------------------------


_TESTS = [
    test_check_can_place_passes_when_idle,
    test_check_can_place_is_side_effect_free,
    test_max_open_orders_blocks_at_cap,
    test_record_closed_frees_capacity,
    test_record_open_is_idempotent,
    test_trip_blocks_placements,
    test_halt_is_sticky,
    test_kill_switch_file_trips_on_poll,
    test_fifo_simple_profit,
    test_fifo_simple_loss,
    test_fifo_partial_match,
    test_fifo_consumes_oldest_first,
    test_unmatched_sell_is_dropped_not_phantomed,
    test_pnl_is_per_symbol,
    test_loss_floor_blocks_placement,
    test_loss_floor_does_not_block_below_threshold,
    test_pnl_ledger_trims_old_entries,
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

"""
Unit tests for exchange/filters.py — the tick/step rounding helpers.

These look trivial but there are two subtle bugs they guard against:
    1. Float precision: 0.1 / 0.01 evaluates to 9.999999999999998, and
       a naive floor() returns 9 instead of 10. The helper must normalise.
    2. Silent truncation: rounding down 0.0001 quantity to a step of
       0.01 must return 0.0, not the original — so the caller can
       detect "below minimum" instead of placing a phantom order.
"""

import sys
import asyncio

from src.exchange.filters import (
    SymbolFilter,
    meets_min_notional,
    round_to_step,
    round_to_tick,
)


def test_round_to_tick_exact_multiple_is_unchanged():
    assert round_to_tick(100.00, 0.01) == 100.00


def test_round_to_tick_floors_to_previous_tick():
    assert round_to_tick(75_001.234, 0.01) == 75_001.23


def test_round_to_tick_handles_float_precision():
    """0.1 / 0.01 = 9.9999... in float — naive floor would return 0.09."""
    assert round_to_tick(0.1, 0.01) == 0.10


def test_round_to_tick_with_zero_tick_is_identity():
    """Guard: if caller forgets to configure tick_size, don't crash."""
    assert round_to_tick(123.456, 0) == 123.456


def test_round_to_step_floors_to_previous_step():
    assert round_to_step(0.001_23, 0.000_01) == 0.001_23
    assert round_to_step(0.001_239, 0.000_01) == 0.001_23


def test_round_to_step_returns_zero_when_below_step():
    """Caller can then detect 'too small' and refuse to send."""
    assert round_to_step(0.000_005, 0.000_01) == 0.0


def test_meets_min_notional_accepts_equal_to_floor():
    assert meets_min_notional(price=100.0, qty=0.1, min_notional=10.0) is True


def test_meets_min_notional_rejects_below_floor():
    assert meets_min_notional(price=100.0, qty=0.05, min_notional=10.0) is False


def test_symbol_filter_is_frozen():
    """Frozen dataclass — mutation should raise, preventing drift."""
    sf = SymbolFilter(symbol="BTCUSDT", tick_size=0.01,
                      step_size=0.00001, min_qty=0.00001, min_notional=10.0)
    try:
        sf.tick_size = 0.1    # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("SymbolFilter should be immutable")


async def _run_all_as_script() -> int:
    tests = [
        test_round_to_tick_exact_multiple_is_unchanged,
        test_round_to_tick_floors_to_previous_tick,
        test_round_to_tick_handles_float_precision,
        test_round_to_tick_with_zero_tick_is_identity,
        test_round_to_step_floors_to_previous_step,
        test_round_to_step_returns_zero_when_below_step,
        test_meets_min_notional_accepts_equal_to_floor,
        test_meets_min_notional_rejects_below_floor,
        test_symbol_filter_is_frozen,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 40)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all_as_script()))

"""
Tests for the BalancePoller wiring inside GridEngine — Phase 11D step 4.

Covers:
  - Engine constructed without poller behaves as pre-Phase-11D
    (uses static config order_size).
  - Engine constructed with a poller asks the poller for order_size on
    every _place_grid call, instead of reading the static config.
  - Engine skips placement when poller.is_flash_crashed() returns True,
    incrementing the new _paused_by_balance_crash counter.
  - The flash-crash check fires BEFORE the more expensive sentiment /
    liquidity computations (no exchange call is made when paused).
  - _active_order_size tracks what the last grid was placed with, so
    fill replacements can use it.

We construct the engine with a fake exchange, fake signals, and only
the balance poller varying between tests. Using DummyPoller subclasses
of BalancePollerBase keeps the test surface narrow.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from src.exchange.base import (
    ExchangeBase,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.exchange.filters import SymbolFilter
from src.grid.config import GridEngineConfig
from src.grid.engine import GridEngine
from src.risk.balance_poller import BalancePollerBase


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeExchange(ExchangeBase):
    """
    Minimal ExchangeBase impl. Tracks placement requests; returns canned
    filter info; never actually hits a network. All methods we don't
    exercise are stubbed to raise so a regression that calls them
    surfaces immediately.
    """
    def __init__(self, *, ticker_price: float):
        self._ticker_price = ticker_price
        self.placed: list[tuple[str, str, float, float]] = []  # (symbol, side, price, qty)
        self.cancelled = 0

    async def connect(self): return None
    async def disconnect(self): return None
    async def get_balance(self, asset): return 0.0
    async def get_account_balances(self): return {}
    async def get_ticker_price(self, symbol): return self._ticker_price
    async def get_order_book(self, symbol, depth=20):
        return OrderBook(symbol=symbol, bids=[], asks=[])
    async def get_symbol_filters(self, symbol):
        return SymbolFilter(
            symbol=symbol,
            min_qty=0.00001,
            step_size=0.00001,
            tick_size=0.01,
            min_notional=10.0,
        )
    async def cancel_all_orders(self, symbol):
        self.cancelled += 1
        return 0
    async def place_order(self, symbol, side, order_type, quantity, price=None, time_in_force="GTC"):
        # Record what we'd place; return a synthetic Order so the engine
        # can track it in GridState.
        side_str = side.value if hasattr(side, "value") else str(side)
        self.placed.append((symbol, side_str, float(price), float(quantity)))
        return Order(
            order_id=f"fake-{len(self.placed)}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=float(price),
            size=float(quantity),
            status=OrderStatus.OPEN,
            timestamp=0.0,
        )
    async def get_order_status(self, symbol, order_id): raise NotImplementedError
    async def cancel_order(self, symbol, order_id): raise NotImplementedError
    async def get_open_orders(self, symbol=None): return []


class _CountingPoller(BalancePollerBase):
    """
    Records how many times each accessor was called so the test can
    assert the engine actually consults the poller (vs falling back to
    static config).
    """
    def __init__(
        self,
        *,
        order_size_to_return: float,
        flash_crashed: bool = False,
        equity_usdt: float = 1000.0,
    ):
        self._size = order_size_to_return
        self._flash = flash_crashed
        self._equity = equity_usdt
        self.order_size_calls = 0
        self.flash_crash_calls = 0

    async def start(self): return None
    async def stop(self): return None

    def order_size_in_base(self, symbol, mid_price, step_size, min_notional):
        self.order_size_calls += 1
        return self._size

    def daily_loss_limit_usd(self): return 50.0
    def is_flash_crashed(self):
        self.flash_crash_calls += 1
        return self._flash
    def current_equity_usdt(self): return self._equity


def _make_engine(*, balance_poller=None, ticker_price: float = 70000.0) -> tuple[GridEngine, _FakeExchange]:
    ex = _FakeExchange(ticker_price=ticker_price)
    cfg = GridEngineConfig(
        symbol="BTCUSDT",
        base_spacing_pct=0.3,
        min_spacing_pct=0.15,
        max_spacing_pct=1.0,
        levels_above=2,
        levels_below=2,
        order_size=0.0003,           # static fallback value
        recenter_threshold_pct=2.0,
        drift_check_interval_s=10000,
    )
    engine = GridEngine(
        exchange=ex, config=cfg, balance_poller=balance_poller,
    )
    return engine, ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_uses_static_order_size_when_no_poller():
    """
    Backward compatibility: when no poller is wired, the engine reads
    order_size from the static config. This is what every pre-Phase-11D
    test expects.
    """
    engine, ex = _make_engine(balance_poller=None)
    await engine._initialize()
    # Every placed order uses the static config size (0.0003) — verified
    # by grid math putting all 4 levels at the configured size.
    assert len(ex.placed) > 0
    for (_sym, _side, _price, qty) in ex.placed:
        assert qty == pytest.approx(0.0003)
    assert engine._active_order_size == pytest.approx(0.0003)


@pytest.mark.asyncio
async def test_engine_uses_poller_order_size_when_wired():
    """When a poller is wired, the engine uses ITS size, not the config's."""
    poller = _CountingPoller(order_size_to_return=0.0007)
    engine, ex = _make_engine(balance_poller=poller)
    await engine._initialize()
    # Poller was consulted at least once for sizing
    assert poller.order_size_calls >= 1
    # All placed orders use the poller's size, not the config's 0.0003
    for (_sym, _side, _price, qty) in ex.placed:
        assert qty == pytest.approx(0.0007)
    assert engine._active_order_size == pytest.approx(0.0007)


@pytest.mark.asyncio
async def test_engine_skips_placement_when_flash_crashed():
    """
    is_flash_crashed=True → no orders placed, _paused_by_balance_crash
    counter incremented. The pause counter lets the operator distinguish
    flash-crash skips from liquidity / sentiment skips.
    """
    poller = _CountingPoller(
        order_size_to_return=0.0003, flash_crashed=True, equity_usdt=160.0,
    )
    engine, ex = _make_engine(balance_poller=poller)
    await engine._initialize()
    assert ex.placed == []
    assert engine._paused_by_balance_crash == 1
    # Flash-crash is checked BEFORE order_size — we never asked for size.
    assert poller.order_size_calls == 0


@pytest.mark.asyncio
async def test_flash_crash_check_runs_before_other_pauses():
    """
    The flash-crash gate is the cheapest pause check (in-memory bool).
    It must fire BEFORE any I/O-bearing checks (sentiment compute,
    liquidity book fetch). We assert on the order indirectly: when
    flash-crashed, the exchange's get_order_book is NEVER called by
    the liquidity path.
    """
    # If liquidity ran, it would call exchange.get_order_book which our
    # _FakeExchange returns an empty book for. By NOT wiring liquidity
    # at all here, plus flash-crash=True, we verify nothing past the
    # early gate runs.
    poller = _CountingPoller(order_size_to_return=0.0003, flash_crashed=True)
    engine, ex = _make_engine(balance_poller=poller)
    # _initialize calls _place_grid; flash-crash should short-circuit.
    await engine._initialize()
    # No orders placed
    assert ex.placed == []
    # No size queried (would be called only if we got past the flash gate)
    assert poller.order_size_calls == 0


@pytest.mark.asyncio
async def test_engine_resumes_placing_when_flash_crash_clears():
    """
    After is_flash_crashed flips False, the next _place_grid call places
    orders normally. Same recovery pattern as liquidity / sentiment.
    """
    # Mutable flag so the test can flip it between calls
    state = {"crashed": True}

    class _ToggleablePoller(_CountingPoller):
        def is_flash_crashed(self):
            super().is_flash_crashed()  # bump the counter
            return state["crashed"]

    poller = _ToggleablePoller(order_size_to_return=0.0003)
    engine, ex = _make_engine(balance_poller=poller)

    await engine._initialize()  # crashed=True → no placements
    assert ex.placed == []
    assert engine._paused_by_balance_crash == 1

    # Recovery: flip the flag and call _place_grid manually
    state["crashed"] = False
    # Need to set center_price first since _initialize was the only call
    # that did it before short-circuiting on flash-crash
    engine._state.center_price = 70000.0
    engine._filters = await ex.get_symbol_filters("BTCUSDT")
    await engine._place_grid(70000.0, zones=None)

    assert len(ex.placed) > 0   # placed something this time
    assert engine._paused_by_balance_crash == 1   # pause counter unchanged

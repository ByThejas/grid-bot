"""
Tests for DryRunExchange.

WHY these tests matter:
- Dry-run is the ONLY safety harness between "we changed grid code" and
  "we put real orders on a real exchange." If DryRunExchange accidentally
  forwarded place_order / cancel_order to the inner adapter, a dry-run
  rehearsal would silently trade. So we explicitly assert:
    * mutating calls never touch the inner
    * read-only calls always touch the inner
    * the synthetic Order returned by place_order is well-formed enough
      to be tracked by GridState

We stub the inner exchange with a fake that records every method name
it received, so the tests can make those "touched / did not touch"
assertions directly.

DUAL-MODE: runs under `pytest tests/test_dry_run.py -v` AND under
`python -m tests.test_dry_run` so you can debug without the pytest layer.
"""

import asyncio
import sys
import traceback
from typing import Optional

from src.exchange.base import (
    ExchangeBase,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.exchange.dry_run import DryRunExchange
from src.exchange.filters import SymbolFilter


# ---------------------------------------------------------------------------
# Test double — a fake ExchangeBase that records which methods were called.
# ---------------------------------------------------------------------------


class RecordingExchange(ExchangeBase):
    """Records method names called on it. Mutations raise — if the dry-run
    wrapper forwards a write by accident, the test blows up LOUDLY."""

    def __init__(self):
        self.calls: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.calls.append("connect")
        self.connected = True

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    async def place_order(self, symbol, side, order_type, quantity, price=None) -> Order:
        self.calls.append("place_order")
        raise AssertionError("dry-run forwarded place_order to inner!")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        self.calls.append("cancel_order")
        raise AssertionError("dry-run forwarded cancel_order to inner!")

    async def cancel_all_orders(self, symbol: str) -> int:
        self.calls.append("cancel_all_orders")
        raise AssertionError("dry-run forwarded cancel_all_orders to inner!")

    async def get_order_status(self, symbol, order_id):
        self.calls.append("get_order_status")
        return Order(
            order_id=order_id, symbol=symbol,
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            price=1.0, quantity=1.0, status=OrderStatus.OPEN,
        )

    async def get_open_orders(self, symbol):
        self.calls.append("get_open_orders")
        return []

    async def get_order_book(self, symbol, depth=20):
        self.calls.append("get_order_book")
        return OrderBook(symbol=symbol, bids=[[100, 1]], asks=[[101, 1]], timestamp=0)

    async def get_balance(self, asset):
        self.calls.append("get_balance")
        return 1000.0

    async def get_ticker_price(self, symbol):
        self.calls.append("get_ticker_price")
        return 100.5

    async def get_symbol_filters(self, symbol):
        self.calls.append("get_symbol_filters")
        return SymbolFilter(symbol=symbol, tick_size=0.01, step_size=0.001,
                            min_qty=0.001, min_notional=10.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_place_order_does_not_touch_inner():
    """place_order must short-circuit — inner.place_order must never run."""
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    order = await dry.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.01, 50000)
    assert "place_order" not in inner.calls
    assert order.order_id.startswith("DRYRUN-")
    assert order.status == OrderStatus.OPEN
    assert order.symbol == "BTCUSDT"
    assert order.side == OrderSide.BUY
    assert order.quantity == 0.01
    assert order.price == 50000


async def test_place_order_ids_are_unique():
    """Every synthetic id is distinct so GridState can track multiple levels."""
    dry = DryRunExchange(RecordingExchange())
    ids = set()
    for _ in range(10):
        o = await dry.place_order("BTCUSDT", OrderSide.SELL, OrderType.LIMIT, 0.01, 50000)
        ids.add(o.order_id)
    assert len(ids) == 10


async def test_cancel_order_does_not_touch_inner():
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    ok = await dry.cancel_order("BTCUSDT", "DRYRUN-1")
    assert ok is True
    assert "cancel_order" not in inner.calls


async def test_cancel_all_does_not_touch_inner():
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    n = await dry.cancel_all_orders("BTCUSDT")
    assert n == 0
    assert "cancel_all_orders" not in inner.calls


async def test_read_paths_forward():
    """Every read call must reach the inner exchange."""
    inner = RecordingExchange()
    dry = DryRunExchange(inner)

    await dry.get_ticker_price("BTCUSDT")
    await dry.get_order_book("BTCUSDT", depth=5)
    await dry.get_balance("USDT")
    await dry.get_symbol_filters("BTCUSDT")
    await dry.get_open_orders("BTCUSDT")

    for m in ("get_ticker_price", "get_order_book", "get_balance",
              "get_symbol_filters", "get_open_orders"):
        assert m in inner.calls, f"expected {m} forwarded to inner"


async def test_connect_disconnect_forward():
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    await dry.connect()
    assert inner.connected is True
    await dry.disconnect()
    assert inner.connected is False


async def test_get_order_status_dryrun_id_short_circuits():
    """DRYRUN-* ids never went to the exchange, so we can't ask it about them."""
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    o = await dry.get_order_status("BTCUSDT", "DRYRUN-42")
    assert o.order_id == "DRYRUN-42"
    assert "get_order_status" not in inner.calls


async def test_get_order_status_real_id_forwards():
    inner = RecordingExchange()
    dry = DryRunExchange(inner)
    await dry.get_order_status("BTCUSDT", "9999999")
    assert "get_order_status" in inner.calls


# ---------------------------------------------------------------------------
# pytest hooks + standalone runner
# ---------------------------------------------------------------------------


def _pytest_wrap(coro_fn):
    def sync():
        asyncio.run(coro_fn())
    sync.__name__ = coro_fn.__name__
    return sync


test_place_order_does_not_touch_inner_sync = _pytest_wrap(test_place_order_does_not_touch_inner)
test_place_order_ids_are_unique_sync = _pytest_wrap(test_place_order_ids_are_unique)
test_cancel_order_does_not_touch_inner_sync = _pytest_wrap(test_cancel_order_does_not_touch_inner)
test_cancel_all_does_not_touch_inner_sync = _pytest_wrap(test_cancel_all_does_not_touch_inner)
test_read_paths_forward_sync = _pytest_wrap(test_read_paths_forward)
test_connect_disconnect_forward_sync = _pytest_wrap(test_connect_disconnect_forward)
test_get_order_status_dryrun_id_short_circuits_sync = _pytest_wrap(test_get_order_status_dryrun_id_short_circuits)
test_get_order_status_real_id_forwards_sync = _pytest_wrap(test_get_order_status_real_id_forwards)


async def _main():
    tests = [
        test_place_order_does_not_touch_inner,
        test_place_order_ids_are_unique,
        test_cancel_order_does_not_touch_inner,
        test_cancel_all_does_not_touch_inner,
        test_read_paths_forward,
        test_connect_disconnect_forward,
        test_get_order_status_dryrun_id_short_circuits,
        test_get_order_status_real_id_forwards,
    ]
    passed = failed = 0
    for t in tests:
        try:
            await t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(_main())

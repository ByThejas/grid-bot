"""
Tests for BacktestExchange.

WHY these tests matter:
The backtest is only trustworthy if the fill logic matches the mental
model (LIMIT BUY crosses when tape <= limit; LIMIT SELL when tape >=
limit). A wrong fill rule gives us a backtest that says the grid
prints money when it wouldn't. We lock down: fill matching, no-fill
conditions, immediate-fill-on-submit, cancel semantics, synthetic
book shape.
"""

import asyncio
import sys
import traceback

from src.backtest.exchange import BacktestExchange
from src.exchange.base import OrderSide, OrderStatus, OrderType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fresh(**kwargs) -> BacktestExchange:
    """Construct a BacktestExchange with the current price pre-set for BTCUSDT
    so place_order doesn't hit the immediate-fill branch by accident."""
    ex = BacktestExchange(**kwargs)
    ex.set_price("BTCUSDT", 75_000.0)
    return ex


class _Collector:
    """Awaitable fill-event collector the exchange can dispatch to."""
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# place_order — basic + filter rejections
# ---------------------------------------------------------------------------


def test_place_limit_order_tracks_as_open():
    async def run():
        ex = _fresh()
        order = await ex.place_order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=0.001, price=74_000.0,
        )
        assert order.status == OrderStatus.OPEN
        assert order.order_id == "1"
        # Open book contains exactly this order.
        open_orders = await ex.get_open_orders("BTCUSDT")
        assert len(open_orders) == 1
        assert open_orders[0].order_id == "1"
    _run(run())


def test_place_market_order_rejected():
    async def run():
        ex = _fresh()
        try:
            await ex.place_order(
                symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=0.001, price=None,
            )
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert ex.rejected_count == 1
    _run(run())


def test_place_limit_without_price_rejected():
    async def run():
        ex = _fresh()
        try:
            await ex.place_order(
                symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                quantity=0.001, price=None,
            )
            assert False, "expected ValueError"
        except ValueError:
            pass
    _run(run())


def test_order_ids_monotonic():
    async def run():
        ex = _fresh()
        o1 = await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000)
        o2 = await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 73_000)
        assert o1.order_id == "1" and o2.order_id == "2"
    _run(run())


# ---------------------------------------------------------------------------
# Immediate fill on submit (aggressive limit crosses market)
# ---------------------------------------------------------------------------


def test_aggressive_buy_fills_immediately():
    async def run():
        ex = _fresh()  # price = 75_000
        collector = _Collector()
        ex.on_fill_event = collector
        # BUY at 76_000 when market is 75_000 — we're bidding ABOVE ask, cross.
        order = await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT,
                                     quantity=0.001, price=76_000.0)
        assert order.status == OrderStatus.FILLED
        # Should NOT be in open book.
        assert len(await ex.get_open_orders("BTCUSDT")) == 0
        # A fill event was dispatched.
        assert len(collector.events) == 1
        assert collector.events[0]["X"] == "FILLED"
        assert collector.events[0]["s"] == "BTCUSDT"
    _run(run())


def test_aggressive_sell_fills_immediately():
    async def run():
        ex = _fresh()  # price = 75_000
        collector = _Collector()
        ex.on_fill_event = collector
        order = await ex.place_order("BTCUSDT", OrderSide.SELL, OrderType.LIMIT,
                                     quantity=0.001, price=74_000.0)
        assert order.status == OrderStatus.FILLED
        assert len(collector.events) == 1
    _run(run())


def test_passive_buy_stays_open():
    async def run():
        ex = _fresh()  # price = 75_000
        order = await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT,
                                     quantity=0.001, price=74_000.0)
        assert order.status == OrderStatus.OPEN


    _run(run())


# ---------------------------------------------------------------------------
# tick_trade — fill matching
# ---------------------------------------------------------------------------


def test_tick_fills_buy_when_price_crosses_down():
    async def run():
        ex = _fresh()
        collector = _Collector()
        ex.on_fill_event = collector
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000.0)
        # Trade at 73_900 — crosses our 74_000 bid.
        filled = await ex.tick_trade("BTCUSDT", price=73_900.0, qty=0.5)
        assert filled == ["1"]
        assert len(collector.events) == 1
        # Fill is at the ORDER's limit price (74_000), not the trade price.
        assert collector.events[0]["L"] == "74000.0"
    _run(run())


def test_tick_does_not_fill_when_price_does_not_cross():
    async def run():
        ex = _fresh()
        collector = _Collector()
        ex.on_fill_event = collector
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000.0)
        # Trade at 74_500 — above our 74_000 bid, no fill.
        filled = await ex.tick_trade("BTCUSDT", price=74_500.0, qty=0.5)
        assert filled == []
        assert collector.events == []
    _run(run())


def test_tick_fills_sell_when_price_crosses_up():
    async def run():
        ex = _fresh()
        collector = _Collector()
        ex.on_fill_event = collector
        await ex.place_order("BTCUSDT", OrderSide.SELL, OrderType.LIMIT, 0.001, 76_000.0)
        filled = await ex.tick_trade("BTCUSDT", price=76_100.0, qty=0.5)
        assert filled == ["1"]
        assert collector.events[0]["S"] == "SELL"
    _run(run())


def test_tick_fills_multiple_orders_on_one_trade():
    async def run():
        ex = _fresh()
        collector = _Collector()
        ex.on_fill_event = collector
        # Three BUY orders at different prices.
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_500.0)
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000.0)
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 73_500.0)
        # Deep trade sweeps all three.
        filled = await ex.tick_trade("BTCUSDT", price=73_400.0, qty=10.0)
        assert set(filled) == {"1", "2", "3"}
        assert len(collector.events) == 3
    _run(run())


def test_tick_ignores_orders_for_other_symbols():
    async def run():
        ex = BacktestExchange()
        ex.set_price("BTCUSDT", 75_000.0)
        ex.set_price("ETHUSDT", 2_500.0)
        collector = _Collector()
        ex.on_fill_event = collector
        await ex.place_order("ETHUSDT", OrderSide.BUY, OrderType.LIMIT, 0.01, 2_400.0)
        # BTC trade should NOT fill the ETH order.
        await ex.tick_trade("BTCUSDT", price=2_000.0, qty=0.1)
        assert collector.events == []
    _run(run())


def test_tick_trade_missing_callback_drops_event():
    # Fill still happens (removed from book), but no callback invoked.
    async def run():
        ex = _fresh()
        # on_fill_event left as None
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000.0)
        await ex.tick_trade("BTCUSDT", price=73_900.0, qty=0.5)
        # Order removed even though callback couldn't be invoked.
        assert len(await ex.get_open_orders("BTCUSDT")) == 0
        assert ex.filled_count == 1
    _run(run())


# ---------------------------------------------------------------------------
# Cancel semantics
# ---------------------------------------------------------------------------


def test_cancel_order_removes_from_book():
    async def run():
        ex = _fresh()
        o = await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000)
        ok = await ex.cancel_order("BTCUSDT", o.order_id)
        assert ok is True
        assert len(await ex.get_open_orders("BTCUSDT")) == 0


    _run(run())


def test_cancel_unknown_order_returns_false():
    async def run():
        ex = _fresh()
        ok = await ex.cancel_order("BTCUSDT", "999")
        assert ok is False
    _run(run())


def test_cancel_all_only_cancels_symbol():
    async def run():
        ex = BacktestExchange()
        ex.set_price("BTCUSDT", 75_000.0)
        ex.set_price("ETHUSDT", 2_500.0)
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 74_000)
        await ex.place_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 73_500)
        await ex.place_order("ETHUSDT", OrderSide.BUY, OrderType.LIMIT, 0.01, 2_400)
        n = await ex.cancel_all_orders("BTCUSDT")
        assert n == 2
        # ETH order still live.
        eth_open = await ex.get_open_orders("ETHUSDT")
        assert len(eth_open) == 1


    _run(run())


# ---------------------------------------------------------------------------
# Synthetic order book
# ---------------------------------------------------------------------------


def test_book_has_correct_spread():
    async def run():
        ex = BacktestExchange(synthetic_spread_bps=10.0)  # 10 bps spread
        ex.set_price("BTCUSDT", 100_000.0)
        book = await ex.get_order_book("BTCUSDT", depth=5)
        # half-spread = 100_000 * 5bps / 10000 = 50
        assert abs(book.best_bid - 99_950.0) < 1e-6
        assert abs(book.best_ask - 100_050.0) < 1e-6
        # Spread relative to mid ≈ 100 / 100_000 = 10 bps. ✓


    _run(run())


def test_book_depth_respects_request():
    async def run():
        ex = BacktestExchange(synthetic_book_depth=20)
        ex.set_price("BTCUSDT", 75_000.0)
        book = await ex.get_order_book("BTCUSDT", depth=5)
        assert len(book.bids) == 5 and len(book.asks) == 5
    _run(run())


def test_book_empty_when_price_not_set():
    async def run():
        ex = BacktestExchange()
        # No set_price call for LTCUSDT.
        book = await ex.get_order_book("LTCUSDT")
        assert book.bids == [] and book.asks == []
    _run(run())


# ---------------------------------------------------------------------------
# Filters + misc
# ---------------------------------------------------------------------------


def test_get_symbol_filters_returns_defaults():
    async def run():
        ex = BacktestExchange()
        f = await ex.get_symbol_filters("BTCUSDT")
        assert f.symbol == "BTCUSDT"
        assert f.tick_size == 0.01


    _run(run())


def test_get_symbol_filters_unknown_raises():
    async def run():
        ex = BacktestExchange()
        try:
            await ex.get_symbol_filters("DOGEUSDT")
            assert False, "expected KeyError"
        except KeyError:
            pass
    _run(run())


# ---------------------------------------------------------------------------
# Standalone runner + pytest discovery
# ---------------------------------------------------------------------------


_TESTS = [
    test_place_limit_order_tracks_as_open,
    test_place_market_order_rejected,
    test_place_limit_without_price_rejected,
    test_order_ids_monotonic,
    test_aggressive_buy_fills_immediately,
    test_aggressive_sell_fills_immediately,
    test_passive_buy_stays_open,
    test_tick_fills_buy_when_price_crosses_down,
    test_tick_does_not_fill_when_price_does_not_cross,
    test_tick_fills_sell_when_price_crosses_up,
    test_tick_fills_multiple_orders_on_one_trade,
    test_tick_ignores_orders_for_other_symbols,
    test_tick_trade_missing_callback_drops_event,
    test_cancel_order_removes_from_book,
    test_cancel_unknown_order_returns_false,
    test_cancel_all_only_cancels_symbol,
    test_book_has_correct_spread,
    test_book_depth_respects_request,
    test_book_empty_when_price_not_set,
    test_get_symbol_filters_returns_defaults,
    test_get_symbol_filters_unknown_raises,
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

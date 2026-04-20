"""
Tests for backtest price sources.

Focus:
 - Random walk respects seed (reproducibility → essential for backtests)
 - Random walk tick counts and timestamps are monotonic
 - Database source merges multi-symbol trades in chronological order
"""

import asyncio
import os
import sys
import tempfile
import traceback

from src.backtest.sources import (
    DatabaseTradeSource,
    PriceTick,
    SyntheticRandomWalkSource,
)
from src.market_data.storage import MarketDataStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _collect(source) -> list[PriceTick]:
    out = []
    async for t in source.ticks():
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# SyntheticRandomWalkSource
# ---------------------------------------------------------------------------


def test_random_walk_yields_requested_count():
    async def run():
        src = SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=100, seed=1)
        ticks = await _collect(src)
        assert len(ticks) == 100
    _run(run())


def test_random_walk_timestamps_monotonic():
    async def run():
        src = SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=50,
                                        dt_seconds=1.5, seed=1)
        ticks = await _collect(src)
        for a, b in zip(ticks, ticks[1:]):
            assert b.timestamp > a.timestamp
            assert abs((b.timestamp - a.timestamp) - 1.5) < 1e-9
    _run(run())


def test_random_walk_seed_reproducible():
    async def run():
        a = await _collect(SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=20, seed=42))
        b = await _collect(SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=20, seed=42))
        assert [t.price for t in a] == [t.price for t in b]
    _run(run())


def test_random_walk_different_seed_produces_different_path():
    async def run():
        a = await _collect(SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=20, seed=1))
        b = await _collect(SyntheticRandomWalkSource("BTCUSDT", 75_000, n_ticks=20, seed=2))
        assert [t.price for t in a] != [t.price for t in b]
    _run(run())


def test_random_walk_prices_positive():
    async def run():
        # Heavy sigma to stress the floor guard.
        src = SyntheticRandomWalkSource("BTCUSDT", 100.0, n_ticks=500,
                                        sigma=0.5, seed=1)
        ticks = await _collect(src)
        for t in ticks:
            assert t.price > 0
    _run(run())


# ---------------------------------------------------------------------------
# DatabaseTradeSource
# ---------------------------------------------------------------------------


def _make_store_with_trades(path: str, trades: list[dict]) -> None:
    async def seed():
        store = MarketDataStore(path)
        await store.connect()
        for t in trades:
            await store.save_trade(
                symbol=t["symbol"],
                trade_id=t["trade_id"],
                price=t["price"],
                quantity=t["quantity"],
                is_buyer_maker=t.get("is_buyer_maker", False),
                timestamp=t["timestamp"],
            )
        await store.close()
    _run(seed())


def test_db_source_merges_symbols_chronologically():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "md.db")
        # Interleaved timestamps across two symbols.
        trades = [
            {"symbol": "BTCUSDT", "trade_id": 1, "price": 75_000, "quantity": 0.1, "timestamp": 1000},
            {"symbol": "ETHUSDT", "trade_id": 10, "price": 2500, "quantity": 0.5, "timestamp": 1500},
            {"symbol": "BTCUSDT", "trade_id": 2, "price": 75_100, "quantity": 0.2, "timestamp": 2000},
            {"symbol": "ETHUSDT", "trade_id": 11, "price": 2510, "quantity": 0.3, "timestamp": 2500},
        ]
        _make_store_with_trades(path, trades)

        async def run():
            store = MarketDataStore(path)
            await store.connect()
            try:
                src = DatabaseTradeSource(store, ["BTCUSDT", "ETHUSDT"])
                ticks = await _collect(src)
            finally:
                await store.close()
            # Order must be strictly by timestamp across symbols.
            assert [t.timestamp for t in ticks] == [1.0, 1.5, 2.0, 2.5]
            assert [t.symbol for t in ticks] == ["BTCUSDT", "ETHUSDT", "BTCUSDT", "ETHUSDT"]
        _run(run())


def test_db_source_since_filter():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "md.db")
        trades = [
            {"symbol": "BTCUSDT", "trade_id": 1, "price": 75_000, "quantity": 0.1, "timestamp": 100},
            {"symbol": "BTCUSDT", "trade_id": 2, "price": 75_100, "quantity": 0.2, "timestamp": 500},
            {"symbol": "BTCUSDT", "trade_id": 3, "price": 75_200, "quantity": 0.3, "timestamp": 1000},
        ]
        _make_store_with_trades(path, trades)

        async def run():
            store = MarketDataStore(path)
            await store.connect()
            try:
                src = DatabaseTradeSource(store, ["BTCUSDT"], since_ts_ms=400)
                ticks = await _collect(src)
            finally:
                await store.close()
            assert len(ticks) == 2
            assert ticks[0].timestamp == 0.5
        _run(run())


def test_db_source_limit_truncates():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "md.db")
        trades = [
            {"symbol": "BTCUSDT", "trade_id": i, "price": 75_000 + i,
             "quantity": 0.001, "timestamp": 1000 + i}
            for i in range(10)
        ]
        _make_store_with_trades(path, trades)

        async def run():
            store = MarketDataStore(path)
            await store.connect()
            try:
                src = DatabaseTradeSource(store, ["BTCUSDT"], limit=5)
                ticks = await _collect(src)
            finally:
                await store.close()
            assert len(ticks) == 5
        _run(run())


def test_db_source_handles_empty_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "md.db")
        _make_store_with_trades(path, [
            {"symbol": "BTCUSDT", "trade_id": 1, "price": 75_000, "quantity": 0.1, "timestamp": 100},
        ])

        async def run():
            store = MarketDataStore(path)
            await store.connect()
            try:
                # ETHUSDT has no trades — merge still works.
                src = DatabaseTradeSource(store, ["BTCUSDT", "ETHUSDT"])
                ticks = await _collect(src)
            finally:
                await store.close()
            assert len(ticks) == 1
            assert ticks[0].symbol == "BTCUSDT"
        _run(run())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


_TESTS = [
    test_random_walk_yields_requested_count,
    test_random_walk_timestamps_monotonic,
    test_random_walk_seed_reproducible,
    test_random_walk_different_seed_produces_different_path,
    test_random_walk_prices_positive,
    test_db_source_merges_symbols_chronologically,
    test_db_source_since_filter,
    test_db_source_limit_truncates,
    test_db_source_handles_empty_symbol,
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

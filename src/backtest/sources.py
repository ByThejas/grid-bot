"""
Price sources for the backtest — the thing the runner reads ticks from.

WHY this layer exists:
The runner doesn't care whether ticks come from a random-walk generator,
a trending-price synthesiser, or a real historical tape pulled out of
SQLite. It just wants an async iterator of `PriceTick`. Keeping the
source interface narrow means a future NLP-sentiment adapter or a CSV
import becomes a tiny new implementation, not a runner rewrite.

WHAT v1 INCLUDES
  * SyntheticRandomWalkSource — geometric Brownian motion. Good for
    smoke tests: deterministic (with seed), no data needed, moves
    enough to exercise fills / recenters in a reasonable tick count.
  * DatabaseTradeSource — replays rows from `data/marketdata.db`
    via MarketDataStore.get_trades. Multi-symbol supported by loading
    each symbol's trades and merging by timestamp.

WHAT v1 EXCLUDES (v2 candidates)
  * Trending / sinusoidal synthetic paths (easy to add).
  * Snapshot replay (engine uses book via LiquiditySignal; v1 synthesises
    a book from the current price in BacktestExchange, which is fine for
    a first pass but not tick-perfect for liquidity-signal tuning).
"""

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from src.market_data.storage import MarketDataStore
from src.utils.logger import get_logger

logger = get_logger("backtest_sources")


@dataclass(frozen=True)
class PriceTick:
    """
    One trade print on the simulated tape. `timestamp` is seconds
    (float — the exchange stores it in ms internally but at the source
    boundary floats are easier to reason about). `qty` is advisory in
    v1 — the backtest exchange's fill rule doesn't gate on qty yet.
    """
    timestamp: float
    symbol: str
    price: float
    qty: float = 0.0


class PriceSource(ABC):
    """Async iterator of ticks in chronological order."""

    @abstractmethod
    def ticks(self) -> AsyncIterator[PriceTick]:
        ...


# ---------------------------------------------------------------------
# Synthetic — random walk
# ---------------------------------------------------------------------


class SyntheticRandomWalkSource(PriceSource):
    """
    Geometric Brownian motion. Each step: P_{t+1} = P_t * (1 + drift + noise)
    where `noise ~ Normal(0, sigma)`. No serial correlation — that's a
    feature, not a bug, because the grid's edge comes from mean reversion
    over small moves and this path has no preferred direction.

    USAGE:
        src = SyntheticRandomWalkSource(
            symbol="BTCUSDT", start_price=75_000.0,
            n_ticks=5_000, dt_seconds=1.0,
            drift=0.0, sigma=0.001, seed=42,
        )
        async for tick in src.ticks(): ...

    PICKING PARAMETERS
    For BTC at $75k, realized intraday volatility is roughly 1-2% per
    hour. One tick per second with sigma=0.0005 gives about that. If
    you want the grid to get recentered during a run, crank sigma up
    or insert a drift term — without drift the walk is a martingale
    and will rarely exceed the recenter threshold in a short run.
    """

    def __init__(
        self,
        symbol: str,
        start_price: float,
        n_ticks: int,
        *,
        dt_seconds: float = 1.0,
        drift: float = 0.0,
        sigma: float = 0.0005,
        start_timestamp: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.symbol = symbol
        self.start_price = start_price
        self.n_ticks = n_ticks
        self.dt_seconds = dt_seconds
        self.drift = drift
        self.sigma = sigma
        self.start_timestamp = start_timestamp
        # Local RNG so multiple sources don't share seed state globally.
        self._rng = random.Random(seed)

    async def ticks(self) -> AsyncIterator[PriceTick]:
        price = self.start_price
        ts = self.start_timestamp
        for _ in range(self.n_ticks):
            yield PriceTick(timestamp=ts, symbol=self.symbol, price=price, qty=0.0)
            noise = self._rng.gauss(0.0, self.sigma)
            price = max(price * (1.0 + self.drift + noise), 0.01)
            ts += self.dt_seconds


# ---------------------------------------------------------------------
# Database — historical trade replay
# ---------------------------------------------------------------------


class DatabaseTradeSource(PriceSource):
    """
    Replay historical trades from SQLite via MarketDataStore.

    For multi-symbol backtests we load each symbol's trades and merge by
    timestamp in memory. The DBs we work with are small (a day's worth
    of BTC + ETH trades is ~2k rows), so in-memory merge is fine. If
    that ever stops being true we can switch to a heap-based streaming
    merge — but not before it matters.

    USAGE:
        store = MarketDataStore("data/marketdata.db")
        await store.connect()
        src = DatabaseTradeSource(
            store=store, symbols=["BTCUSDT", "ETHUSDT"], since_ts_ms=0,
        )
        async for tick in src.ticks(): ...
        await store.close()
    """

    def __init__(
        self,
        store: MarketDataStore,
        symbols: list[str],
        *,
        since_ts_ms: int = 0,
        limit: Optional[int] = None,
    ):
        self._store = store
        self._symbols = list(symbols)
        self._since_ts_ms = since_ts_ms
        self._limit = limit

    async def ticks(self) -> AsyncIterator[PriceTick]:
        # Load per-symbol then merge — get_trades returns sorted ascending
        # so we can use heapq.merge on (timestamp, symbol_index) keys.
        per_symbol: list[list[dict]] = []
        for sym in self._symbols:
            rows = await self._store.get_trades(sym, self._since_ts_ms)
            logger.info("db trades loaded", symbol=sym, count=len(rows))
            per_symbol.append(rows)

        total_yielded = 0
        # Simple in-memory merge — linear pointer over each list.
        pointers = [0] * len(per_symbol)
        while True:
            # Find the list with the smallest next-timestamp.
            next_i = -1
            next_ts = None
            for i, rows in enumerate(per_symbol):
                p = pointers[i]
                if p >= len(rows):
                    continue
                ts = rows[p]["timestamp"]
                if next_ts is None or ts < next_ts:
                    next_ts = ts
                    next_i = i
            if next_i == -1:
                return   # all lists exhausted

            row = per_symbol[next_i][pointers[next_i]]
            pointers[next_i] += 1
            yield PriceTick(
                # Store's timestamps are ms since epoch. We expose seconds
                # to consumers — the engine's drift monitor doesn't care
                # about absolute epoch, only deltas.
                timestamp=row["timestamp"] / 1000.0,
                symbol=self._symbols[next_i],
                price=float(row["price"]),
                qty=float(row["quantity"]),
            )
            total_yielded += 1
            if self._limit and total_yielded >= self._limit:
                return

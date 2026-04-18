"""
Order Book Stream — maintains a live order book via Binance WebSocket.

WHY a streaming order book instead of polling REST?
- REST is rate-limited (~1200 req/min) and adds 100-300ms latency per call.
- Order books change 5-20 times per second on BTC/USDT. Polling at 1Hz misses
  the absorption events and liquidity walls our S&D engine needs.
- WebSocket pushes diffs as they happen — sub-50ms from exchange to us.

WHY DepthCacheManager (from python-binance) instead of raw WebSocket?
The Binance order book sync protocol is non-trivial:
    1. Open WS to <symbol>@depth and BUFFER all events.
    2. Hit REST /depth?limit=1000 to get a snapshot with lastUpdateId.
    3. Drop buffered events with u <= lastUpdateId (already in snapshot).
    4. Validate: first kept event must satisfy U <= lastUpdateId+1 <= u.
    5. Apply remaining events; for each, validate pu == prev event's u.
    6. On any sequence gap → throw away the book and restart from step 1.

DepthCacheManager implements all of that. Reimplementing it = guaranteed
sequence bug at 3am. We use it and focus on what's project-specific:
    - Throttled snapshot persistence (1 row/second instead of every diff)
    - Graceful shutdown
    - Reconnect with backoff via the configured policy

WHAT WE PERSIST:
The DepthCache tracks ~1000 levels each side. We snapshot only the top 20
to storage — that's the depth our S&D and Liquidity signals actually use,
and it keeps DB growth manageable.
"""

import asyncio
import time
from typing import Optional

from binance import AsyncClient, DepthCacheManager

from src.market_data.storage import MarketDataStore
from src.utils.logger import get_logger

logger = get_logger("orderbook_stream")


class OrderBookStream:
    """
    One instance per symbol. Owns the WebSocket subscription and the
    persistence loop for that symbol's order book.

    USAGE (from the coordinator):
        client = await AsyncClient.create(api_key, api_secret, testnet=True)
        stream = OrderBookStream(client, "BTCUSDT", store, snapshot_every_s=1.0)
        await stream.run(stop_event)

    The stream runs until stop_event is set. On disconnect or error,
    it sleeps `reconnect_delay_s` and rebuilds the depth cache from scratch.
    """

    def __init__(
        self,
        client: AsyncClient,
        symbol: str,
        store: MarketDataStore,
        snapshot_every_s: float = 1.0,
        depth_levels: int = 20,
        reconnect_delay_s: float = 1.0,
        max_reconnect_attempts: int = 10,
    ):
        self._client = client
        self._symbol = symbol
        self._store = store
        self._snapshot_every_s = snapshot_every_s
        self._depth_levels = depth_levels
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_attempts = max_reconnect_attempts

        # Last time we wrote a snapshot. Used for throttling.
        self._last_snapshot_ts = 0.0
        self._snapshots_written = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        """
        Main loop. Holds the WebSocket open, persists snapshots,
        reconnects on failure, exits cleanly when stop_event is set.
        """
        attempts = 0
        while not stop_event.is_set():
            try:
                await self._consume_until_disconnect(stop_event)
                # Clean exit (stop_event set during consume) → break out.
                break
            except asyncio.CancelledError:
                # Cancellation propagates from the parent task — honor it.
                logger.info("orderbook stream cancelled", symbol=self._symbol)
                raise
            except Exception as e:
                attempts += 1
                if attempts > self._max_reconnect_attempts:
                    logger.error(
                        "orderbook stream gave up after max reconnects",
                        symbol=self._symbol,
                        attempts=attempts,
                        error=str(e),
                    )
                    raise
                logger.warning(
                    "orderbook stream disconnected, will retry",
                    symbol=self._symbol,
                    attempt=attempts,
                    delay_s=self._reconnect_delay_s,
                    error=str(e),
                )
                await asyncio.sleep(self._reconnect_delay_s)

        logger.info(
            "orderbook stream stopped",
            symbol=self._symbol,
            snapshots_written=self._snapshots_written,
        )

    async def _consume_until_disconnect(self, stop_event: asyncio.Event) -> None:
        """
        Open the depth cache and consume updates until the connection
        breaks or stop_event is set. Persists throttled snapshots.

        WHY recv() with a timeout?
        - Without a timeout, recv() blocks forever. If the user signals stop,
          we would hang until the next message arrives.
        - 1.0s timeout means at worst we delay shutdown by 1 second.
        """
        dcm = DepthCacheManager(self._client, symbol=self._symbol)
        async with dcm as dcm_socket:
            logger.info("orderbook stream connected", symbol=self._symbol)
            while not stop_event.is_set():
                try:
                    depth_cache = await asyncio.wait_for(
                        dcm_socket.recv(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # No message in last second — loop back to check stop_event.
                    continue

                await self._maybe_persist(depth_cache)

    async def _maybe_persist(self, depth_cache) -> None:
        """
        Throttle: only write to storage if snapshot_every_s has elapsed
        since the last write. This caps write rate regardless of how
        fast the WS feed pushes updates.
        """
        now = time.time()
        if now - self._last_snapshot_ts < self._snapshot_every_s:
            return

        # DepthCache.get_bids() returns sorted high->low; get_asks() low->high.
        # That matches our OrderBook convention exactly.
        bids = [[float(p), float(q)] for p, q in depth_cache.get_bids()[: self._depth_levels]]
        asks = [[float(p), float(q)] for p, q in depth_cache.get_asks()[: self._depth_levels]]

        # update_time is the exchange's timestamp on the latest diff applied.
        # Falls back to wall clock if the lib hasn't populated it yet.
        ts = getattr(depth_cache, "update_time", None) or int(now * 1000)

        await self._store.save_orderbook_snapshot(
            symbol=self._symbol,
            timestamp=int(ts),
            bids=bids,
            asks=asks,
        )
        self._last_snapshot_ts = now
        self._snapshots_written += 1

        logger.debug(
            "snapshot persisted",
            symbol=self._symbol,
            best_bid=bids[0][0],
            best_ask=asks[0][0],
            total=self._snapshots_written,
        )

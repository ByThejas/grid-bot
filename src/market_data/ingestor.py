"""
Market Data Ingestor — coordinator that runs all streams for all symbols.

WHY a separate coordinator?
- OrderBookStream and TradeStream each manage one symbol's WebSocket.
- We need 4 streams total (BTCUSDT/ETHUSDT × order book/trade).
- Running them via asyncio.gather lets them share one event loop and one
  AsyncClient (Binance allows multiple subscriptions per connection).
- One stop_event coordinates clean shutdown across all of them: setting
  the event from a signal handler causes every stream to exit its loop,
  flush its writes, and return — giving us a deterministic teardown.

WHY one AsyncClient shared by all streams?
- Each AsyncClient owns an aiohttp session + a connection pool.
- Creating one per stream wastes file descriptors and re-does the TLS
  handshake on every API call.
- python-binance's BinanceSocketManager and DepthCacheManager are
  designed to share a single client across many streams.

LIFECYCLE:
    ingestor = MarketDataIngestor(api_key, secret, symbols, db_path)
    await ingestor.start()      # spins up all streams, returns immediately
    ...
    await ingestor.stop()       # signals stop, waits for clean exit

For tests we expose run_for_seconds() which does both with a timer.
"""

import asyncio
from typing import Optional

from binance import AsyncClient

from src.market_data.orderbook_stream import OrderBookStream
from src.market_data.storage import MarketDataStore
from src.market_data.trade_stream import TradeStream
from src.utils.logger import get_logger

logger = get_logger("ingestor")


class MarketDataIngestor:
    """
    Top-level orchestrator. One per process.

    Args:
        api_key, api_secret: Binance credentials (testnet keys for now).
        symbols: list of trading pairs, e.g. ["BTCUSDT", "ETHUSDT"].
        db_path: SQLite file path, e.g. "data/marketdata.db".
        snapshot_every_s: throttle for order book persistence.
        depth_levels: how many price levels to store per side.

        Note: this ingestor always connects to MAINNET WebSocket streams
        because Binance Spot Testnet's WS feed is unreliable. Public market
        data needs no auth, so this is safe. Orders go through a separate
        testnet client elsewhere.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: list[str],
        db_path: str,
        snapshot_every_s: float = 1.0,
        depth_levels: int = 20,
        reconnect_delay_s: float = 1.0,
        max_reconnect_attempts: int = 10,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = symbols
        self._db_path = db_path
        self._snapshot_every_s = snapshot_every_s
        self._depth_levels = depth_levels
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_attempts = max_reconnect_attempts

        self._client: Optional[AsyncClient] = None
        self._store: Optional[MarketDataStore] = None
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """
        Initialize client + storage, launch one task per stream.

        WHY mainnet (testnet=False) for the data client?
        Binance Spot Testnet's WebSocket streams are notoriously unreliable —
        connections succeed but pushes go silent for minutes at a time.
        Public market data (order book, trades) is the SAME on mainnet and
        requires NO authentication, so there is no reason to suffer testnet's
        flakiness. Order placement still goes through the testnet BinanceExchange
        (kept separate; constructed in main.py / strategy code), so capital is
        safe. This is the standard production paper-trading architecture.
        """
        self._client = await AsyncClient.create(
            self._api_key, self._api_secret, testnet=False
        )
        self._store = MarketDataStore(self._db_path)
        await self._store.connect()

        for symbol in self._symbols:
            ob = OrderBookStream(
                self._client,
                symbol,
                self._store,
                snapshot_every_s=self._snapshot_every_s,
                depth_levels=self._depth_levels,
                reconnect_delay_s=self._reconnect_delay_s,
                max_reconnect_attempts=self._max_reconnect_attempts,
            )
            tr = TradeStream(
                self._client,
                symbol,
                self._store,
                reconnect_delay_s=self._reconnect_delay_s,
                max_reconnect_attempts=self._max_reconnect_attempts,
            )
            self._tasks.append(asyncio.create_task(
                ob.run(self._stop_event), name=f"ob:{symbol}"
            ))
            self._tasks.append(asyncio.create_task(
                tr.run(self._stop_event), name=f"trade:{symbol}"
            ))

        logger.info("ingestor started",
                    symbols=self._symbols,
                    streams=len(self._tasks),
                    data_endpoint="mainnet")

    async def stop(self) -> None:
        """
        Signal stop, wait for every task to drain, close connections.

        WHY gather with return_exceptions=True?
        - If one stream raised (e.g. exhausted reconnects), gather without
          the flag would propagate immediately and leave the others
          half-cleaned. We want EVERY task to get a chance to flush.
        """
        logger.info("ingestor stopping")
        self._stop_event.set()

        if self._tasks:
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for task, result in zip(self._tasks, results):
                if isinstance(result, Exception):
                    logger.error("stream ended with error",
                                 task=task.get_name(),
                                 error=str(result))

        if self._store:
            await self._store.close()
        if self._client:
            await self._client.close_connection()

        logger.info("ingestor stopped cleanly")

    async def run_for_seconds(self, seconds: float) -> None:
        """Convenience for tests: start, sleep, stop."""
        await self.start()
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.stop()

    @property
    def store(self) -> MarketDataStore:
        """Expose the store so tests can query it after stop()."""
        return self._store

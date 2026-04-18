"""
Trade Stream — persists every executed trade via Binance WebSocket.

WHY trades, separate from order book?
- Order book shows INTENT (resting orders waiting to fill).
- Trade stream shows EXECUTION (orders that actually matched).
- Phase 4's S&D engine needs both:
    * Absorption = large resting wall (order book) repeatedly hit by
      aggressive market orders (trade stream) without moving the wall.
    * Footprint imbalance = ratio of buyer-aggressed vs seller-aggressed
      volume in a price bucket. Cannot derive from order book alone.

WHY persist EVERY trade (no throttle)?
- Trade volume is naturally low: ~5-50 trades/sec/symbol on testnet,
  realistic mainnet range. SQLite handles this trivially.
- Throttling would lose information we cannot reconstruct.
- The (symbol, trade_id) UNIQUE index makes redelivery on reconnect harmless.

Binance trade event schema (per @trade stream docs):
    e: "trade"      event type
    E: int          event time (ms)
    s: str          symbol
    t: int          trade id
    p: str          price
    q: str          quantity
    T: int          trade time (ms) — use this, not E
    m: bool         is buyer the maker?
                    True  → seller hit the bid (sell aggressor)
                    False → buyer lifted the ask (buy aggressor)
"""

import asyncio
from typing import Optional

from binance import AsyncClient, BinanceSocketManager

from src.market_data.storage import MarketDataStore
from src.utils.logger import get_logger

logger = get_logger("trade_stream")


class TradeStream:
    """
    One instance per symbol. Owns the @trade WebSocket subscription and
    inserts each trade into storage.

    USAGE:
        stream = TradeStream(client, "BTCUSDT", store)
        await stream.run(stop_event)
    """

    def __init__(
        self,
        client: AsyncClient,
        symbol: str,
        store: MarketDataStore,
        reconnect_delay_s: float = 1.0,
        max_reconnect_attempts: int = 10,
    ):
        self._client = client
        self._symbol = symbol
        self._store = store
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_attempts = max_reconnect_attempts
        self._trades_written = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop. Same shape as OrderBookStream.run for consistency."""
        attempts = 0
        while not stop_event.is_set():
            try:
                await self._consume_until_disconnect(stop_event)
                break
            except asyncio.CancelledError:
                logger.info("trade stream cancelled", symbol=self._symbol)
                raise
            except Exception as e:
                attempts += 1
                if attempts > self._max_reconnect_attempts:
                    logger.error(
                        "trade stream gave up after max reconnects",
                        symbol=self._symbol,
                        attempts=attempts,
                        error=str(e),
                    )
                    raise
                logger.warning(
                    "trade stream disconnected, will retry",
                    symbol=self._symbol,
                    attempt=attempts,
                    delay_s=self._reconnect_delay_s,
                    error=str(e),
                )
                await asyncio.sleep(self._reconnect_delay_s)

        logger.info(
            "trade stream stopped",
            symbol=self._symbol,
            trades_written=self._trades_written,
        )

    async def _consume_until_disconnect(self, stop_event: asyncio.Event) -> None:
        bm = BinanceSocketManager(self._client)
        # trade_socket() yields raw @trade events. For aggregated trades
        # use aggtrade_socket() — we want raw because it preserves trade_id
        # which is what we use for dedup.
        socket = bm.trade_socket(self._symbol)
        async with socket as ts:
            logger.info("trade stream connected", symbol=self._symbol)
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ts.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Defensive: skip malformed events. The lib occasionally
                # emits ping/pong wrappers that don't have trade fields.
                if not isinstance(msg, dict) or msg.get("e") != "trade":
                    continue

                await self._persist(msg)

    async def _persist(self, msg: dict) -> None:
        try:
            await self._store.save_trade(
                symbol=msg["s"],
                trade_id=int(msg["t"]),
                price=float(msg["p"]),
                quantity=float(msg["q"]),
                is_buyer_maker=bool(msg["m"]),
                timestamp=int(msg["T"]),
            )
            self._trades_written += 1
        except (KeyError, ValueError) as e:
            # Log and continue — one bad message should not kill the stream.
            logger.warning(
                "skipped malformed trade",
                symbol=self._symbol,
                error=str(e),
                msg_keys=list(msg.keys()) if isinstance(msg, dict) else None,
            )

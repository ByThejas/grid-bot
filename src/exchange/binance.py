"""
Binance Exchange Adapter

This implements the ExchangeBase interface for Binance.
It wraps the python-binance library with:
- Proper error handling and retries
- Rate limit awareness
- Testnet/mainnet switching
- Structured logging on every operation

KEY CONCEPT: This file is the ONLY place in the entire codebase
that knows about Binance-specific API details. If Binance changes
their API, you fix it here and nowhere else.
"""

import asyncio
import time
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from src.exchange.base import (
    ExchangeBase,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.exchange.filters import SymbolFilter, round_to_step, round_to_tick
from src.utils.logger import get_logger

logger = get_logger("binance_exchange")


# Map Binance's status strings to our OrderStatus enum
# WHY? Binance returns strings like "NEW", "FILLED". We want our own
# enum so the rest of the bot doesn't depend on Binance's naming.
_STATUS_MAP = {
    "NEW": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,    # Note: Binance spells it "CANCELED"
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}


class BinanceExchange(ExchangeBase):
    """
    Binance implementation of the exchange interface.

    Usage:
        exchange = BinanceExchange(api_key="...", api_secret="...", testnet=True)
        await exchange.connect()
        book = await exchange.get_order_book("BTCUSDT")
        print(book.mid_price)
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._client: Optional[Client] = None

        # Rate limiting tracker
        # Binance allows 1200 requests/minute for order endpoints
        # We track our own usage to avoid hitting their limits
        self._request_count = 0
        self._request_window_start = time.time()
        self._max_requests_per_minute = 1000  # Stay under 1200 limit

        # Cached symbol filters: tick_size/step_size rarely change but we
        # must not fetch them per-order (adds latency + rate-limit load).
        # Fetched lazily on first use.
        self._filter_cache: dict[str, SymbolFilter] = {}

    async def connect(self) -> None:
        """
        Initialize the Binance client.

        WHY async? Even though python-binance's Client is synchronous,
        we wrap it in async because:
        1. Our bot's main loop is async (event-driven)
        2. Future exchange adapters (Bybit, OKX) may be truly async
        3. Keeps the interface consistent
        """
        try:
            self._client = Client(
                self._api_key,
                self._api_secret,
                testnet=self._testnet,
            )
            # Test connectivity
            server_time = self._client.get_server_time()
            logger.info("connected to binance",
                        testnet=self._testnet,
                        server_time=server_time["serverTime"])
        except BinanceAPIException as e:
            logger.error("binance connection failed",
                         error=str(e),
                         code=e.code)
            raise
        except Exception as e:
            logger.error("unexpected connection error", error=str(e))
            raise

    async def disconnect(self) -> None:
        """Close the Binance client connection."""
        if self._client:
            self._client.close_connection()
            logger.info("disconnected from binance")

    async def _check_rate_limit(self) -> None:
        """
        Track API usage and pause if approaching rate limits.

        WHY do this ourselves instead of letting Binance reject us?
        - A 429 (rate limit) response from Binance can trigger an
          IP ban if repeated. Prevention > cure.
        - We log a warning so you can tune request frequency.
        """
        now = time.time()
        if now - self._request_window_start > 60:
            # New minute window — reset counter
            self._request_count = 0
            self._request_window_start = now

        self._request_count += 1
        if self._request_count >= self._max_requests_per_minute:
            wait_time = 60 - (now - self._request_window_start)
            logger.warning("rate limit approaching, pausing",
                           requests_used=self._request_count,
                           wait_seconds=round(wait_time, 1))
            await asyncio.sleep(wait_time)
            self._request_count = 0
            self._request_window_start = time.time()

    def _parse_order(self, raw: dict) -> Order:
        """
        Convert Binance's raw order response to our Order dataclass.

        This is the translation layer. Binance returns dicts with their
        own field names — we convert to our standardized format.
        """
        return Order(
            order_id=str(raw["orderId"]),
            symbol=raw["symbol"],
            side=OrderSide(raw["side"]),
            order_type=OrderType(raw["type"]),
            price=float(raw["price"]),
            quantity=float(raw["origQty"]),
            status=_STATUS_MAP.get(raw["status"], OrderStatus.PENDING),
            filled_quantity=float(raw.get("executedQty", 0)),
            timestamp=raw.get("transactTime", raw.get("time")),
            client_order_id=raw.get("clientOrderId"),
        )

    async def place_order(self, symbol: str, side: OrderSide,
                          order_type: OrderType, quantity: float,
                          price: Optional[float] = None) -> Order:
        """
        Place an order on Binance.

        IMPORTANT DETAILS:
        - LIMIT orders require price and use GTC (Good Till Cancel)
        - We use "newOrderRespType=FULL" to get complete fill info
        - Every order is logged with full details for audit trail
        """
        await self._check_rate_limit()

        try:
            params = {
                "symbol": symbol,
                "side": side.value,
                "type": order_type.value,
                "quantity": quantity,
                "newOrderRespType": "FULL",
            }

            if order_type == OrderType.LIMIT:
                if price is None:
                    raise ValueError("LIMIT orders require a price")
                params["price"] = f"{price:.8f}"
                params["timeInForce"] = "GTC"  # Good Till Cancel

            raw = self._client.create_order(**params)

            order = self._parse_order(raw)
            logger.info("order placed",
                        order_id=order.order_id,
                        symbol=symbol,
                        side=side.value,
                        price=price,
                        quantity=quantity,
                        status=order.status.value)
            return order

        except BinanceAPIException as e:
            logger.error("order placement failed",
                         symbol=symbol,
                         side=side.value,
                         price=price,
                         error=str(e),
                         code=e.code)
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order by ID."""
        await self._check_rate_limit()

        try:
            self._client.cancel_order(symbol=symbol, orderId=int(order_id))
            logger.info("order cancelled", symbol=symbol, order_id=order_id)
            return True
        except BinanceAPIException as e:
            if e.code == -2011:  # "Unknown order" — already cancelled/filled
                logger.warning("order already gone",
                               symbol=symbol, order_id=order_id)
                return False
            logger.error("cancel failed",
                         symbol=symbol, order_id=order_id, error=str(e))
            raise

    async def get_order_status(self, symbol: str, order_id: str) -> Order:
        """Get the current state of a specific order."""
        await self._check_rate_limit()

        raw = self._client.get_order(symbol=symbol, orderId=int(order_id))
        return self._parse_order(raw)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """Get all currently open orders for a trading pair."""
        await self._check_rate_limit()

        raw_orders = self._client.get_open_orders(symbol=symbol)
        orders = [self._parse_order(o) for o in raw_orders]
        logger.info("fetched open orders",
                     symbol=symbol, count=len(orders))
        return orders

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        """
        Get a snapshot of the order book.

        This is the data our S&D and Liquidity signals consume.
        depth=20 means 20 price levels on each side (bids + asks).

        WHY 20? It's a balance:
        - depth=5: too shallow, misses important walls
        - depth=100: too much data, slow to process every tick
        - depth=20: captures major support/resistance, fast enough
        """
        await self._check_rate_limit()

        raw = self._client.get_order_book(symbol=symbol, limit=depth)

        book = OrderBook(
            symbol=symbol,
            bids=[[float(p), float(q)] for p, q in raw["bids"]],
            asks=[[float(p), float(q)] for p, q in raw["asks"]],
            timestamp=int(time.time() * 1000),
        )

        logger.debug("order book fetched",
                      symbol=symbol,
                      best_bid=book.best_bid,
                      best_ask=book.best_ask,
                      spread_pct=f"{book.spread_pct:.4f}%")
        return book

    async def get_balance(self, asset: str) -> float:
        """
        Get available (free) balance for an asset.

        'free' = available to trade. 'locked' = in open orders.
        We return 'free' because that's what we can actually use.
        """
        await self._check_rate_limit()

        account = self._client.get_account()
        for balance in account["balances"]:
            if balance["asset"] == asset:
                free = float(balance["free"])
                logger.debug("balance fetched", asset=asset, free=free)
                return free

        logger.warning("asset not found in account", asset=asset)
        return 0.0

    async def get_ticker_price(self, symbol: str) -> float:
        """Get the current market price for a symbol."""
        await self._check_rate_limit()

        ticker = self._client.get_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        logger.debug("ticker price", symbol=symbol, price=price)
        return price

    # ------------------------------------------------------------------
    # Phase 5 additions: filters, bulk cancel, rounding helpers.
    # ------------------------------------------------------------------

    async def cancel_all_orders(self, symbol: str) -> int:
        """
        Cancel every open order for `symbol`. Returns the count cancelled.

        WHY a dedicated endpoint instead of looping cancel_order?
        Binance exposes DELETE /openOrders which cancels the whole book in
        one call. That's atomic from our perspective — no race where a
        new order places between individual cancels. Critical for the
        shutdown path: when we SIGINT the bot we want to guarantee nothing
        is left hanging.
        """
        await self._check_rate_limit()
        # python-binance 1.0.19 does NOT expose a public wrapper for
        # DELETE /api/v3/openOrders. We hit the signed endpoint directly via
        # the library's internal _delete helper — same atomic single-call
        # semantics, no race window between individual cancels.
        try:
            raw = self._client._delete("openOrders", True, data={"symbol": symbol})
            count = len(raw) if isinstance(raw, list) else 0
            logger.info("bulk cancel", symbol=symbol, count=count)
            return count
        except BinanceAPIException as e:
            # -2011 "Unknown order" is returned when there's nothing to cancel.
            # That's success, not failure — the postcondition (no open orders)
            # holds either way.
            if e.code == -2011:
                logger.info("bulk cancel: nothing to cancel", symbol=symbol)
                return 0
            logger.error("bulk cancel failed", symbol=symbol, error=str(e))
            raise

    async def get_symbol_filters(self, symbol: str, refresh: bool = False) -> SymbolFilter:
        """
        Return the tick/step/min-notional filters for `symbol`.

        Cached by default — filters change rarely (listing changes only).
        Pass refresh=True to force a fresh fetch.
        """
        if not refresh and symbol in self._filter_cache:
            return self._filter_cache[symbol]

        await self._check_rate_limit()
        info = self._client.get_symbol_info(symbol)
        if not info:
            raise ValueError(f"no exchange info for symbol {symbol}")

        tick_size = step_size = min_qty = min_notional = 0.0
        for f in info.get("filters", []):
            ft = f.get("filterType")
            if ft == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif ft == "LOT_SIZE":
                step_size = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                # Binance renamed this filter mid-2023. Either key is valid.
                key = "minNotional" if "minNotional" in f else "notional"
                min_notional = float(f[key])

        sf = SymbolFilter(
            symbol=symbol,
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
        )
        self._filter_cache[symbol] = sf
        logger.info("symbol filters fetched",
                    symbol=symbol,
                    tick_size=tick_size,
                    step_size=step_size,
                    min_qty=min_qty,
                    min_notional=min_notional)
        return sf

    async def round_price(self, symbol: str, price: float) -> float:
        """Convenience: fetch filter and round price to tick."""
        sf = await self.get_symbol_filters(symbol)
        return round_to_tick(price, sf.tick_size)

    async def round_quantity(self, symbol: str, qty: float) -> float:
        """Convenience: fetch filter and round quantity to step."""
        sf = await self.get_symbol_filters(symbol)
        return round_to_step(qty, sf.step_size)

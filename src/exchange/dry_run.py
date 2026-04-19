"""
DryRunExchange — a transparent wrapper that forwards read-only calls to a
real ExchangeBase and *intercepts* every mutating call (place / cancel).

WHY this design (wrap, not a new adapter)?
- The grid engine, user data stream, config, signal — none of them change.
  They still get a fully-functional `ExchangeBase`, and they keep reading
  live prices, order books, balances, filters from the real exchange.
- Only writes are stubbed. That's exactly what we want for a "will this
  grid place sanely?" rehearsal: real prices in, fake orders out.
- Keeping it outside `BinanceExchange` means the real adapter stays lean
  and there's zero chance a rogue if/else accidentally sends a live order.

WHAT IT DOES
- `place_order`: logs intent, returns a synthetic `Order` with a
  `DRYRUN-N` id. No network call. Order is NOT tracked by the wrapper —
  the engine tracks in its own `GridState` as usual.
- `cancel_order` / `cancel_all_orders`: logs intent and returns success.
- Read paths (`get_ticker_price`, `get_order_book`, `get_balance`,
  `get_symbol_filters`, `get_order_status`, `get_open_orders`): forwarded
  to the inner exchange unmodified.

WHAT IT DOES NOT DO
- It does not simulate fills. The user data stream will simply receive
  nothing because we never placed a real order. That's fine for dry-run:
  we're verifying placement math, not walk-forward performance.
- It does not track order state. Returning `DRYRUN-N` ids is enough for
  the engine to populate its `GridState`; no reconciliation happens
  because nothing ever fills.
"""

import itertools
import time
from typing import Optional

from src.exchange.base import (
    ExchangeBase,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.utils.logger import get_logger

logger = get_logger("dry_run_exchange")


class DryRunExchange(ExchangeBase):
    """Wrap a real ExchangeBase. Log mutating calls; forward reads."""

    def __init__(self, inner: ExchangeBase):
        self._inner = inner
        self._id_counter = itertools.count(1)

    # ------------------------------------------------------------------
    # Lifecycle — forwarded
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self._inner.connect()
        logger.info("dry-run mode ACTIVE — no real orders will be placed")

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    # ------------------------------------------------------------------
    # Mutating — intercepted
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
    ) -> Order:
        order_id = f"DRYRUN-{next(self._id_counter)}"
        logger.info("DRY-RUN place_order",
                    order_id=order_id,
                    symbol=symbol,
                    side=side.value,
                    type=order_type.value,
                    qty=quantity,
                    price=price)
        return Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price if price is not None else 0.0,
            quantity=quantity,
            status=OrderStatus.OPEN,
            timestamp=int(time.time() * 1000),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        logger.info("DRY-RUN cancel_order", symbol=symbol, order_id=order_id)
        return True

    async def cancel_all_orders(self, symbol: str) -> int:
        logger.info("DRY-RUN cancel_all_orders", symbol=symbol)
        return 0

    # ------------------------------------------------------------------
    # Read-only — forwarded
    # ------------------------------------------------------------------

    async def get_order_status(self, symbol: str, order_id: str) -> Order:
        # Dry-run ids were never sent to the exchange — short-circuit.
        if order_id.startswith("DRYRUN-"):
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=0.0,
                quantity=0.0,
                status=OrderStatus.OPEN,
            )
        return await self._inner.get_order_status(symbol, order_id)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        # The inner exchange is the source of truth for real orders. In
        # dry-run we placed none, so forwarding is still correct (returns
        # whatever live orders exist on the account — usually empty on
        # testnet before a run).
        return await self._inner.get_open_orders(symbol)

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return await self._inner.get_order_book(symbol, depth)

    async def get_balance(self, asset: str) -> float:
        return await self._inner.get_balance(asset)

    async def get_ticker_price(self, symbol: str) -> float:
        return await self._inner.get_ticker_price(symbol)

    async def get_symbol_filters(self, symbol: str):
        return await self._inner.get_symbol_filters(symbol)

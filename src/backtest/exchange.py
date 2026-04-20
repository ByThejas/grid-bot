"""
BacktestExchange — in-memory ExchangeBase for replay-driven simulation.

WHY this exists:
Testnet is slow, rate-limited, and rarely moves enough to exercise
recenters, replacements, liquidity pauses, or non-zero S&D zones.
Mainnet burns real money. We need a third mode where the ENGINE is
unchanged (same ExchangeBase surface) but the "exchange" is a fiction
under our control: we decide prices, we decide when fills happen, we
decide how the book looks. The engine can't tell the difference.

================================================================
DESIGN
================================================================
The backtest has TWO clocks:
  * wall-clock asyncio time (what engine.run sleeps against)
  * virtual simulation time (what the price path is indexed by)

In v1 we keep them ONE-TO-ONE but speed-adjustable: the runner calls
`exchange.set_price(...)` on its own schedule, so effectively the
engine's drift_check_interval_s is measured against the pace of the
runner's ticks. Setting drift_check_interval_s to a small value and
running the runner fast gets us a fast replay.

Fill matching (v1):
- LIMIT BUY at price P fills when the tape trades ≤ P (someone was
  willing to sell at or below our bid → we'd have been hit).
- LIMIT SELL at price P fills when the tape trades ≥ P.
- Fills are at the ORDER's limit price (passive fills). No slippage,
  no partial fills, no queue-position modelling. These are all v2.
- Fill events are emitted via an `on_fill_event` callback that the
  runner points at `engine.on_fill`, mimicking UserDataStream.

Book (v1):
- Synthesized around the current price with a configurable fake
  spread. Enough for the LiquiditySignal to produce non-pause output.
- Historical book replay (from `data/marketdata.db` snapshots) is v2.

Filters:
- Defaults hardcoded to match Binance production values for BTCUSDT
  and ETHUSDT as of 2026-04. If the operator wants to exercise a
  different filter regime, they can override via `filters=...`.
"""

import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from src.exchange.base import (
    ExchangeBase,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.exchange.filters import SymbolFilter
from src.utils.logger import get_logger

logger = get_logger("backtest_exchange")


# Sensible defaults pulled from Binance mainnet filter API (production
# BTCUSDT / ETHUSDT, 2026-04). If the real values drift we can refresh
# these — they only matter for rejecting absurd order sizes in a sim.
DEFAULT_FILTERS: dict[str, SymbolFilter] = {
    "BTCUSDT": SymbolFilter(
        symbol="BTCUSDT", tick_size=0.01, step_size=0.00001,
        min_qty=0.00001, min_notional=5.0,
    ),
    "ETHUSDT": SymbolFilter(
        symbol="ETHUSDT", tick_size=0.01, step_size=0.0001,
        min_qty=0.0001, min_notional=5.0,
    ),
}


FillCallback = Callable[[dict], Awaitable[None]]


@dataclass
class _OpenOrder:
    """Internal bookkeeping — distinct from public Order so the exchange
    can track what's needed for fill matching without leaking internals."""
    order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    placed_at: float  # virtual timestamp (seconds)


class BacktestExchange(ExchangeBase):
    """
    USAGE (from a runner):
        ex = BacktestExchange(symbols=["BTCUSDT"])
        ex.on_fill_event = engine.on_fill   # same contract UserDataStream uses
        ex.set_price("BTCUSDT", 75_000.0)
        await ex.connect()
        # ...later, after engine.run() placed its grid...
        await ex.tick_trade("BTCUSDT", price=74_800.0, qty=0.5)
        # Any BUY orders at >= 74_800 just filled. on_fill_event was awaited
        # once per filled order with a Binance-shaped executionReport dict.
    """

    def __init__(
        self,
        *,
        symbols: Optional[list[str]] = None,
        filters: Optional[dict[str, SymbolFilter]] = None,
        synthetic_spread_bps: float = 1.0,
        synthetic_book_depth: int = 20,
        synthetic_book_qty: float = 10.0,
        initial_balances: Optional[dict[str, float]] = None,
    ):
        self._symbols = list(symbols or DEFAULT_FILTERS.keys())
        # Filters come from caller-provided map first, then defaults.
        f = dict(DEFAULT_FILTERS)
        if filters:
            f.update(filters)
        self._filters = f

        # Current simulated state.
        self._prices: dict[str, float] = {}
        self._open_orders: dict[str, _OpenOrder] = {}
        self._id_counter = itertools.count(1)
        # Virtual clock — seconds. Starts at 0. Runner advances via
        # advance_time() or implicitly via tick_trade(timestamp=...).
        self._now: float = 0.0

        # Synthetic-book parameters. These shape what liquidity signal sees.
        self._spread_bps = synthetic_spread_bps
        self._book_depth = synthetic_book_depth
        self._book_qty = synthetic_book_qty

        # Balances are not meaningful in v1 (no position accounting) but
        # some callers may want a non-zero USDT figure. Default 100k USDT
        # per symbol's quote asset is enough for any sane grid.
        self._balances: dict[str, float] = dict(initial_balances or {
            "USDT": 100_000.0,
            "BTC": 1.0,
            "ETH": 10.0,
        })

        # Callback the runner points at engine.on_fill. Set externally
        # before the first fill-generating tick. If unset when a fill
        # occurs we log and drop the event — this is an integration bug,
        # not a data bug, so crashing is wrong but silence would be worse.
        self.on_fill_event: Optional[FillCallback] = None

        # Stats — observability only; read by the runner at shutdown.
        self.placed_count = 0
        self.cancelled_count = 0
        self.filled_count = 0
        self.rejected_count = 0

    # ------------------------------------------------------------------
    # Runner-facing control surface (NOT part of ExchangeBase)
    # ------------------------------------------------------------------

    def set_price(self, symbol: str, price: float) -> None:
        """Set the current ticker price. Does NOT trigger fill matching —
        use tick_trade() for that. Separating lets the runner update
        prices between ticks without double-counting trades for fills."""
        self._prices[symbol] = price

    def advance_time(self, seconds: float) -> None:
        """Move the virtual clock forward. Pure bookkeeping — no side
        effects on orders. Runner calls this to keep placed_at / fill
        timestamps coherent with the replay timeline."""
        self._now += seconds

    async def tick_trade(
        self,
        symbol: str,
        price: float,
        qty: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> list[str]:
        """
        Apply one trade print to the simulated tape.

        This is the ONE call that drives fills. The runner invokes it for
        every historical (or synthetic) trade. Returns the list of
        order_ids that filled on this tick — useful for the runner's
        stats and for tests.

        Fill rule:
          - BUY order at limit L fills if trade price <= L
          - SELL order at limit L fills if trade price >= L
        Fills are at the order's limit price, not the trade price —
        this models a passive maker order being hit.
        """
        if timestamp is not None:
            self._now = max(self._now, timestamp)
        self._prices[symbol] = price

        filled_ids: list[str] = []
        # Copy ids snapshot — we mutate _open_orders during iteration.
        for oid in list(self._open_orders.keys()):
            o = self._open_orders.get(oid)
            if o is None or o.symbol != symbol:
                continue
            crossed = (
                (o.side == OrderSide.BUY and price <= o.price)
                or (o.side == OrderSide.SELL and price >= o.price)
            )
            if not crossed:
                continue
            # Remove BEFORE dispatching so the on_fill handler sees a
            # coherent state if it synchronously asks back.
            del self._open_orders[oid]
            self.filled_count += 1
            filled_ids.append(oid)
            await self._emit_fill(o)
            # qty is advisory — used for partial fills in v2. Log if the
            # trade qty would have been too small to fully fill us, for
            # future realism tuning.
            if qty and qty < o.quantity:
                logger.debug("trade qty below order qty — v1 still fully fills",
                             order_id=oid, trade_qty=qty, order_qty=o.quantity)
        return filled_ids

    async def _emit_fill(self, order: _OpenOrder) -> None:
        """Build a Binance-shaped executionReport and dispatch it."""
        event = {
            "e": "executionReport",
            "s": order.symbol,
            "i": int(order.order_id) if order.order_id.isdigit() else order.order_id,
            "X": "FILLED",
            "S": order.side.value,
            "L": str(order.price),   # Binance sends strings; engine does float(...)
            "l": str(order.quantity),
            "E": int(self._now * 1000),
        }
        if self.on_fill_event is None:
            logger.warning("fill event dropped — on_fill_event not wired",
                           order_id=order.order_id)
            return
        try:
            await self.on_fill_event(event)
        except Exception as e:
            # Don't let a handler bug halt the backtest — surface + continue.
            logger.error("on_fill_event handler raised",
                         order_id=order.order_id, error=str(e))

    # ------------------------------------------------------------------
    # ExchangeBase — lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        logger.info("backtest exchange connected",
                    symbols=self._symbols, spread_bps=self._spread_bps)

    async def disconnect(self) -> None:
        logger.info("backtest exchange disconnected",
                    placed=self.placed_count,
                    cancelled=self.cancelled_count,
                    filled=self.filled_count,
                    rejected=self.rejected_count,
                    open_at_end=len(self._open_orders))

    # ------------------------------------------------------------------
    # ExchangeBase — writes
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
    ) -> Order:
        if order_type != OrderType.LIMIT:
            # Grid engine only ever sends LIMIT in v1. MARKET orders would
            # need slippage modelling we haven't built.
            self.rejected_count += 1
            raise ValueError("BacktestExchange v1 only supports LIMIT orders")
        if price is None or price <= 0:
            self.rejected_count += 1
            raise ValueError("LIMIT order requires a positive price")

        # Immediate-fill guard: if the limit already crosses the current
        # market, we treat it as an immediate fill. This matches real-
        # exchange behavior for aggressive limits. Not a reject.
        current = self._prices.get(symbol, 0.0)
        order_id = str(next(self._id_counter))
        self.placed_count += 1

        immediate_fill = False
        if current > 0:
            if side == OrderSide.BUY and current <= price:
                immediate_fill = True
            elif side == OrderSide.SELL and current >= price:
                immediate_fill = True

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            status=OrderStatus.FILLED if immediate_fill else OrderStatus.OPEN,
            filled_quantity=quantity if immediate_fill else 0.0,
            timestamp=int(self._now * 1000),
        )

        if immediate_fill:
            # Immediate fills DO get a fill event — same as a real exchange
            # would dispatch an executionReport. The order is never tracked
            # as "open" because it filled at submit.
            self.filled_count += 1
            await self._emit_fill(_OpenOrder(
                order_id=order_id, symbol=symbol, side=side,
                price=price, quantity=quantity, placed_at=self._now,
            ))
        else:
            self._open_orders[order_id] = _OpenOrder(
                order_id=order_id, symbol=symbol, side=side,
                price=price, quantity=quantity, placed_at=self._now,
            )
        return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        order = self._open_orders.pop(order_id, None)
        if order is None:
            return False
        self.cancelled_count += 1
        return True

    async def cancel_all_orders(self, symbol: str) -> int:
        to_cancel = [oid for oid, o in self._open_orders.items() if o.symbol == symbol]
        for oid in to_cancel:
            del self._open_orders[oid]
        self.cancelled_count += len(to_cancel)
        return len(to_cancel)

    # ------------------------------------------------------------------
    # ExchangeBase — reads
    # ------------------------------------------------------------------

    async def get_order_status(self, symbol: str, order_id: str) -> Order:
        o = self._open_orders.get(order_id)
        if o is None:
            # Not open — treat as filled/cancelled; we don't keep a history
            # table in v1. Engine only polls status for active orders.
            raise KeyError(f"order {order_id} not found in open book")
        return Order(
            order_id=o.order_id, symbol=o.symbol, side=o.side,
            order_type=OrderType.LIMIT, price=o.price, quantity=o.quantity,
            status=OrderStatus.OPEN, filled_quantity=0.0,
            timestamp=int(o.placed_at * 1000),
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return [
            Order(
                order_id=o.order_id, symbol=o.symbol, side=o.side,
                order_type=OrderType.LIMIT, price=o.price, quantity=o.quantity,
                status=OrderStatus.OPEN, filled_quantity=0.0,
                timestamp=int(o.placed_at * 1000),
            )
            for o in self._open_orders.values()
            if o.symbol == symbol
        ]

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        price = self._prices.get(symbol, 0.0)
        if price <= 0:
            return OrderBook(symbol=symbol, bids=[], asks=[], timestamp=int(self._now * 1000))
        # Half-spread in absolute terms. With 1 bp spread at $75k that's
        # $3.75 on each side.
        half_spread = price * (self._spread_bps / 2.0) / 10_000.0
        # Levels step out by half_spread each — so level 0 is inside-the-
        # quote, and level N is ~N × half_spread from mid. This gives the
        # liquidity signal a well-shaped book it can compute imbalance on.
        levels = min(depth, self._book_depth)
        bids = [[price - half_spread * (i + 1), self._book_qty] for i in range(levels)]
        asks = [[price + half_spread * (i + 1), self._book_qty] for i in range(levels)]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=int(self._now * 1000))

    async def get_balance(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    async def get_ticker_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    async def get_symbol_filters(self, symbol: str) -> SymbolFilter:
        f = self._filters.get(symbol)
        if f is None:
            raise KeyError(f"no filter configured for {symbol}")
        return f

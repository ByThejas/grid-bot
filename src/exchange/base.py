"""
Abstract Exchange Interface

WHY an abstract base class?
Imagine you build the entire bot hardcoded to Binance. Then:
- Binance goes down for maintenance → bot is dead
- Binance bans your country → rewrite everything
- You find better fees on Bybit → rewrite everything

With an abstract interface:
- You define WHAT operations exist (place order, cancel order, get book)
- Each exchange implements HOW it does them
- The rest of the bot only talks to the interface
- Swapping exchanges = changing one line in config

This is called the "Strategy Pattern" in software design.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(Enum):
    PENDING = "PENDING"       # Sent to exchange, not confirmed yet
    OPEN = "OPEN"             # Confirmed, sitting on the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"         # Fully executed
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class Order:
    """
    Represents a single order.

    WHY a dataclass?
    - It's a plain data container — no behavior, just fields
    - Python auto-generates __init__, __repr__, __eq__
    - Cleaner than a dict (you get autocomplete and type safety)
    """
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: float
    quantity: float
    status: OrderStatus
    filled_quantity: float = 0.0
    timestamp: Optional[int] = None       # Exchange timestamp (ms)
    client_order_id: Optional[str] = None # Our own ID for tracking


@dataclass
class OrderBook:
    """
    Snapshot of the order book at a point in time.

    bids: list of [price, quantity] — buyers (highest first)
    asks: list of [price, quantity] — sellers (lowest first)
    """
    symbol: str
    bids: list  # [[price, qty], [price, qty], ...]
    asks: list  # [[price, qty], [price, qty], ...]
    timestamp: int

    @property
    def best_bid(self) -> float:
        """Highest price someone is willing to buy at."""
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        """Lowest price someone is willing to sell at."""
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        """Midpoint between best bid and ask — the 'fair' price."""
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        """Gap between best ask and best bid."""
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        """Spread as percentage of mid price."""
        mid = self.mid_price
        return (self.spread / mid * 100) if mid > 0 else 0.0


class ExchangeBase(ABC):
    """
    Abstract interface that every exchange must implement.

    When you see @abstractmethod, it means:
    "Any class that inherits from ExchangeBase MUST implement this method."
    If they don't, Python throws an error at startup — not at 3am when
    you're asleep and the bot tries to place an order.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the exchange."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close connection."""
        ...

    @abstractmethod
    async def place_order(self, symbol: str, side: OrderSide,
                          order_type: OrderType, quantity: float,
                          price: Optional[float] = None) -> Order:
        """
        Place an order on the exchange.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            side: BUY or SELL
            order_type: LIMIT or MARKET
            quantity: Amount of base asset
            price: Required for LIMIT orders

        Returns:
            Order object with exchange-assigned ID and status
        """
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        ...

    @abstractmethod
    async def get_order_status(self, symbol: str, order_id: str) -> Order:
        """Get current status of an order."""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[Order]:
        """Get all open orders for a symbol."""
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        """
        Get current order book snapshot.

        Args:
            symbol: Trading pair
            depth: Number of price levels per side (default 20)
        """
        ...

    @abstractmethod
    async def get_balance(self, asset: str) -> float:
        """Get available balance for an asset (e.g., 'BTC', 'USDT')."""
        ...

    async def get_account_balances(self) -> dict[str, float]:
        """
        Get TOTAL balance (free + locked) for every asset in the account.

        Phase 11D added this to support dynamic sizing — the balance poller
        needs free+locked because locked balances are still the operator's
        money, just sitting in open orders. Without locking included,
        placing the grid would shrink "equity" each cycle.

        Returns dict of asset → total amount, e.g.
            {'USDT': 95.36, 'BTC': 0.0009, 'ETH': 0.013}

        Default implementation raises NotImplementedError so adapters that
        don't support balance queries (e.g. BacktestExchange) don't have
        to opt in. Concrete adapters that DO support it (BinanceExchange,
        DryRunExchange wrapping it) override.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_account_balances. "
            f"Required for Phase 11D dynamic sizing."
        )

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> float:
        """Get current price for a symbol."""
        ...

    # ------------------------------------------------------------------
    # Phase 5 additions. Every concrete adapter (and every test fake) must
    # implement these — they are how the grid engine learns the exchange's
    # rules and how it shuts down cleanly.
    # ------------------------------------------------------------------

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> int:
        """Cancel every open order for `symbol`. Returns count cancelled."""
        ...

    @abstractmethod
    async def get_symbol_filters(self, symbol: str):
        """
        Return a SymbolFilter with tick/step/min_notional for the symbol.
        Return type is 'SymbolFilter' (from src.exchange.filters) — not
        annotated here to avoid circular import at interface level.
        """
        ...

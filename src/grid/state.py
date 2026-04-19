"""
Grid State — in-memory record of currently-placed orders.

WHY a dedicated type and not just a dict on the engine?
- Makes the engine's responsibilities obvious: it manipulates state via
  named operations, not ad-hoc dict mutation.
- Single place to add invariants (e.g., "refuse duplicate order_ids") if
  they become important.
- Pure Python object → trivially unit-testable.

This is CRASH-VOLATILE: if the bot dies, we lose track of placed orders
until the next reconcile (which re-reads open_orders from the exchange).
That's fine — state rebuild is always possible because the exchange is
the source of truth.
"""

from dataclasses import dataclass, field


@dataclass
class TrackedOrder:
    """
    What we remember locally about one placed order.
    This is a subset of the full Order object — only the fields the engine
    actually needs for replacement logic.
    """
    order_id: str
    side: str           # "BUY" or "SELL"
    price: float
    size: float


@dataclass
class GridState:
    """
    Mutable state of the grid for ONE symbol.

    One instance per engine (one engine per symbol in v1). Keeping it
    per-symbol avoids cross-symbol leaks in the replacement logic.
    """
    center_price: float = 0.0
    orders: dict[str, TrackedOrder] = field(default_factory=dict)

    def add_order(self, order_id: str, side: str, price: float, size: float) -> None:
        self.orders[order_id] = TrackedOrder(
            order_id=order_id, side=side, price=price, size=size
        )

    def remove_order(self, order_id: str) -> TrackedOrder | None:
        """Pop and return the tracked order, or None if unknown."""
        return self.orders.pop(order_id, None)

    def get_order(self, order_id: str) -> TrackedOrder | None:
        return self.orders.get(order_id)

    def clear(self) -> None:
        self.orders.clear()

    def __len__(self) -> int:
        return len(self.orders)

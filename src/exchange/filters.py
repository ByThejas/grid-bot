"""
Exchange Symbol Filters — the rules every order must obey.

WHY THIS MATTERS
Binance (and every real exchange) rejects orders that don't comply with
per-symbol "filters":
    PRICE_FILTER     : price must be a multiple of tick_size
    LOT_SIZE         : quantity must be a multiple of step_size
    NOTIONAL         : price * quantity must be ≥ min_notional

Sending an order that violates a filter returns an immediate error from
the exchange. The grid engine CANNOT know these rules ahead of time —
they differ per symbol (BTCUSDT tick is 0.01, ETHUSDT tick is 0.01 too
but step sizes differ, and the min_notional can be $10-20 depending on
market conditions). This module owns the rounding logic so the engine
just calls `round_price` / `round_quantity` and never thinks about it.

WHY a dedicated file and not a method on BinanceExchange?
- Pure functions — no network, no state. Easy to test.
- Other exchanges (Bybit, OKX) will have the same concept; their adapter
  will populate a SymbolFilter and reuse these rounding helpers.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolFilter:
    """
    Per-symbol trading rules enforced by the exchange.

    All values are in the same units the exchange uses:
        tick_size, min_notional : quote currency (USDT for BTCUSDT)
        step_size, min_qty      : base currency (BTC for BTCUSDT)
    """
    symbol: str
    tick_size: float       # smallest price increment, e.g. 0.01 USDT
    step_size: float       # smallest quantity increment, e.g. 0.00001 BTC
    min_qty: float         # smallest allowed quantity per order
    min_notional: float    # smallest allowed (price * qty) per order


def round_to_tick(price: float, tick_size: float) -> float:
    """
    Snap `price` DOWN to the nearest multiple of tick_size.

    WHY FLOOR and not round-to-nearest?
    For a BUY order we'd rather place slightly below (better fill price
    for us); for a SELL order we'd rather place slightly above. A single
    global rounding direction is wrong in general, but the engine rounds
    both after placement has already chosen a target price — at that
    point, floor is the more conservative choice (price moves in our
    favor vs the round-to-nearest case where it might move against).

    For v1 this is fine. If a future policy cares, it can call floor or
    ceil explicitly and skip this helper.
    """
    if tick_size <= 0:
        return price
    # Using math.floor after division avoids the float-precision issue
    # where 0.1 / 0.01 = 9.999999999 instead of 10.
    # The round(..., 12) before floor picks the intended integer;
    # the round(..., 12) after multiply strips the noise introduced by
    # multiplying back through a non-exact float (e.g. 0.00001 is not
    # exactly 1e-5 in IEEE 754).
    n = math.floor(round(price / tick_size, 12))
    return round(n * tick_size, 12)


def round_to_step(qty: float, step_size: float) -> float:
    """
    Snap `qty` DOWN to the nearest multiple of step_size.

    Always floor for quantities — we can't ship an order that's larger
    than we intended to risk. If rounding down produces 0, the caller is
    asking for a sub-minimum order and should be rejected upstream.
    """
    if step_size <= 0:
        return qty
    n = math.floor(round(qty / step_size, 12))
    return round(n * step_size, 12)


def meets_min_notional(price: float, qty: float, min_notional: float) -> bool:
    """True if the order passes Binance's NOTIONAL filter."""
    return price * qty >= min_notional

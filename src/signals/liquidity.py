"""
Liquidity Signal — second pillar of the signal stack.

WHY this signal exists:
A grid bot's profit per round-trip ≈ spacing − 2× fees. If the bid/ask
spread approaches the spacing, every cycle loses money to the spread on
top of fees. Conversely, in a tight, deep market we can run a denser
grid and capture more cycles. Static spacing ignores this and is wrong
in either regime — too sparse when liquidity is rich, suicidal when
liquidity is poor.

So this signal reads the order book and produces a `LiquidityState`
the grid engine consults at init and on every recenter:
  * spread_bps               — current spread in basis points
  * imbalance                — depth-weighted bid vs ask (-1..+1)
  * recommended_spacing_pct  — clamped function of spread + base
  * should_pause             — true if spread exceeds the broken-market
                               threshold; engine refuses to place

Imbalance is computed and logged in v1 but NOT yet wired into placement.
Asymmetric spacing (tighter on the heavier side) interacts non-trivially
with the recenter loop and FIFO PnL accounting; that's a v2 concern.

================================================================
DESIGN
================================================================
Pure-function math (`compute_liquidity_state`) is separated from the
exchange call so unit tests can feed it canned OrderBook objects with
no I/O. The class wrapper is a thin shell that fetches and forwards.

This signal is INSTANCE PER PROCESS (not per symbol). The runner makes
one and passes the same instance to all engines. State is per-call —
nothing accumulates across compute() calls.

This signal does NOT extend the Signal ABC. That contract returns
SignalResult+zones, which is the wrong shape for liquidity output.
The engine takes liquidity as a separate optional dependency.
"""

from dataclasses import dataclass
from typing import Optional

from src.exchange.base import ExchangeBase, OrderBook
from src.utils.logger import get_logger

logger = get_logger("liquidity")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidityConfig:
    """
    All knobs the operator might want to tune. Per-symbol granularity is
    deliberately omitted — both BTCUSDT and ETHUSDT trade in similar
    spread regimes on Binance; if that changes we can split into a dict.
    """
    # Hard pause threshold. Above this spread the market is broken (halt,
    # one-sided book, exchange degradation) and placing limit orders is
    # likely to result in stuck inventory. Default of 50 bps = 0.5%.
    pause_spread_bps: float = 50.0

    # Spacing must be at least this multiple of spread, otherwise round-
    # trip profit is consumed by the spread itself. Default 4× covers
    # 2× spread cost + 2× fees with margin.
    min_spacing_to_spread_ratio: float = 4.0

    # How many book levels to sum when computing depth imbalance. Too
    # shallow misses real depth; too deep weights stale far-from-mid
    # orders that won't trade. 20 matches the engine's default fetch.
    depth_levels: int = 20

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "LiquidityConfig":
        raw = raw or {}
        return cls(
            pause_spread_bps=float(raw.get("pause_spread_bps", 50.0)),
            min_spacing_to_spread_ratio=float(raw.get("min_spacing_to_spread_ratio", 4.0)),
            depth_levels=int(raw.get("depth_levels", 20)),
        )


# ---------------------------------------------------------------------
# State (output)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidityState:
    """
    What the engine consumes. Frozen because downstream code should never
    mutate a snapshot — re-compute if you need fresh values.
    """
    symbol: str
    spread_bps: float
    imbalance: float                 # [-1.0, +1.0], +ve = bid-heavy
    recommended_spacing_pct: float   # the value to feed into compute_grid
    should_pause: bool
    pause_reason: str = ""           # populated only if should_pause


# ---------------------------------------------------------------------
# Pure-function math (testable without an exchange)
# ---------------------------------------------------------------------


def _spread_bps(book: OrderBook) -> float:
    """
    Bid/ask spread in basis points relative to mid-price.

    1 bp = 0.01%. A 5 bp spread on $75k BTC = $3.75. Returns 0 if the
    book is empty or one-sided — caller must treat zero as "no signal,"
    not as "tight market." We never invent a value out of nothing.
    """
    if not book.bids or not book.asks:
        return 0.0
    mid = book.mid_price
    if mid <= 0:
        return 0.0
    return (book.spread / mid) * 10_000.0


def _depth_imbalance(book: OrderBook, depth_levels: int) -> float:
    """
    Depth imbalance over the top N levels of each side.

        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)

    Range [-1.0, +1.0]:
      +1 = all depth is on the bid side (extreme buying pressure)
      -1 = all depth on the ask side (extreme selling pressure)
       0 = perfectly balanced book

    Why depth_levels and not all levels? Top-of-book weights what's
    actually likely to trade. A million-dollar wall 5% away from mid
    has near-zero immediate effect on price, so summing it dilutes the
    signal. 20 levels covers ~the inside book on Binance for our pairs.
    """
    bid_depth = sum(qty for _, qty in book.bids[:depth_levels])
    ask_depth = sum(qty for _, qty in book.asks[:depth_levels])
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def compute_liquidity_state(
    book: OrderBook,
    config: LiquidityConfig,
    base_spacing_pct: float,
    min_spacing_pct: float,
    max_spacing_pct: float,
) -> LiquidityState:
    """
    Compute the LiquidityState from one book snapshot.

    The recommended spacing is the MAXIMUM of:
      (a) the operator's configured base spacing, and
      (b) min_spacing_to_spread_ratio × current spread (in pct, not bps),
    then clamped to [min_spacing_pct, max_spacing_pct].

    We never RECOMMEND less than base spacing — the operator chose that
    deliberately as their floor, the signal only widens when the market
    forces our hand.
    """
    spread_bps = _spread_bps(book)
    imbalance = _depth_imbalance(book, config.depth_levels)

    # Pause check first — if the market is broken nothing else matters.
    if spread_bps >= config.pause_spread_bps:
        return LiquidityState(
            symbol=book.symbol,
            spread_bps=spread_bps,
            imbalance=imbalance,
            recommended_spacing_pct=base_spacing_pct,
            should_pause=True,
            pause_reason=f"spread {spread_bps:.1f} bps >= {config.pause_spread_bps:.1f} bps",
        )

    # Spread expressed in pct (1 bp = 0.01%).
    spread_pct = spread_bps / 100.0
    # Spread-driven floor: we need spacing wide enough to clear spread + fees.
    spread_floor_pct = config.min_spacing_to_spread_ratio * spread_pct
    recommended = max(base_spacing_pct, spread_floor_pct)
    # Clamp into the operator's safety rails. min/max are SOFT guardrails
    # that override even an ugly market — at max we'd rather run a sparse
    # grid than refuse to trade.
    recommended = max(min_spacing_pct, min(max_spacing_pct, recommended))

    return LiquidityState(
        symbol=book.symbol,
        spread_bps=spread_bps,
        imbalance=imbalance,
        recommended_spacing_pct=recommended,
        should_pause=False,
    )


# ---------------------------------------------------------------------
# Class wrapper — thin shell that fetches and forwards
# ---------------------------------------------------------------------


class LiquiditySignal:
    """
    USAGE:
        signal = LiquiditySignal(exchange, LiquidityConfig.from_dict(...))
        state = await signal.compute(symbol, base_spacing_pct, min_pct, max_pct)
        if state.should_pause:
            ...
        spacing = state.recommended_spacing_pct

    The signal does NOT cache. The engine calls it once at init and
    again on each recenter, both relatively rare events. Caching adds
    risk (stale signal) for negligible gain.
    """

    def __init__(self, exchange: ExchangeBase, config: LiquidityConfig):
        self._exchange = exchange
        self._config = config

    async def compute(
        self,
        symbol: str,
        base_spacing_pct: float,
        min_spacing_pct: float,
        max_spacing_pct: float,
    ) -> LiquidityState:
        book = await self._exchange.get_order_book(symbol, depth=self._config.depth_levels)
        state = compute_liquidity_state(
            book=book,
            config=self._config,
            base_spacing_pct=base_spacing_pct,
            min_spacing_pct=min_spacing_pct,
            max_spacing_pct=max_spacing_pct,
        )
        logger.info("liquidity",
                    symbol=symbol,
                    spread_bps=round(state.spread_bps, 2),
                    imbalance=round(state.imbalance, 3),
                    recommended_spacing_pct=round(state.recommended_spacing_pct, 4),
                    should_pause=state.should_pause)
        return state

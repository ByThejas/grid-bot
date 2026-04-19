"""
Signal Module Contracts.

WHY a base module if there is only one signal so far?
- Defines the SHAPE of every signal (S&D, Liquidity, Sentiment) so the
  signal-fusion layer in Phase 6 can treat them uniformly.
- Forces every new signal to declare what it produces — Zone for
  price-localized signals, raw scores for global ones.
- Lets us wire a `Signal` registry later without refactoring callers.

WHAT IS A ZONE?
A region of price where order flow indicates one side dominated:
  - 'demand' zone = support (buyers absorbed selling here)
  - 'supply' zone = resistance (sellers absorbed buying here)
The grid engine consumes zones to:
  1. Place buy orders inside DEMAND zones (likely bounce points)
  2. Place sell orders inside SUPPLY zones (likely rejection points)
  3. AVOID placing orders that would be hit through a strong opposing zone
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ZoneType(str, Enum):
    DEMAND = "demand"   # Support — buyers dominated, expect bounces up
    SUPPLY = "supply"   # Resistance — sellers dominated, expect rejections down


class ZoneSource(str, Enum):
    """Where a zone came from. Useful for debugging and per-source weighting."""
    FOOTPRINT = "footprint"          # Trade-flow imbalance per price bucket
    ABSORPTION = "absorption"        # Persistent large resting orders
    REACTION_HISTORY = "reaction_history"  # Phase 4.5 — historical bounces


@dataclass(frozen=True)
class Zone:
    """
    A price region where the order book or trade flow signals support
    or resistance.

    WHY frozen=True?
    Zones are immutable snapshots — created by a signal at a moment in
    time and consumed read-only by the grid engine. Frozen catches any
    accidental mutation downstream (e.g. someone trying to "update" a
    zone's strength would crash loudly instead of silently lying).

    WHY price_low / price_high instead of a single price?
    Real zones are bands, not lines. A footprint bucket spans $50 of
    BTC; an absorption wall spans the bid/ask tick. The grid engine
    decides whether to place an order based on overlap with the band,
    not exact equality.
    """
    symbol: str
    price_low: float
    price_high: float
    zone_type: ZoneType
    strength: float                  # [0.0, 1.0] — confidence of the zone
    source: ZoneSource
    timestamp_ms: int                # When the zone was computed
    metadata: dict = field(default_factory=dict)  # Source-specific extras

    def contains(self, price: float) -> bool:
        return self.price_low <= price <= self.price_high

    def midpoint(self) -> float:
        return (self.price_low + self.price_high) / 2

    def width(self) -> float:
        return self.price_high - self.price_low


@dataclass
class SignalResult:
    """
    Bundled output of one signal computation.

    Holds zones plus metadata. Liquidity and Sentiment will return
    additional fields (spread_bps, sentiment_score) — extend this
    dataclass when those signals come online.
    """
    symbol: str
    timestamp_ms: int
    zones: list[Zone]
    metadata: dict = field(default_factory=dict)

    def demand_zones(self) -> list[Zone]:
        return [z for z in self.zones if z.zone_type == ZoneType.DEMAND]

    def supply_zones(self) -> list[Zone]:
        return [z for z in self.zones if z.zone_type == ZoneType.SUPPLY]

    def near(self, price: float, max_distance_pct: float) -> list[Zone]:
        """Zones within max_distance_pct of `price`. For grid placement."""
        out = []
        for z in self.zones:
            mid = z.midpoint()
            if mid <= 0:
                continue
            distance_pct = abs(mid - price) / price * 100
            if distance_pct <= max_distance_pct:
                out.append(z)
        return out


class Signal(ABC):
    """
    Abstract contract every signal module implements.

    Signals are deliberately stateless across calls — each compute() pulls
    fresh data from storage and returns a snapshot. State that needs to
    persist (e.g., rolling averages) lives inside the implementation as
    instance fields, but call semantics stay simple.
    """

    @abstractmethod
    async def compute(self, symbol: str) -> SignalResult:
        """
        Compute the signal for `symbol` at "now".

        Implementations should:
          - Read recent data from MarketDataStore.
          - Apply their algorithm.
          - Return a SignalResult timestamped to wall clock.
        """
        ...

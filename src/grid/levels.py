"""
Grid Level Computation — pure functions, no IO.

THE JOB
Given a current price and config, decide WHERE to place each limit order.
This module does NOT talk to the exchange; it just returns a list of
price/side/size triples. Phase 5C (engine) is what actually places them.

WHY a separate module?
- Testability. Grid placement is the most consequential piece of logic in
  the bot. It must be unit-testable in isolation — no mocking the exchange,
  no sleeps, no asyncio. A function you can call with four arguments and
  assert on the return value.
- Replacability. The placement policy will evolve (zone-weighted sizing,
  volatility-adaptive spacing, etc). Keeping it in one file means the
  engine's loop code does not churn when the policy changes.

=====================================================================
ALGORITHM v1
=====================================================================
Step 1 — BASE GRID
    buys:  center - k * spacing  for k = 1..levels_below
    sells: center + k * spacing  for k = 1..levels_above
    where spacing = center * base_spacing_pct / 100  (linear, not geometric)

    WHY linear: intraday moves are typically < 2 %. Over that range the
    difference vs geometric spacing is a rounding error. Linear is easier
    to reason about (fill distance is constant in $ terms).

Step 2 — ZONE SNAPPING (only if zones are provided)
    For each base level, look for a SAME-SIDE zone (demand for buys,
    supply for sells) whose midpoint is within ±half-spacing. If one
    exists, move the level to the zone's midpoint. Two base levels that
    snap to the same zone collapse to one — stacking size there is a v2
    optimization, not today.

Step 3 — OPPOSING-ZONE AVOIDANCE
    If a level's price falls INSIDE an opposing zone (buy inside a
    supply/resistance zone, sell inside a demand/support zone), drop it.
    We refuse to fight a wall on the wrong side.

NOTE on min/max_spacing_pct (config safety rails):
    These bounds exist in settings.yaml but are NOT applied here. They
    constrain the SPACING PARAMETER itself — meaningful once a later phase
    lets the Liquidity signal tighten or widen spacing dynamically. In v1
    spacing is a fixed config value, so the bounds do nothing yet. Applying
    them per-level would wrongly reject snaps that are closer to center
    than base levels we already accept (base k=2 sits at 2×spacing from
    center, which can exceed max_spacing_pct even when spacing is legal).
"""

from dataclasses import dataclass, field
from typing import Optional

from src.signals.base import Zone, ZoneType


@dataclass(frozen=True)
class GridLevel:
    """
    A single intended limit order.

    WHY frozen=True?
    Once the engine receives a level, downstream code should never mutate
    it — if the policy wants to re-compute, it should return a new list.
    Prevents a class of bugs where the engine state drifts from what the
    policy emitted.
    """
    price: float
    side: str           # "BUY" or "SELL"
    size: float         # base asset quantity
    reason: str         # short provenance tag — "base", "zone-shifted(footprint)", etc.


@dataclass
class GridParams:
    """
    All the inputs the placement policy needs for one compute_grid call.

    This is a data object not because it does anything clever, but because
    compute_grid() would otherwise have nine positional arguments and be
    a nightmare to call. Every new policy parameter lands here.
    """
    symbol: str
    center_price: float
    base_spacing_pct: float
    min_spacing_pct: float
    max_spacing_pct: float
    levels_above: int
    levels_below: int
    order_size: float                     # base asset per level
    zones: Optional[list[Zone]] = None    # optional S&D zones


def compute_grid(params: GridParams) -> list[GridLevel]:
    """
    Return the list of grid levels for the given parameters.

    The returned list is NOT sorted by price — it preserves logical order
    (buys first going down from center, then sells going up). Callers that
    need price order can sort cheaply.
    """
    spacing = params.center_price * params.base_spacing_pct / 100.0

    base_levels: list[GridLevel] = []
    for k in range(1, params.levels_below + 1):
        base_levels.append(GridLevel(
            price=params.center_price - k * spacing,
            side="BUY",
            size=params.order_size,
            reason="base",
        ))
    for k in range(1, params.levels_above + 1):
        base_levels.append(GridLevel(
            price=params.center_price + k * spacing,
            side="SELL",
            size=params.order_size,
            reason="base",
        ))

    if not params.zones:
        return base_levels

    demand_zones = [z for z in params.zones if z.zone_type == ZoneType.DEMAND]
    supply_zones = [z for z in params.zones if z.zone_type == ZoneType.SUPPLY]

    adjusted: list[GridLevel] = []
    # Track (side, price) pairs already committed, so two base levels that
    # snap to the same zone midpoint don't produce duplicate orders.
    seen: set[tuple[str, float]] = set()

    for lvl in base_levels:
        if lvl.side == "BUY":
            same_side = demand_zones
            opposing = supply_zones
        else:
            same_side = supply_zones
            opposing = demand_zones

        # Step 3 — opposing-zone avoidance.
        # Skip any level whose price sits inside a zone we'd be fighting.
        if any(z.contains(lvl.price) for z in opposing):
            continue

        # Step 2 — snap to same-side zone if one is nearby.
        half_spacing = spacing / 2.0
        near = [
            z for z in same_side
            if abs(z.midpoint() - lvl.price) <= half_spacing
        ]
        if near:
            zone = min(near, key=lambda z: abs(z.midpoint() - lvl.price))
            final = GridLevel(
                price=zone.midpoint(),
                side=lvl.side,
                size=lvl.size,
                reason=f"zone-shifted({zone.source.value})",
            )
        else:
            final = lvl

        key = (final.side, round(final.price, 8))
        if key in seen:
            # Duplicate after snapping — drop. v2 can stack size here.
            continue
        seen.add(key)
        adjusted.append(final)

    return adjusted

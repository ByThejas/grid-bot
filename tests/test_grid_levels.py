"""
Unit tests for compute_grid — the grid placement policy.

These are PURE tests: no DB, no exchange, no async. Just construct
GridParams, call compute_grid(), assert on the returned levels.
Speed and determinism matter here because placement is the most
consequential piece of logic — every bug shows up as mis-placed money.
"""

import sys
import asyncio

import pytest

from src.grid.levels import GridLevel, GridParams, compute_grid
from src.signals.base import Zone, ZoneSource, ZoneType


SYMBOL = "BTCUSDT"


def _base_params(**overrides) -> GridParams:
    """Sensible defaults matching settings.yaml shape."""
    defaults = dict(
        symbol=SYMBOL,
        center_price=75_000.0,
        base_spacing_pct=0.3,       # 0.3% → $225 spacing at $75k
        min_spacing_pct=0.15,
        max_spacing_pct=1.0,
        levels_above=5,
        levels_below=5,
        order_size=0.001,
        zones=None,
    )
    defaults.update(overrides)
    return GridParams(**defaults)


def _zone(
    low: float, high: float, zt: ZoneType,
    source: ZoneSource = ZoneSource.FOOTPRINT, strength: float = 0.8,
) -> Zone:
    return Zone(
        symbol=SYMBOL,
        price_low=low,
        price_high=high,
        zone_type=zt,
        strength=strength,
        source=source,
        timestamp_ms=0,
        metadata={},
    )


# ---------------------------------------------------------------------
# Base grid (no zones)
# ---------------------------------------------------------------------

def test_base_grid_has_correct_count_of_each_side():
    params = _base_params(levels_above=3, levels_below=4)
    grid = compute_grid(params)

    buys = [g for g in grid if g.side == "BUY"]
    sells = [g for g in grid if g.side == "SELL"]
    assert len(buys) == 4
    assert len(sells) == 3


def test_base_grid_is_evenly_spaced_around_center():
    params = _base_params(levels_above=2, levels_below=2)
    grid = compute_grid(params)
    spacing = 75_000.0 * 0.003   # 0.3% → $225

    prices_by_side = {
        "BUY": sorted([g.price for g in grid if g.side == "BUY"], reverse=True),
        "SELL": sorted([g.price for g in grid if g.side == "SELL"]),
    }
    # Expected: buys at center-225, center-450; sells at center+225, center+450
    assert prices_by_side["BUY"] == pytest.approx([75_000 - spacing, 75_000 - 2 * spacing])
    assert prices_by_side["SELL"] == pytest.approx([75_000 + spacing, 75_000 + 2 * spacing])


def test_base_grid_uses_configured_order_size():
    params = _base_params(order_size=0.0042)
    grid = compute_grid(params)
    assert all(g.size == 0.0042 for g in grid)


def test_base_grid_levels_are_tagged_as_base():
    params = _base_params()
    grid = compute_grid(params)
    assert all(g.reason == "base" for g in grid)


# ---------------------------------------------------------------------
# Zone snapping — same-side
# ---------------------------------------------------------------------

def test_buy_level_snaps_to_nearby_demand_zone():
    """A demand zone near a base buy level pulls that order to the zone."""
    # Base spacing = $225. First BUY sits at $74,775.
    # Demand zone 74,700–74,760 has mid 74,730, which is within half-spacing
    # ($112.5) of 74,775 — so the level should snap to 74,730.
    zone = _zone(74_700, 74_760, ZoneType.DEMAND)
    params = _base_params(levels_below=1, levels_above=0, zones=[zone])
    grid = compute_grid(params)

    assert len(grid) == 1
    g = grid[0]
    assert g.side == "BUY"
    assert g.price == pytest.approx(74_730)
    assert "zone-shifted" in g.reason


def test_sell_level_snaps_to_nearby_supply_zone():
    zone = _zone(75_240, 75_300, ZoneType.SUPPLY)  # mid = 75,270
    params = _base_params(levels_below=0, levels_above=1, zones=[zone])
    grid = compute_grid(params)

    assert len(grid) == 1
    g = grid[0]
    assert g.side == "SELL"
    assert g.price == pytest.approx(75_270)
    assert "zone-shifted" in g.reason


def test_level_does_not_snap_to_distant_zone():
    """A zone that is >half-spacing away should not pull the level."""
    # First BUY at 74,775. Demand zone at 74,000–74,050 is ~$750 away,
    # well outside the $112 half-spacing. Must stay at base.
    zone = _zone(74_000, 74_050, ZoneType.DEMAND)
    params = _base_params(levels_below=1, levels_above=0, zones=[zone])
    grid = compute_grid(params)

    assert grid[0].price == pytest.approx(74_775)
    assert grid[0].reason == "base"


# ---------------------------------------------------------------------
# Zone avoidance — opposing side
# ---------------------------------------------------------------------

def test_buy_level_inside_supply_zone_is_dropped():
    """Never place a buy inside a wall of sellers."""
    # Base BUY at 74,775. Put a supply zone straddling it.
    zone = _zone(74_750, 74_800, ZoneType.SUPPLY)
    params = _base_params(levels_below=1, levels_above=0, zones=[zone])
    grid = compute_grid(params)

    assert grid == []


def test_sell_level_inside_demand_zone_is_dropped():
    zone = _zone(75_200, 75_250, ZoneType.DEMAND)
    params = _base_params(levels_below=0, levels_above=1, zones=[zone])
    grid = compute_grid(params)

    assert grid == []


# ---------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------

def test_two_base_levels_snapping_to_same_zone_collapse_to_one():
    """Dedup: if snapping would produce two identical orders, keep one."""
    # Two base buys at 74,775 and 74,550. Put a wide demand zone whose
    # midpoint sits between them so both would snap to the same price.
    zone = _zone(74_650, 74_700, ZoneType.DEMAND)  # mid = 74,675
    # Half-spacing is $112.5. 74,775 - 74,675 = 100 (near). 74,550 - 74,675 = -125 (just outside).
    # So only the first buy snaps here. Build a tighter case:
    # spacing_pct 1.0 → spacing $750, half $375. Bases at 74,250 and 73,500.
    # Zone mid 73,875 is within $375 of both.
    zone2 = _zone(73_850, 73_900, ZoneType.DEMAND)  # mid = 73,875
    params = _base_params(
        levels_below=2, levels_above=0,
        base_spacing_pct=1.0, zones=[zone2],
    )
    grid = compute_grid(params)

    # Both base levels would want to snap to 73,875 → only one should survive.
    assert len(grid) == 1
    assert grid[0].price == pytest.approx(73_875)


def test_no_zones_returns_pure_base_grid():
    params = _base_params(zones=None)
    grid = compute_grid(params)
    assert len(grid) == 10
    assert all(g.reason == "base" for g in grid)


def test_empty_zone_list_returns_pure_base_grid():
    params = _base_params(zones=[])
    grid = compute_grid(params)
    assert len(grid) == 10
    assert all(g.reason == "base" for g in grid)


# ---------------------------------------------------------------------
# Script-mode runner — matches the pattern from test_supply_demand.py
# so `python -m tests.test_grid_levels` works without pytest installed.
# ---------------------------------------------------------------------

async def _run_all_as_script() -> int:
    tests = [
        test_base_grid_has_correct_count_of_each_side,
        test_base_grid_is_evenly_spaced_around_center,
        test_base_grid_uses_configured_order_size,
        test_base_grid_levels_are_tagged_as_base,
        test_buy_level_snaps_to_nearby_demand_zone,
        test_sell_level_snaps_to_nearby_supply_zone,
        test_level_does_not_snap_to_distant_zone,
        test_buy_level_inside_supply_zone_is_dropped,
        test_sell_level_inside_demand_zone_is_dropped,
        test_two_base_levels_snapping_to_same_zone_collapse_to_one,
        test_no_zones_returns_pure_base_grid,
        test_empty_zone_list_returns_pure_base_grid,
    ]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print("-" * 40)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all_as_script()))

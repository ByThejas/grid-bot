"""
Deterministic unit tests for SupplyDemandSignal.

WHY synthetic data instead of hitting the real DB?
- Tests must be repeatable. Real market data shifts — a test that passes
  once against the live DB may fail next minute.
- We control exactly what the algorithm sees, so we can assert specific
  zones appear (or don't).

Run with:
    python -m pytest tests/test_supply_demand.py -v

Or as a script (no pytest framework needed):
    python -m tests.test_supply_demand
"""

import asyncio
import pathlib
import sys
import tempfile

import pytest

from src.market_data.storage import MarketDataStore
from src.signals.base import ZoneSource, ZoneType
from src.signals.supply_demand import (
    AbsorptionConfig,
    FootprintConfig,
    SupplyDemandConfig,
    SupplyDemandSignal,
)


# ---------------------------------------------------------------------
# Shared fixture-ish helpers (usable from both pytest and the script path).
# ---------------------------------------------------------------------

SYMBOL = "BTCUSDT"


def _base_config() -> SupplyDemandConfig:
    """Defaults sized for the synthetic data below."""
    return SupplyDemandConfig(
        footprint=FootprintConfig(
            window_minutes=60,  # generous — synthetic trades span minutes
            bucket_size={SYMBOL: 50.0},
            min_volume={SYMBOL: 0.5},
            imbalance_threshold=0.6,
        ),
        absorption=AbsorptionConfig(
            window_minutes=60,
            min_persistence_ratio=0.7,
            max_size_decay_pct=0.30,
            size_zscore_threshold=2.0,
            zone_pad={SYMBOL: 5.0},
        ),
    )


async def _fresh_store() -> tuple[MarketDataStore, str]:
    """Create a temp-file SQLite DB with schema applied."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = MarketDataStore(tmp.name)
    await store.connect()
    return store, tmp.name


def _seed_trades(
    price: float, buy_qty: float, sell_qty: float, base_ts_ms: int
) -> list[tuple]:
    """
    Build a list of (trade_id, price, qty, is_buyer_maker, timestamp)
    tuples so the caller can bulk-insert them.

    buy_qty goes in as BUY-aggressor trades (is_buyer_maker=False).
    sell_qty goes in as SELL-aggressor trades (is_buyer_maker=True).
    We split each total into 10 trades so the bucket has multiple hits.
    """
    rows = []
    trade_id = int(base_ts_ms)  # unique-enough base
    chunk_buy = buy_qty / 10.0 if buy_qty else 0.0
    chunk_sell = sell_qty / 10.0 if sell_qty else 0.0
    for i in range(10):
        if chunk_buy > 0:
            rows.append((trade_id, price, chunk_buy, False, base_ts_ms + i))
            trade_id += 1
        if chunk_sell > 0:
            rows.append((trade_id, price, chunk_sell, True, base_ts_ms + i + 100))
            trade_id += 1
    return rows


async def _insert_trades(store: MarketDataStore, rows: list[tuple]) -> None:
    for trade_id, price, qty, is_buyer_maker, ts in rows:
        await store.save_trade(
            symbol=SYMBOL,
            trade_id=trade_id,
            price=price,
            quantity=qty,
            is_buyer_maker=is_buyer_maker,
            timestamp=ts,
        )


async def _insert_snapshots(
    store: MarketDataStore,
    snapshots: list[dict],
) -> None:
    for snap in snapshots:
        await store.save_orderbook_snapshot(
            symbol=SYMBOL,
            timestamp=snap["timestamp"],
            bids=snap["bids"],
            asks=snap["asks"],
        )


# ---------------------------------------------------------------------
# Footprint tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_footprint_emits_demand_zone_when_buyers_dominate():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        # 90% buyers, 10% sellers at $75,000 → imbalance ≈ +0.8
        await _insert_trades(store, _seed_trades(75_000, buy_qty=9.0, sell_qty=1.0, base_ts_ms=now - 60_000))

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        demand = [z for z in result.zones
                  if z.zone_type == ZoneType.DEMAND and z.source == ZoneSource.FOOTPRINT]
        assert demand, f"expected a demand zone, got zones={result.zones}"
        z = demand[0]
        assert z.price_low <= 75_000 <= z.price_high
        assert z.strength >= 0.6
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_footprint_emits_supply_zone_when_sellers_dominate():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        # 10% buyers, 90% sellers at $76,000 → imbalance ≈ -0.8
        await _insert_trades(store, _seed_trades(76_000, buy_qty=1.0, sell_qty=9.0, base_ts_ms=now - 60_000))

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        supply = [z for z in result.zones
                  if z.zone_type == ZoneType.SUPPLY and z.source == ZoneSource.FOOTPRINT]
        assert supply, f"expected a supply zone, got zones={result.zones}"
        z = supply[0]
        assert z.price_low <= 76_000 <= z.price_high
        assert z.strength >= 0.6
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_footprint_ignores_balanced_flow():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        # 50/50 → imbalance ≈ 0, below the 0.6 threshold
        await _insert_trades(store, _seed_trades(75_500, buy_qty=5.0, sell_qty=5.0, base_ts_ms=now - 60_000))

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        footprint_zones = [z for z in result.zones if z.source == ZoneSource.FOOTPRINT]
        assert footprint_zones == [], f"expected no zones, got {footprint_zones}"
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_footprint_respects_min_volume():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        # 0.2 BTC total — below the 0.5 min_volume floor even though imbalanced
        await _insert_trades(store, _seed_trades(75_000, buy_qty=0.18, sell_qty=0.02, base_ts_ms=now - 60_000))

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        footprint_zones = [z for z in result.zones if z.source == ZoneSource.FOOTPRINT]
        assert footprint_zones == [], f"expected skip below min_volume, got {footprint_zones}"
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------
# Absorption tests
# ---------------------------------------------------------------------

def _synth_snapshots(
    base_ts_ms: int,
    n: int,
    wall_price: float,
    wall_qty: float,
    wall_side: str,              # 'bid' or 'ask'
    persistence_ratio: float,    # fraction of snapshots that include the wall
) -> list[dict]:
    """
    Generate n snapshots. Each has ~20 levels per side with small quantities,
    plus in `persistence_ratio` of them, a large wall at `wall_price` on
    the specified side.

    Small-level quantities are chosen so the wall is clearly an outlier.
    """
    snaps = []
    for i in range(n):
        # Small "noise" book: levels from 75000 down/up by $10 with qty 0.1-0.3
        bids = [[75_000 - k * 10, 0.1 + k * 0.01] for k in range(20)]
        asks = [[75_010 + k * 10, 0.1 + k * 0.01] for k in range(20)]

        if i < int(n * persistence_ratio):
            # Insert the wall into top-of-book on the specified side.
            # Real order books have UNIQUE prices per side — drop any noise
            # level that collides with the wall price before inserting.
            # Otherwise bid_history[wall_price] ends up with both qtys mixed
            # and the size-decay check (max vs min) kills the detection.
            if wall_side == "bid":
                bids = [b for b in bids if b[0] != wall_price]
                bids.insert(0, [wall_price, wall_qty])
                bids = bids[:20]
            else:
                asks = [a for a in asks if a[0] != wall_price]
                asks.insert(0, [wall_price, wall_qty])
                asks = asks[:20]

        snaps.append({
            "timestamp": base_ts_ms + i * 1000,
            "bids": bids,
            "asks": asks,
        })
    return snaps


@pytest.mark.asyncio
async def test_absorption_emits_demand_zone_for_persistent_bid_wall():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        base = now - 60 * 1000  # 60 snapshots back
        snaps = _synth_snapshots(
            base_ts_ms=base,
            n=60,
            wall_price=74_950,
            wall_qty=20.0,             # huge vs noise ~0.1-0.3
            wall_side="bid",
            persistence_ratio=1.0,     # present in every snapshot
        )
        await _insert_snapshots(store, snaps)

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        demand = [z for z in result.zones
                  if z.zone_type == ZoneType.DEMAND and z.source == ZoneSource.ABSORPTION]
        assert demand, f"expected a demand wall, got {result.zones}"
        z = demand[0]
        assert z.price_low <= 74_950 <= z.price_high
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_absorption_emits_supply_zone_for_persistent_ask_wall():
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        base = now - 60 * 1000
        snaps = _synth_snapshots(
            base_ts_ms=base,
            n=60,
            wall_price=75_050,
            wall_qty=20.0,
            wall_side="ask",
            persistence_ratio=1.0,
        )
        await _insert_snapshots(store, snaps)

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        supply = [z for z in result.zones
                  if z.zone_type == ZoneType.SUPPLY and z.source == ZoneSource.ABSORPTION]
        assert supply, f"expected a supply wall, got {result.zones}"
        z = supply[0]
        assert z.price_low <= 75_050 <= z.price_high
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_absorption_ignores_short_lived_wall():
    """A wall that only appears in 30% of snapshots is NOT persistent."""
    store, path = await _fresh_store()
    try:
        import time
        now = int(time.time() * 1000)
        base = now - 60 * 1000
        snaps = _synth_snapshots(
            base_ts_ms=base,
            n=60,
            wall_price=74_950,
            wall_qty=20.0,
            wall_side="bid",
            persistence_ratio=0.3,     # below the 0.7 threshold
        )
        await _insert_snapshots(store, snaps)

        signal = SupplyDemandSignal(store, _base_config())
        result = await signal.compute(SYMBOL)

        absorption = [z for z in result.zones if z.source == ZoneSource.ABSORPTION]
        # Since the wall is not in the CURRENT (last) snapshot either
        # (30%  of 60 = first 18 snapshots only), there's nothing to flag anyway.
        assert absorption == [], f"expected no absorption zones, got {absorption}"
    finally:
        await store.close()
        pathlib.Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------
# Script-mode runner (so `python -m tests.test_supply_demand` works)
# ---------------------------------------------------------------------

async def _run_all_as_script() -> int:
    """Execute every test function; return nonzero exit code on any failure."""
    tests = [
        test_footprint_emits_demand_zone_when_buyers_dominate,
        test_footprint_emits_supply_zone_when_sellers_dominate,
        test_footprint_ignores_balanced_flow,
        test_footprint_respects_min_volume,
        test_absorption_emits_demand_zone_for_persistent_bid_wall,
        test_absorption_emits_supply_zone_for_persistent_ask_wall,
        test_absorption_ignores_short_lived_wall,
    ]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            await t()
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

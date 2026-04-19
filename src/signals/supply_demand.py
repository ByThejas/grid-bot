"""
Supply & Demand Signal.

Two complementary zone-detection methods, both derived purely from
microstructure data (order flow + book depth). No classical TA.

=====================================================================
METHOD 1: FOOTPRINT IMBALANCE
=====================================================================
For each trade, the AGGRESSOR is the market-order side that crossed
the spread. On Binance's trade feed:
    is_buyer_maker = True   → seller was aggressor (hit the bid)
    is_buyer_maker = False  → buyer was aggressor (lifted the ask)

Bucket trades by price (e.g. $50 bands for BTC). Within each bucket:
    imbalance = (buy_aggressor_volume - sell_aggressor_volume) / total

Range [-1, +1]. Strong positive → buyers absorbed sellers here
(demand zone → support). Strong negative → sellers absorbed buyers
(supply zone → resistance).

WHY THIS WORKS
Classic price action says "support/resistance from historical lows/highs".
Footprint imbalance says "support/resistance from WHERE ACTUAL SIZE
TRADED AT WHAT AGGRESSION". It's cleaner — a level that attracted
heavy buy aggression is a zone where real money defended. No
interpretation needed, just arithmetic over the trade tape.

=====================================================================
METHOD 2: ABSORPTION WALLS
=====================================================================
Look at the last ~5 minutes of order book snapshots. A PERSISTENT
large resting order at a specific price that doesn't get consumed
while trades bounce nearby is a wall — someone is absorbing pressure.

For each price level in the CURRENT book:
    persistence   = fraction of recent snapshots that contained this level
    size_zscore   = (level size − mean depth) / std(depth)
    size_stability = max_size / (max_size - min_size)    # how steady

A level qualifies as a wall if persistence is high AND the level is
anomalously large (zscore) AND size has NOT decayed much (stable).

Bid walls → demand zones. Ask walls → supply zones.

=====================================================================
OUTPUT
=====================================================================
Both methods emit Zone objects via the shared Signal contract. The
grid engine (Phase 5) will consume these to bias its buy/sell
placement — put buys inside demand zones, sells inside supply zones,
avoid fighting a strong wall on the wrong side.
"""

import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from src.market_data.storage import MarketDataStore
from src.signals.base import Signal, SignalResult, Zone, ZoneSource, ZoneType
from src.utils.logger import get_logger

logger = get_logger("supply_demand")


# ---------------------------------------------------------------------
# Configuration dataclasses.
#
# WHY dataclasses instead of dicts?
# - Typed access: config.footprint.bucket_size vs config["footprint"]["bucket_size"]
# - Autocomplete in editors.
# - Validation in __post_init__ if we want to add it later.
# - Still trivially loaded from settings.yaml (see from_dict factory).
# ---------------------------------------------------------------------

@dataclass
class FootprintConfig:
    window_minutes: int = 15
    bucket_size: dict = field(default_factory=dict)     # symbol → $ width
    min_volume: dict = field(default_factory=dict)      # symbol → base-asset qty
    imbalance_threshold: float = 0.6                    # |imbalance| ≥ this


@dataclass
class AbsorptionConfig:
    window_minutes: int = 5
    min_persistence_ratio: float = 0.7                  # level present in ≥70% of snapshots
    max_size_decay_pct: float = 0.30                    # wall may shrink at most 30%
    size_zscore_threshold: float = 2.0                  # wall ≥ 2σ above mean depth
    zone_pad: dict = field(default_factory=dict)        # symbol → ± $ around exact price


@dataclass
class SupplyDemandConfig:
    footprint: FootprintConfig = field(default_factory=FootprintConfig)
    absorption: AbsorptionConfig = field(default_factory=AbsorptionConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "SupplyDemandConfig":
        """Build from the raw dict shape used in settings.yaml."""
        fp_raw = d.get("footprint", {}) or {}
        ab_raw = d.get("absorption", {}) or {}
        return cls(
            footprint=FootprintConfig(
                window_minutes=fp_raw.get("window_minutes", 15),
                bucket_size=fp_raw.get("bucket_size", {}),
                min_volume=fp_raw.get("min_volume", {}),
                imbalance_threshold=fp_raw.get("imbalance_threshold", 0.6),
            ),
            absorption=AbsorptionConfig(
                window_minutes=ab_raw.get("window_minutes", 5),
                min_persistence_ratio=ab_raw.get("min_persistence_ratio", 0.7),
                max_size_decay_pct=ab_raw.get("max_size_decay_pct", 0.30),
                size_zscore_threshold=ab_raw.get("size_zscore_threshold", 2.0),
                zone_pad=ab_raw.get("zone_pad", {}),
            ),
        )


# ---------------------------------------------------------------------
# The signal itself.
# ---------------------------------------------------------------------

class SupplyDemandSignal(Signal):
    """
    Computes supply & demand zones for a symbol by reading recent
    trades and order book snapshots from MarketDataStore.
    """

    def __init__(self, store: MarketDataStore, config: SupplyDemandConfig):
        self._store = store
        self._config = config

    async def compute(
        self, symbol: str, reference_time_ms: Optional[int] = None
    ) -> SignalResult:
        """
        Compute zones as of `reference_time_ms` (defaults to wall-clock now).

        WHY the reference_time_ms parameter?
        Production always computes zones as-of "right now", but inspection
        and backtest scripts need to look at historical windows — e.g., run
        the signal against last night's data where "now" is 9 hours ago.
        Keeping this optional means callers that don't care can ignore it.
        """
        now_ms = reference_time_ms if reference_time_ms is not None else int(time.time() * 1000)

        fp_since = now_ms - self._config.footprint.window_minutes * 60_000
        ab_since = now_ms - self._config.absorption.window_minutes * 60_000

        trades = await self._store.get_trades(symbol, fp_since)
        snapshots = await self._store.get_snapshots(symbol, ab_since)
        current_book = await self._store.get_latest_snapshot(symbol)

        zones: list[Zone] = []
        if trades:
            zones.extend(self._detect_footprint_zones(trades, symbol, now_ms))
        if current_book and len(snapshots) >= 10:
            # Need at least ~10 snapshots for absorption stats to be meaningful.
            # Below that, any level looks "persistent" because there is no
            # history to refute it.
            zones.extend(self._detect_absorption_zones(
                snapshots, current_book, symbol, now_ms
            ))

        logger.debug(
            "zones computed",
            symbol=symbol,
            trade_count=len(trades),
            snapshot_count=len(snapshots),
            zones=len(zones),
        )

        return SignalResult(
            symbol=symbol,
            timestamp_ms=now_ms,
            zones=zones,
            metadata={
                "trade_count": len(trades),
                "snapshot_count": len(snapshots),
                "footprint_window_min": self._config.footprint.window_minutes,
                "absorption_window_min": self._config.absorption.window_minutes,
            },
        )

    # -----------------------------------------------------------------
    # Footprint imbalance
    # -----------------------------------------------------------------

    def _detect_footprint_zones(
        self, trades: list[dict], symbol: str, now_ms: int
    ) -> list[Zone]:
        cfg = self._config.footprint
        bucket_size = cfg.bucket_size.get(symbol)
        min_vol = cfg.min_volume.get(symbol, 0.0)

        if not bucket_size or bucket_size <= 0:
            logger.warning("no bucket_size for symbol; skipping footprint",
                           symbol=symbol)
            return []

        # price-center → [buy_vol, sell_vol]
        buckets: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for t in trades:
            # Round to bucket center. Half-bucket padding means a trade at
            # exactly a bucket boundary biases to the lower bucket — fine.
            center = round(t["price"] / bucket_size) * bucket_size
            if t["is_buyer_maker"]:
                # Seller was the aggressor (maker = buyer was resting)
                buckets[center][1] += t["quantity"]
            else:
                buckets[center][0] += t["quantity"]

        zones: list[Zone] = []
        for center, (buy_vol, sell_vol) in buckets.items():
            total = buy_vol + sell_vol
            if total < min_vol:
                continue
            imbalance = (buy_vol - sell_vol) / total
            if abs(imbalance) < cfg.imbalance_threshold:
                continue

            zone_type = ZoneType.DEMAND if imbalance > 0 else ZoneType.SUPPLY
            half = bucket_size / 2
            zones.append(Zone(
                symbol=symbol,
                price_low=center - half,
                price_high=center + half,
                zone_type=zone_type,
                strength=min(1.0, abs(imbalance)),
                source=ZoneSource.FOOTPRINT,
                timestamp_ms=now_ms,
                metadata={
                    "bucket_center": center,
                    "buy_volume": buy_vol,
                    "sell_volume": sell_vol,
                    "total_volume": total,
                    "imbalance": imbalance,
                },
            ))

        return zones

    # -----------------------------------------------------------------
    # Absorption walls
    # -----------------------------------------------------------------

    def _detect_absorption_zones(
        self,
        snapshots: list[dict],
        current_book: dict,
        symbol: str,
        now_ms: int,
    ) -> list[Zone]:
        cfg = self._config.absorption
        pad = cfg.zone_pad.get(symbol, 0.0)
        n_snaps = len(snapshots)

        # Aggregate: for each price, list of sizes observed (only when
        # that price was present in the snapshot's top levels).
        bid_history: dict[float, list[float]] = defaultdict(list)
        ask_history: dict[float, list[float]] = defaultdict(list)
        all_qtys: list[float] = []

        for snap in snapshots:
            for price, qty in snap["bids"]:
                bid_history[price].append(qty)
                all_qtys.append(qty)
            for price, qty in snap["asks"]:
                ask_history[price].append(qty)
                all_qtys.append(qty)

        if not all_qtys:
            return []

        mean_qty = statistics.fmean(all_qtys)
        std_qty = statistics.pstdev(all_qtys) if len(all_qtys) > 1 else 0.0
        if std_qty <= 0:
            # All depths identical — cannot compute z-score meaningfully.
            return []

        zones: list[Zone] = []

        zones.extend(self._check_side_for_walls(
            current_book["bids"], bid_history, n_snaps,
            mean_qty, std_qty, ZoneType.DEMAND, symbol, now_ms, pad, cfg,
        ))
        zones.extend(self._check_side_for_walls(
            current_book["asks"], ask_history, n_snaps,
            mean_qty, std_qty, ZoneType.SUPPLY, symbol, now_ms, pad, cfg,
        ))

        return zones

    def _check_side_for_walls(
        self,
        current_levels: list,
        history: dict,
        n_snaps: int,
        mean_qty: float,
        std_qty: float,
        zone_type: ZoneType,
        symbol: str,
        now_ms: int,
        pad: float,
        cfg: AbsorptionConfig,
    ) -> list[Zone]:
        zones = []
        for price, qty in current_levels:
            sizes = history.get(price, [])
            persistence = len(sizes) / n_snaps if n_snaps else 0
            if persistence < cfg.min_persistence_ratio:
                continue

            zscore = (qty - mean_qty) / std_qty
            if zscore < cfg.size_zscore_threshold:
                continue

            max_size = max(sizes)
            min_size = min(sizes)
            if max_size <= 0:
                continue
            decay = (max_size - min_size) / max_size
            if decay > cfg.max_size_decay_pct:
                continue

            # Strength: persistence × normalized z-score, capped at 1.0.
            # Divisor of 5 means z=5 maps to persistence×1; good z=2 and
            # persistence=0.8 → strength 0.32 (moderate).
            strength = min(1.0, persistence * (zscore / 5.0))

            zones.append(Zone(
                symbol=symbol,
                price_low=price - pad,
                price_high=price + pad,
                zone_type=zone_type,
                strength=strength,
                source=ZoneSource.ABSORPTION,
                timestamp_ms=now_ms,
                metadata={
                    "price": price,
                    "current_qty": qty,
                    "persistence": persistence,
                    "size_zscore": zscore,
                    "size_decay_pct": decay,
                    "max_observed_size": max_size,
                },
            ))
        return zones

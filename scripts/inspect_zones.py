"""
Zone Inspector — run the Supply & Demand signal against the real DB
and pretty-print the zones it detects.

WHY a dedicated script?
Unit tests prove the algorithm behaves correctly on synthetic data.
This script proves the algorithm produces SENSIBLE output on data that
actually came off Binance. Two very different things — hand-crafted
tests cannot catch "the window is too short for mainnet liquidity" or
"zone_pad is too small for real BTC tick spacing".

USAGE:
    python -m scripts.inspect_zones
    # or
    python scripts/inspect_zones.py

Reads:
    config/settings.yaml      — S&D parameters + the symbol list
    data/marketdata.db        — the DB the ingestor_runner writes to

Writes:
    stdout only.  Nothing touches disk.
"""

import asyncio
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import yaml

# Let the script be runnable from repo root as either a module or a plain file.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.market_data.storage import MarketDataStore
from src.signals.base import Zone, ZoneType
from src.signals.supply_demand import SupplyDemandConfig, SupplyDemandSignal


CONFIG_PATH = REPO_ROOT / "config" / "settings.yaml"
DB_PATH = REPO_ROOT / "data" / "marketdata.db"


def _load_config() -> tuple[list[str], SupplyDemandConfig]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    symbols = [p["symbol"] for p in raw["trading"]["pairs"]]
    sd_raw = raw.get("signals", {}).get("supply_demand", {}) or {}
    cfg = SupplyDemandConfig.from_dict(sd_raw)
    return symbols, cfg


def _fmt_price(p: float) -> str:
    # BTC ~ 5 digits, ETH ~ 4. Use 2 decimals always — matches how
    # Binance prints prices on the web UI.
    return f"{p:,.2f}"


def _fmt_zone(z: Zone, current_price: float) -> str:
    distance_pct = (z.midpoint() - current_price) / current_price * 100
    arrow = "↑" if distance_pct > 0 else "↓"
    return (
        f"    [{z.source.value:10}] "
        f"{_fmt_price(z.price_low):>12} – {_fmt_price(z.price_high):<12}  "
        f"mid={_fmt_price(z.midpoint()):>10}  "
        f"{arrow}{abs(distance_pct):5.2f}%  "
        f"strength={z.strength:.2f}"
    )


async def _footprint_diagnostic(
    store: MarketDataStore, cfg: SupplyDemandConfig, symbol: str, reference_ms: int
) -> None:
    """
    Print top footprint buckets by volume with their imbalance + the
    configured thresholds. Makes it obvious whether zones are missing
    because thresholds are too strict or because flow really is balanced.
    """
    fp = cfg.footprint
    since = reference_ms - fp.window_minutes * 60_000
    trades = await store.get_trades(symbol, since)
    if not trades:
        print("    (no trades in window — footprint diagnostic skipped)")
        return

    bucket_size = fp.bucket_size.get(symbol) or 0
    if bucket_size <= 0:
        print("    (no bucket_size configured for symbol)")
        return

    buckets: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        center = round(t["price"] / bucket_size) * bucket_size
        if t["is_buyer_maker"]:
            buckets[center][1] += t["quantity"]
        else:
            buckets[center][0] += t["quantity"]

    rows = []
    for center, (buy, sell) in buckets.items():
        total = buy + sell
        imbalance = (buy - sell) / total if total else 0.0
        rows.append((center, buy, sell, total, imbalance))
    rows.sort(key=lambda r: -r[3])   # by total volume, descending

    min_vol = fp.min_volume.get(symbol, 0.0)
    print(f"    thresholds: min_volume={min_vol}  |imbalance|≥{fp.imbalance_threshold}")
    print(f"    top 5 buckets by volume (of {len(rows)}):")
    print(f"      {'price':>12}  {'buy':>8}  {'sell':>8}  {'total':>8}  {'imb':>6}  verdict")
    for center, buy, sell, total, imb in rows[:5]:
        if total < min_vol:
            verdict = "vol<min"
        elif abs(imb) < fp.imbalance_threshold:
            verdict = "imb<thr"
        else:
            verdict = "→ ZONE"
        print(f"      {center:>12,.2f}  {buy:>8.3f}  {sell:>8.3f}  {total:>8.3f}  {imb:>+6.2f}  {verdict}")


async def _absorption_diagnostic(
    store: MarketDataStore, cfg: SupplyDemandConfig, symbol: str, reference_ms: int
) -> None:
    """
    Print top persistent levels in the current book by z-score, so we can
    see whether walls are being filtered by persistence, z-score, or decay.
    """
    ab = cfg.absorption
    since = reference_ms - ab.window_minutes * 60_000
    snapshots = await store.get_snapshots(symbol, since)
    latest = await store.get_latest_snapshot(symbol)
    if not snapshots or not latest:
        print("    (no snapshots in window)")
        return

    n_snaps = len(snapshots)
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
        print("    (no depth entries)")
        return
    mean_qty = statistics.fmean(all_qtys)
    std_qty = statistics.pstdev(all_qtys) if len(all_qtys) > 1 else 0.0

    print(f"    thresholds: persistence≥{ab.min_persistence_ratio}  "
          f"zscore≥{ab.size_zscore_threshold}  decay≤{ab.max_size_decay_pct}")
    print(f"    depth stats: n={len(all_qtys)}  mean={mean_qty:.3f}  std={std_qty:.3f}")

    def rows_for(side_name: str, current_levels, history):
        out = []
        for price, qty in current_levels:
            sizes = history.get(price, [])
            persistence = len(sizes) / n_snaps if n_snaps else 0.0
            zscore = (qty - mean_qty) / std_qty if std_qty > 0 else 0.0
            if sizes:
                mx, mn = max(sizes), min(sizes)
                decay = (mx - mn) / mx if mx > 0 else 1.0
            else:
                decay = 1.0
            if persistence < ab.min_persistence_ratio:
                verdict = "persist<thr"
            elif zscore < ab.size_zscore_threshold:
                verdict = "zscore<thr"
            elif decay > ab.max_size_decay_pct:
                verdict = "decay>max"
            else:
                verdict = "→ ZONE"
            out.append((side_name, price, qty, persistence, zscore, decay, verdict))
        return out

    rows = rows_for("bid", latest["bids"], bid_history) + \
           rows_for("ask", latest["asks"], ask_history)
    rows.sort(key=lambda r: -r[4])   # by z-score, descending

    print(f"    top 5 levels by z-score (of {len(rows)}):")
    print(f"      side  {'price':>12}  {'qty':>8}  persist  zscore  decay   verdict")
    for side, price, qty, persist, zs, dec, verdict in rows[:5]:
        print(f"      {side:<4}  {price:>12,.2f}  {qty:>8.3f}  "
              f"{persist:>6.2f}  {zs:>+6.2f}  {dec:>5.2f}   {verdict}")


async def _inspect_symbol(
    store: MarketDataStore, signal: SupplyDemandSignal, symbol: str,
    cfg: SupplyDemandConfig,
) -> None:
    latest = await store.get_latest_snapshot(symbol)
    if not latest:
        print(f"\n{symbol}: no snapshots in DB — run the ingestor first.")
        return

    current = latest["mid_price"]
    # Use the latest snapshot's timestamp as "now" so we can analyse data
    # collected hours ago. If the ingestor is live, this is effectively
    # wall-clock now anyway.
    reference_ms = latest["timestamp"]
    result = await signal.compute(symbol, reference_time_ms=reference_ms)

    import datetime as _dt
    age_s = max(0, (int(_dt.datetime.now().timestamp() * 1000) - reference_ms) // 1000)
    as_of = _dt.datetime.fromtimestamp(reference_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

    meta = result.metadata
    print(f"\n{'=' * 72}")
    print(f"{symbol}   current={_fmt_price(current)}   "
          f"as-of={as_of}  (data is {age_s}s old)")
    print(f"           trades={meta['trade_count']} (last {meta['footprint_window_min']}m)   "
          f"snapshots={meta['snapshot_count']} (last {meta['absorption_window_min']}m)")
    print("=" * 72)

    demand = sorted(result.demand_zones(), key=lambda z: -z.strength)
    supply = sorted(result.supply_zones(), key=lambda z: -z.strength)

    print(f"\n  DEMAND zones (support, buy-friendly): {len(demand)}")
    if not demand:
        print("    (none — either balanced flow or insufficient window)")
    for z in demand:
        print(_fmt_zone(z, current))

    print(f"\n  SUPPLY zones (resistance, sell-friendly): {len(supply)}")
    if not supply:
        print("    (none)")
    for z in supply:
        print(_fmt_zone(z, current))

    # Always show near-miss diagnostics so we can tell the difference
    # between "market was balanced" and "threshold is too strict".
    print("\n  footprint diagnostic:")
    await _footprint_diagnostic(store, cfg, symbol, reference_ms)
    print("\n  absorption diagnostic:")
    await _absorption_diagnostic(store, cfg, symbol, reference_ms)


async def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} does not exist.")
        print("Run `python -m src.market_data.ingestor_runner` for a while first.")
        sys.exit(1)

    symbols, cfg = _load_config()

    store = MarketDataStore(str(DB_PATH))
    await store.connect()
    try:
        signal = SupplyDemandSignal(store, cfg)
        for symbol in symbols:
            await _inspect_symbol(store, signal, symbol, cfg)
    finally:
        await store.close()

    print()


if __name__ == "__main__":
    asyncio.run(main())

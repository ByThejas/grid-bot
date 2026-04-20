"""
Backtest CLI — run the grid strategy against a price source and print a report.

USAGE (venv activated, from repo root):

    # Synthetic random walk, 10k ticks on BTCUSDT (smoke-test the pipeline):
    python -m scripts.backtest --mode synthetic --symbol BTCUSDT \
        --ticks 10000 --sigma 0.002 --start-price 75000 --seed 42

    # Historical replay from the ingestor DB (real trades):
    python -m scripts.backtest --mode db --symbol BTCUSDT --symbol ETHUSDT \
        --db data/marketdata.db

    # Show the report and save it to disk too:
    python -m scripts.backtest --mode synthetic --symbol BTCUSDT \
        --ticks 5000 --out backtest_report.txt

WHEN TO USE WHICH MODE
  * synthetic: verifies the engine/fills/replacements logic end-to-end
    without needing real data. Reproducible with --seed. Use this when
    tuning grid parameters on an idealised tape.
  * db: replays real trades captured by `src.market_data.ingestor_runner`.
    Use this for realistic PnL expectations — spacing, fill frequency,
    and drawdown depend on the actual intraday dynamics, not on a walk.
"""

import argparse
import asyncio
import sys

from src.backtest.runner import BacktestOptions, BacktestRunner
from src.backtest.sources import (
    DatabaseTradeSource,
    PriceSource,
    SyntheticRandomWalkSource,
)
from src.market_data.storage import MarketDataStore
from src.utils.logger import get_logger

logger = get_logger("backtest_cli")


async def _build_source(args: argparse.Namespace) -> tuple[PriceSource, list, callable]:
    """
    Returns (source, symbols, cleanup_callable).

    cleanup_callable is awaited at the end to close the MarketDataStore
    if we opened one. Synthetic mode returns a no-op.
    """
    symbols = list(args.symbol)
    if args.mode == "synthetic":
        if len(symbols) != 1:
            raise ValueError("--mode synthetic supports exactly one --symbol")
        source = SyntheticRandomWalkSource(
            symbol=symbols[0],
            start_price=args.start_price,
            n_ticks=args.ticks,
            dt_seconds=args.dt_seconds,
            drift=args.drift,
            sigma=args.sigma,
            seed=args.seed,
        )

        async def _noop():
            pass
        return source, symbols, _noop

    # DB mode.
    store = MarketDataStore(args.db)
    await store.connect()
    source = DatabaseTradeSource(
        store=store,
        symbols=symbols,
        since_ts_ms=args.since_ts_ms,
        limit=args.limit,
    )

    async def _close_store():
        await store.close()
    return source, symbols, _close_store


async def _run(args: argparse.Namespace) -> int:
    source, symbols, cleanup = await _build_source(args)
    try:
        opts = BacktestOptions(
            settings_path=args.settings,
            symbols=symbols,
            drift_check_every=args.drift_check_every,
            synthetic_spread_bps=args.synthetic_spread_bps,
            enable_risk=not args.disable_risk,
            enable_liquidity=not args.disable_liquidity,
            enable_sentiment=args.enable_sentiment,
        )
        runner = BacktestRunner(source=source, options=opts)
        report = await runner.run()
    finally:
        await cleanup()

    text = report.render()
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("report written", path=args.out)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Grid bot backtest runner")
    p.add_argument("--mode", choices=("synthetic", "db"), default="synthetic",
                   help="synthetic random walk, or replay from a MarketDataStore DB")
    p.add_argument("--symbol", action="append", default=[],
                   help="Trading symbol (can be given multiple times in db mode)")
    p.add_argument("--settings", default="config/settings.yaml")
    p.add_argument("--out", default=None, help="Also write rendered report here")
    p.add_argument("--drift-check-every", type=int, default=10,
                   help="How many ticks between manual drift checks")
    p.add_argument("--synthetic-spread-bps", type=float, default=1.0,
                   help="Spread of the exchange's synthesised order book")

    # Signal toggles.
    p.add_argument("--disable-risk", action="store_true",
                   help="Skip wiring the RiskManager — useful to isolate strategy PnL")
    p.add_argument("--disable-liquidity", action="store_true",
                   help="Skip wiring the LiquiditySignal")
    p.add_argument("--enable-sentiment", action="store_true",
                   help="Wire the SentimentSignal (off by default in backtest)")

    # Synthetic-mode knobs.
    p.add_argument("--ticks", type=int, default=5000)
    p.add_argument("--start-price", type=float, default=75_000.0)
    p.add_argument("--dt-seconds", type=float, default=1.0)
    p.add_argument("--drift", type=float, default=0.0,
                   help="Per-step drift (0.0001 = +0.01% per tick)")
    p.add_argument("--sigma", type=float, default=0.0005,
                   help="Per-step sigma of the log-price noise")
    p.add_argument("--seed", type=int, default=None)

    # DB-mode knobs.
    p.add_argument("--db", default="data/marketdata.db",
                   help="Path to the ingestor SQLite DB (db mode only)")
    p.add_argument("--since-ts-ms", type=int, default=0,
                   help="Replay only trades with timestamp >= this (ms epoch)")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap on total ticks replayed")

    args = p.parse_args()
    if not args.symbol:
        # Default to BTCUSDT if user didn't provide any — most common case.
        args.symbol = ["BTCUSDT"]

    try:
        rc = asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.info("backtest interrupted")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

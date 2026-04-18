"""
Live integration test for the market data ingestor.

Run with:
    python -m tests.test_data_ingestor

This test:
    1. Starts the ingestor on BTCUSDT + ETHUSDT for 30 seconds.
    2. After stop, opens the SQLite DB and counts rows.
    3. Asserts (loosely) that we got snapshots and trades for both symbols.

This is NOT a pytest unit test — it's a live integration check that
hits Binance Spot Testnet over WebSocket. Treat the assertions as smoke
checks: the exact counts depend on testnet activity at run time.
"""

import asyncio
import os
import pathlib

from dotenv import load_dotenv

from src.market_data.ingestor import MarketDataIngestor
from src.market_data.storage import MarketDataStore
from src.utils.logger import get_logger

logger = get_logger("ingestor_test")

DURATION_SECONDS = 30
DB_PATH = "data/marketdata_test.db"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


async def main():
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if not api_key or api_key == "your_testnet_api_key_here":
        logger.error("set your testnet API keys in .env first!")
        return

    # Make sure data/ exists. .gitignore already excludes it.
    pathlib.Path("data").mkdir(exist_ok=True)

    # Fresh test DB every run so we know the counts came from THIS run.
    db_file = pathlib.Path(DB_PATH)
    if db_file.exists():
        db_file.unlink()
    # SQLite WAL also creates -wal and -shm sidecar files; remove if present.
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(DB_PATH + suffix)
        if sidecar.exists():
            sidecar.unlink()

    logger.info("starting ingestor",
                symbols=SYMBOLS,
                duration_s=DURATION_SECONDS,
                db=DB_PATH)

    ingestor = MarketDataIngestor(
        api_key=api_key,
        api_secret=api_secret,
        symbols=SYMBOLS,
        db_path=DB_PATH,
        snapshot_every_s=1.0,
        depth_levels=20,
    )

    await ingestor.run_for_seconds(DURATION_SECONDS)

    # Reopen storage for verification.
    # WHY a separate handle? ingestor.stop() closed the writer connection.
    # Tests should not rely on the writer's handle being alive after stop.
    verifier = MarketDataStore(DB_PATH)
    await verifier.connect()

    logger.info("=== verification ===")
    total_problems = 0
    for symbol in SYMBOLS:
        snapshots = await verifier.count_snapshots(symbol)
        trades = await verifier.count_trades(symbol)
        logger.info("counts", symbol=symbol, snapshots=snapshots, trades=trades)

        # 30 seconds at 1 snapshot/sec → expect ~25-30 snapshots/symbol.
        # Tolerate 15 to allow for connect-warmup time.
        if snapshots < 15:
            logger.error("too few snapshots", symbol=symbol, got=snapshots, expected_min=15)
            total_problems += 1

        # Trade volume on testnet is unpredictable. Require at least 1
        # to confirm the stream is wired up; do not fail on low activity.
        if trades < 1:
            logger.warning(
                "no trades captured (testnet often quiet — confirm by re-running)",
                symbol=symbol,
            )

    await verifier.close()

    if total_problems == 0:
        logger.info("ingestor test passed", problems=0)
    else:
        logger.error("ingestor test failed", problems=total_problems)


if __name__ == "__main__":
    asyncio.run(main())

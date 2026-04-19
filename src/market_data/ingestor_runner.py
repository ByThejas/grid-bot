"""
Ingestor Runner — long-running CLI entrypoint for the market data ingestor.

USAGE (from a dedicated terminal, with venv activated):
    python -m src.market_data.ingestor_runner

It writes to data/marketdata.db (the production-style file) and runs
indefinitely until you press Ctrl+C. Use this to accumulate hours/days
of order book and trade data while you build downstream signal modules.

WHY a separate runner instead of just running the test in a loop?
- The test wipes the DB on every run (intentional for testing). The
  runner appends to the persistent DB.
- Clean Ctrl+C handling: we install a signal handler that flips the
  ingestor's stop_event so streams flush their buffers and close
  the DB cleanly. No SIGKILL means no half-written rows.
- Default config: same symbols and depth as production, sourced from
  settings.yaml so the runner stays in sync with what the strategy code
  will read.
"""

import asyncio
import os
import pathlib
import signal as os_signal

import yaml
from dotenv import load_dotenv

from src.market_data.ingestor import MarketDataIngestor
from src.utils.logger import get_logger

logger = get_logger("ingestor_runner")

DB_PATH = "data/marketdata.db"
SETTINGS_PATH = "config/settings.yaml"


def _load_symbols() -> list[str]:
    """Read trading pairs from settings.yaml so we stay in sync with strategy."""
    with open(SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    return [pair["symbol"] for pair in cfg["trading"]["pairs"]]


async def _run() -> None:
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    # NOTE: the ingestor connects to MAINNET WS for public data; keys are
    # passed for parity with the constructor signature but not used by the
    # public depth/trade streams.

    pathlib.Path("data").mkdir(exist_ok=True)
    symbols = _load_symbols()

    logger.info("starting persistent ingestor",
                symbols=symbols, db=DB_PATH, ctrl_c_to_stop=True)

    ingestor = MarketDataIngestor(
        api_key=api_key,
        api_secret=api_secret,
        symbols=symbols,
        db_path=DB_PATH,
        snapshot_every_s=1.0,
        depth_levels=20,
    )

    # Wire Ctrl+C / SIGTERM to a graceful stop.
    # WHY not just let asyncio.run() catch KeyboardInterrupt?
    # That works on Linux/Mac but on Windows (which the user is on),
    # signal handling inside asyncio is finicky. An explicit asyncio.Event
    # toggled by a signal handler is portable.
    stop_signal_event = asyncio.Event()

    def _on_signal(*_):
        logger.info("shutdown signal received")
        stop_signal_event.set()

    # SIGINT = Ctrl+C. SIGTERM = polite kill. Windows supports SIGINT only
    # in console apps; SIGTERM may raise — guard with try/except.
    for sig in (os_signal.SIGINT, getattr(os_signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            os_signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # Some shells/IDEs disallow installing handlers; fall back to
            # KeyboardInterrupt propagation below.
            pass

    await ingestor.start()
    try:
        await stop_signal_event.wait()
    except KeyboardInterrupt:
        logger.info("keyboard interrupt")
    finally:
        await ingestor.stop()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Last-resort handler in case the inner await never gets there.
        logger.info("ingestor runner exited")


if __name__ == "__main__":
    main()

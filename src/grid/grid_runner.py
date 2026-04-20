"""
Grid Runner — long-running CLI entrypoint for the grid trading engine.

USAGE (venv activated, from repo root):
    python -m src.grid.grid_runner

WHAT IT DOES
- Reads trading.pairs from settings.yaml
- For each symbol: builds a GridEngine (exchange + config + optional S&D
  signal + shared RiskManager)
- Opens ONE authenticated UserDataStream (account-wide) and dispatches
  each executionReport event to every engine whose symbol it matches
- Runs a risk monitor task that polls the kill-switch sentinel file and
  flips stop_event if the RiskManager halts for any reason
- On SIGINT / SIGTERM / risk halt: flips stop_event, every engine runs
  cancel_all on the way out, clients close

EXTERNAL HALT
The risk layer watches `runtime/HALT` (configurable). To stop the bot
from outside the process: `touch runtime/HALT`. Engines unwind cleanly,
all open orders cancel, then the runner exits.

SAFETY
- `use_testnet: true` in settings.yaml is the only place the production/
  testnet switch lives. Double-check it before running.
- Every engine's shutdown path cancels all orders for its symbol. Even
  if one engine's cancel fails, the others still run.

CREDENTIALS
Two kinds, both on the same Binance account:
- BINANCE_API_KEY + BINANCE_API_SECRET (HMAC) — signs every REST call
  made by the BinanceExchange adapter (orders, filters, ticker, balances).
- BINANCE_WS_API_KEY + BINANCE_WS_PRIVATE_KEY_PATH (Ed25519) — signs
  `session.logon` on the WS API connection that carries user data events.
  Binance retired HMAC for user data streams, so this split is required.
"""

import argparse
import asyncio
import os
import pathlib
import signal as os_signal
import sys

import yaml
from dotenv import load_dotenv

from src.exchange.binance import BinanceExchange
from src.exchange.dry_run import DryRunExchange
from src.exchange.user_data_stream import UserDataStream
from src.grid.config import GridEngineConfig
from src.grid.engine import GridEngine
from src.market_data.storage import MarketDataStore
from src.risk.config import RiskConfig
from src.risk.manager import RiskManager
from src.signals.liquidity import LiquidityConfig, LiquiditySignal
from src.signals.supply_demand import SupplyDemandConfig, SupplyDemandSignal
from src.utils.logger import get_logger

logger = get_logger("grid_runner")

SETTINGS_PATH = "config/settings.yaml"
DB_PATH = "data/marketdata.db"


def _load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run(dry_run: bool = False) -> None:
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        logger.error("BINANCE_API_KEY / BINANCE_API_SECRET not set in .env")
        sys.exit(2)

    ws_api_key = os.getenv("BINANCE_WS_API_KEY", "")
    ws_priv_path = os.getenv("BINANCE_WS_PRIVATE_KEY_PATH", "")
    if not ws_api_key or not ws_priv_path:
        logger.error("BINANCE_WS_API_KEY / BINANCE_WS_PRIVATE_KEY_PATH not set in .env "
                     "(Ed25519 key for session.logon — see scripts/generate_ed25519_keypair.py)")
        sys.exit(2)

    settings = _load_settings()
    use_testnet = bool(settings.get("exchange", {}).get("use_testnet", True))
    symbols = [p["symbol"] for p in settings["trading"]["pairs"]]

    logger.info("grid runner starting",
                symbols=symbols,
                testnet=use_testnet,
                dry_run=dry_run,
                settings=SETTINGS_PATH)

    # --- Exchange adapter (sync client under the hood) ---
    real_exchange = BinanceExchange(api_key=api_key, api_secret=api_secret, testnet=use_testnet)
    # Wrap BEFORE connect() so the dry-run banner fires at the right time.
    exchange = DryRunExchange(real_exchange) if dry_run else real_exchange
    await exchange.connect()

    # --- Optional S&D signal: only if the ingestor has been collecting data ---
    store: "MarketDataStore | None" = None
    signal: "SupplyDemandSignal | None" = None
    if pathlib.Path(DB_PATH).exists():
        store = MarketDataStore(DB_PATH)
        await store.connect()
        sd_raw = settings.get("signals", {}).get("supply_demand", {}) or {}
        sd_cfg = SupplyDemandConfig.from_dict(sd_raw)
        signal = SupplyDemandSignal(store, sd_cfg)
        logger.info("S&D signal wired", db=DB_PATH)
    else:
        logger.info("no market data DB found — grid will be unbiased",
                    expected_db=DB_PATH)

    # --- Shared risk manager (one per process, all engines consult it) ---
    risk_cfg = RiskConfig.from_yaml(settings)
    risk = RiskManager(risk_cfg)
    logger.info("risk manager wired",
                max_open_orders=risk_cfg.max_open_orders,
                daily_loss_limit_usd=risk_cfg.daily_loss_limit_usd,
                kill_switch_path=risk_cfg.kill_switch_path)

    # --- Shared liquidity signal (one per process, all engines consult it) ---
    liq_raw = settings.get("signals", {}).get("liquidity", {}) or {}
    liq_cfg = LiquidityConfig.from_dict(liq_raw)
    liquidity = LiquiditySignal(exchange=exchange, config=liq_cfg)
    logger.info("liquidity signal wired",
                pause_spread_bps=liq_cfg.pause_spread_bps,
                min_spacing_to_spread_ratio=liq_cfg.min_spacing_to_spread_ratio,
                depth_levels=liq_cfg.depth_levels)

    # --- One engine per symbol ---
    engines: dict[str, GridEngine] = {}
    for symbol in symbols:
        cfg = GridEngineConfig.from_yaml(symbol, settings)
        engines[symbol] = GridEngine(
            exchange=exchange, config=cfg, signal=signal,
            risk=risk, liquidity=liquidity,
        )

    # --- User data stream dispatcher: fan events out to all engines ---
    # Each engine filters for its own symbol, so a duplicate dispatch is
    # harmless. Simpler than maintaining a symbol→engine lookup here.
    async def _dispatch(event: dict) -> None:
        # Shallow fan-out — gather so handlers run concurrently; exceptions
        # are absorbed inside each engine's on_fill already.
        await asyncio.gather(*(e.on_fill(event) for e in engines.values()))

    user_stream = UserDataStream(
        testnet=use_testnet,
        api_key=ws_api_key,
        private_key_path=ws_priv_path,
        on_event=_dispatch,
    )

    # --- Signal handling ---
    stop_event = asyncio.Event()

    def _on_signal(*_):
        logger.info("shutdown signal received")
        stop_event.set()

    for sig in (os_signal.SIGINT, getattr(os_signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            os_signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    # --- Risk monitor: polls kill switch + watches halt state ---
    # Shared with all engines via the RiskManager instance. When the file
    # appears or the manager trips for any other reason, we flip stop_event
    # so engines run their normal cancel-all shutdown path.
    async def _risk_monitor() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.sleep(risk_cfg.poll_interval_s)
            except asyncio.CancelledError:
                return
            risk.poll_kill_switch()
            if risk.halted:
                logger.error("risk halt — initiating shutdown",
                             reason=risk.halt_reason)
                stop_event.set()
                return

    # --- Run stream + engines concurrently ---
    tasks = [asyncio.create_task(user_stream.run(stop_event), name="user_data_stream"),
             asyncio.create_task(_risk_monitor(), name="risk_monitor")]
    for symbol, engine in engines.items():
        tasks.append(asyncio.create_task(engine.run(stop_event), name=f"engine:{symbol}"))

    try:
        # Wait until any task ends (normally stop_event firing ends them all
        # together; but if one engine crashes, we still want to stop the rest).
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # If something exited early, signal everyone to stop cleanly.
        if not stop_event.is_set():
            logger.warning("a task exited early — triggering shutdown")
            stop_event.set()
        # Let the rest finish their shutdown (this is where cancel_all runs).
        if pending:
            await asyncio.wait(pending, timeout=30.0)
    finally:
        # Surface any exception from completed tasks.
        for t in tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error("task finished with error",
                                 task=t.get_name(), error=str(exc))

        # Close clients + storage.
        try:
            await exchange.disconnect()
        except Exception:
            pass
        if store:
            await store.close()

        logger.info("grid runner stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid trading engine runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Rehearsal mode: read live prices, but do NOT place real orders. "
             "Every place/cancel is logged and returns a synthetic response.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("grid runner exited (keyboard interrupt)")


if __name__ == "__main__":
    main()

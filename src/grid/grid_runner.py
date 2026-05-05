"""
Grid Runner — long-running CLI entrypoint for the grid trading engine.

USAGE (venv activated, from repo root):
    python -m src.grid.grid_runner                         # testnet (default)
    python -m src.grid.grid_runner --dry-run               # no orders placed
    python -m src.grid.grid_runner --env mainnet --live    # REAL MONEY

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

ENVIRONMENT (Phase 10)
Testnet vs mainnet is resolved by src/exchange/env_config.py in this
order: --env CLI flag > BINANCE_ENV var > settings.yaml use_testnet.
Mainnet additionally REQUIRES --live (or BINANCE_LIVE=yes in env) — this
is the 'I understand this is real money' gate. When mainnet is active,
RiskConfig merges risk_overrides.mainnet over the base risk: block, so
the real-money run gets stricter caps than the testnet defaults.

CREDENTIALS
Two kinds per environment, both on the same Binance account:
- API_KEY + API_SECRET (HMAC) — signs every REST call made by the
  BinanceExchange adapter (orders, filters, ticker, balances).
- WS_API_KEY + WS_PRIVATE_KEY_PATH (Ed25519) — signs `session.logon` on
  the WS API connection that carries user data events. Binance retired
  HMAC for user data streams, so this split is required.
The env_config module loads them with the BINANCE_TESTNET_* / BINANCE_MAINNET_*
prefixes, falling back to the unprefixed BINANCE_* vars if not set.
"""

import argparse
import asyncio
import pathlib
import signal as os_signal
import sys
import time

import yaml
from dotenv import load_dotenv

from src.exchange.binance import BinanceExchange
from src.exchange.dry_run import DryRunExchange
from src.exchange.env_config import (
    EnvConfigError,
    load_credentials,
    render_banner,
    require_live_acknowledgement,
    resolve_env_mode,
)
from src.exchange.user_data_stream import UserDataStream
from src.grid.config import GridEngineConfig
from src.grid.engine import GridEngine
from src.market_data.storage import MarketDataStore
from src.notifications.alerter import make_alerter
from src.observability.heartbeat import HeartbeatMonitor, ObservabilityConfig
from src.risk.config import RiskConfig
from src.risk.manager import RiskManager
from src.signals.liquidity import LiquidityConfig, LiquiditySignal
from src.signals.sentiment import (
    FileSentimentSource,
    SentimentConfig,
    SentimentSignal,
)
from src.signals.supply_demand import SupplyDemandConfig, SupplyDemandSignal
from src.utils.logger import configure_logging, get_logger

logger = get_logger("grid_runner")

SETTINGS_PATH = "config/settings.yaml"
DB_PATH = "data/marketdata.db"

# Default kill-switch sentinel path. Mirrors RiskConfig.kill_switch_path
# default. The early-startup check (_abort_if_halt_sentinel_present) reads
# this directly rather than going through RiskConfig because it must run
# before any expensive setup. If you customise kill_switch_path in
# settings.yaml, update this constant too.
_HALT_SENTINEL_PATH = pathlib.Path("runtime/HALT")

# Exit code used when we abort because the HALT sentinel is present at
# startup. Listed in deploy/grid-bot.service's RestartPreventExitStatus so
# systemd does NOT auto-restart in this specific case — preventing the
# boot-halt-restart storm we hit on 2026-05-04.
_EXIT_HALT_SENTINEL_PRESENT = 3


def _load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _abort_if_halt_sentinel_present(
    path: pathlib.Path = _HALT_SENTINEL_PATH,
) -> None:
    """
    If the HALT sentinel file exists at startup, exit immediately with code
    `_EXIT_HALT_SENTINEL_PRESENT` so systemd does NOT auto-restart us.

    WHY this exists: the runtime/HALT sentinel is intended as "trip the kill
    switch on a RUNNING bot", not "prevent a stopped bot from starting". But
    systemd's `Restart=always` doesn't make that distinction — it brings the
    process back, the runner constructs the alerter, fires [BOOT], the risk
    monitor sees the sentinel, fires [HALT], the runner exits with [STOP],
    systemd restarts again, and so on. That's a 4-alert-per-cycle storm
    (~50 alerts/min). Found in Phase 11D dev on 2026-05-04 after a single
    /halt produced ~1.4k Telegram messages in an hour.

    The fix is two-pronged:
      * This function: bail out BEFORE any alerter / engine / task setup,
        so the boot path is silent if the sentinel is present.
      * deploy/grid-bot.service: `RestartPreventExitStatus=3` so systemd
        respects this exit code and does NOT restart.

    Either alone is insufficient. Both together break the storm cleanly.
    """
    if path.exists():
        # Logger may not be set up yet at the very first call site, but
        # this is called AFTER configure_logging(settings) so it's safe.
        logger.error("HALT sentinel present at startup — refusing to start",
                     path=str(path))
        # Stderr too, for the operator running interactively.
        print(
            f"\nRefusing to start: {path} is present.\n"
            f"Remove it and re-launch when ready: rm {path}\n",
            file=sys.stderr,
        )
        sys.exit(_EXIT_HALT_SENTINEL_PRESENT)


async def _run(dry_run: bool = False,
               env_override: "str | None" = None,
               live_ack: bool = False) -> None:
    load_dotenv()
    settings = _load_settings()

    # Wire stdlib + structlog now so every line below (including the env
    # banner / errors) lands in logs/grid_bot.log as well as stderr. Phase 10C
    # discovered this had been a no-op since Phase 1 — settings.logging.file
    # was being read by nothing.
    configure_logging(settings)

    # Early HALT-sentinel check — done BEFORE any alerter / engine / task
    # setup so a forgotten runtime/HALT does not trigger a systemd boot-halt-
    # restart storm. See _abort_if_halt_sentinel_present docstring for the
    # full incident postmortem.
    _abort_if_halt_sentinel_present()

    # --- Env resolution (Phase 10) ---------------------------------------
    # Decide testnet vs mainnet BEFORE we print anything or load keys. The
    # banner and the credential prefix both depend on this.
    try:
        env_mode = resolve_env_mode(settings, cli_override=env_override)
        require_live_acknowledgement(env_mode, cli_live=live_ack)
        creds = load_credentials(env_mode)
    except EnvConfigError as e:
        # Print to stderr so it's seen even if logs go to file. Exit 2 →
        # distinguishes "misconfiguration" from "crash".
        print(f"\nENV CONFIG ERROR:\n{e}\n", file=sys.stderr)
        sys.exit(2)

    # Loud banner first — before any logger output. Plain print so it
    # renders identically regardless of log sink.
    print(render_banner(env_mode, dry_run), file=sys.stderr)

    use_testnet = env_mode == "testnet"
    symbols = [p["symbol"] for p in settings["trading"]["pairs"]]

    # Track session start for the [STOP] alert's uptime field. time.time()
    # is fine — UTC wall clock is what the operator will reason about.
    started_at = time.time()

    # Alerter (Phase 11A). Constructed early so any subsequent failure that
    # raises before we hit `await alerter.start()` still leaves a working
    # NullAlerter wired into anything we do construct. make_alerter never
    # raises — broken telegram config returns NullAlerter.
    alerter = make_alerter(settings)
    await alerter.start()

    logger.info("grid runner starting",
                symbols=symbols,
                env_mode=env_mode,
                dry_run=dry_run,
                settings=SETTINGS_PATH)

    # --- Exchange adapter (sync client under the hood) ---
    real_exchange = BinanceExchange(
        api_key=creds.api_key, api_secret=creds.api_secret, testnet=use_testnet,
    )
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
    # env_mode drives the mainnet-overrides merge inside from_yaml.
    risk_cfg = RiskConfig.from_yaml(settings, env_mode=env_mode)
    risk = RiskManager(risk_cfg, alerter=alerter)
    logger.info("risk manager wired",
                env_mode=env_mode,
                max_open_orders=risk_cfg.max_open_orders,
                daily_loss_limit_usd=risk_cfg.daily_loss_limit_usd,
                kill_switch_path=risk_cfg.kill_switch_path)

    # --- Shared liquidity signal (one per process, all engines consult it) ---
    liq_raw = settings.get("signals", {}).get("liquidity", {}) or {}
    liq_cfg = LiquidityConfig.from_dict(liq_raw)
    liquidity = LiquiditySignal(exchange=exchange, config=liq_cfg, alerter=alerter)
    logger.info("liquidity signal wired",
                pause_spread_bps=liq_cfg.pause_spread_bps,
                min_spacing_to_spread_ratio=liq_cfg.min_spacing_to_spread_ratio,
                depth_levels=liq_cfg.depth_levels)

    # --- Shared sentiment signal (one per process, all engines consult it) ---
    # The pause threshold lives at the top level of `signals:` (alongside
    # the weights) for historical reasons; sub-block holds path/staleness.
    signals_raw = settings.get("signals", {}) or {}
    sent_raw = signals_raw.get("sentiment", {}) or {}
    sent_cfg = SentimentConfig.from_dict(
        sent_raw,
        pause_threshold=signals_raw.get("sentiment_pause_threshold"),
    )
    sentiment_source = FileSentimentSource(sent_cfg.sentiment_path)
    sentiment = SentimentSignal(
        source=sentiment_source, config=sent_cfg, alerter=alerter,
    )
    logger.info("sentiment signal wired",
                pause_threshold=sent_cfg.pause_threshold,
                sentiment_path=sent_cfg.sentiment_path,
                max_age_seconds=sent_cfg.max_age_seconds)

    # --- One engine per symbol ---
    engines: dict[str, GridEngine] = {}
    for symbol in symbols:
        cfg = GridEngineConfig.from_yaml(symbol, settings)
        engines[symbol] = GridEngine(
            exchange=exchange, config=cfg, signal=signal,
            risk=risk, liquidity=liquidity, sentiment=sentiment,
            alerter=alerter,
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
        api_key=creds.ws_api_key,
        private_key_path=creds.ws_private_key_path,
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

    # --- Heartbeat monitor (Phase 10B) ---
    # Emits a STATUS.json snapshot + log line every interval_s, plus a
    # DAILY SUMMARY line on UTC day rollover. Read-only on engines + risk.
    obs_cfg = ObservabilityConfig.from_yaml(settings)
    heartbeat = HeartbeatMonitor(
        engines=engines, risk=risk, env_mode=env_mode, config=obs_cfg,
        alerter=alerter,
    )
    logger.info("heartbeat wired",
                interval_s=obs_cfg.heartbeat_interval_s,
                status_path=obs_cfg.status_path)

    # --- Telegram two-way commands (Phase 11A step 6) -----------------
    # status_provider: closure that snapshots state for the [STATUS] reply.
    # Reads are race-free enough — counters are simple ints (GIL-safe in
    # CPython) and risk-side reads use the manager's own lock internally.
    def _status_provider() -> dict:
        placed = sum(getattr(e, "_placed", 0) for e in engines.values())
        filled = sum(getattr(e, "_filled", 0) for e in engines.values())
        return {
            "env_mode": env_mode,
            "open_orders": int(risk.open_order_count),
            "placed_total": int(placed),
            "filled_total": int(filled),
            "realized_24h_usd": float(risk.realized_pnl_24h),
            "halted": bool(risk.halted),
            "uptime_seconds": time.time() - started_at,
        }

    # halt_handler: creates the sentinel file the existing risk monitor
    # already polls. We deliberately go through the file path rather than
    # calling risk.trip() directly so the on-disk semantics match a manual
    # `touch runtime/HALT` — operators can audit what triggered the stop
    # by checking whether the file exists.
    def _halt_handler() -> None:
        path = pathlib.Path(risk_cfg.kill_switch_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        logger.warning("remote /halt received via Telegram — sentinel created",
                       path=str(path))

    alerter.set_command_handlers(
        status_provider=_status_provider,
        halt_handler=_halt_handler,
    )
    # Idempotent — second call spawns the listener now that handlers are
    # attached. The first start() (further up) already spawned the sender
    # so boot/crash alerts work even if init fails before this point.
    await alerter.start()

    # Boot alert — components are wired but tasks haven't started yet. The
    # operator now sees [BOOT] arrive ~immediately, so a startup that hangs
    # in user_stream.run() is detectable from the phone.
    alerter.boot(
        env_mode=env_mode,
        symbols=symbols,
        max_open_orders=risk_cfg.max_open_orders,
        # Phase 11D: read the effective floor via the RiskManager property,
        # which prefers the BalancePoller's equity-derived value if wired.
        # Without a poller, this returns the static config value verbatim.
        daily_loss_limit_usd=risk.daily_loss_limit_usd,
    )

    # --- Run stream + engines concurrently ---
    tasks = [asyncio.create_task(user_stream.run(stop_event), name="user_data_stream"),
             asyncio.create_task(_risk_monitor(), name="risk_monitor"),
             asyncio.create_task(heartbeat.run(stop_event), name="heartbeat")]
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
        # Surface any exception from completed tasks. Each task crash fires
        # a [CRASH] alert so the operator hears about it even before the
        # [STOP] alert lands a few lines later.
        for t in tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error("task finished with error",
                                 task=t.get_name(), error=str(exc))
                    try:
                        alerter.crash(component=t.get_name(), exc=exc)
                    except Exception:
                        # Alerter must never block shutdown — but defensively
                        # swallow anything it raises in this path.
                        pass

        # Shutdown alert BEFORE we tear down clients — the alert summarises
        # the session and reads from RiskManager state, which is still live
        # at this point. alerter.stop() drains the sender queue afterwards.
        try:
            alerter.shutdown(
                final_open_orders=risk.open_order_count,
                session_realized_usd=risk.realized_pnl_24h,
                uptime_seconds=time.time() - started_at,
            )
        except Exception:
            pass
        try:
            await alerter.stop()
        except Exception:
            pass

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
    parser.add_argument(
        "--env",
        choices=("testnet", "mainnet"),
        default=None,
        help="Override settings.yaml and BINANCE_ENV. Selects which Binance "
             "environment + credential slot to use.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required to run against mainnet (unless BINANCE_LIVE=yes is set). "
             "This is the 'I understand this is real money' gate — mainnet "
             "aborts without it.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(dry_run=args.dry_run,
                         env_override=args.env,
                         live_ack=args.live))
    except KeyboardInterrupt:
        logger.info("grid runner exited (keyboard interrupt)")


if __name__ == "__main__":
    main()

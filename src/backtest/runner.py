"""
BacktestRunner — orchestrates a full replay.

WHAT IT DOES
1. Builds a BacktestExchange seeded with a starting price for every
   symbol (pulled from the first tick of the source).
2. Builds one GridEngine per symbol using the loaded settings.yaml,
   with drift_check_interval_s overridden huge — the internal
   drift_monitor is disabled and we drive drift manually from the
   replay loop (see engine.check_drift_now).
3. Wires the exchange's on_fill_event to a dispatcher that (a) records
   fill stats and (b) fans the event out to every engine.
4. Starts each engine.run(stop_event) as a task so placement/shutdown
   go through the normal code paths — same code the live bot executes.
5. Iterates ticks from the PriceSource: feeds each into the exchange,
   records tick stats, and periodically drives drift checks.
6. When the source is exhausted, flips stop_event and waits for all
   engines to tear down (cancel_all under the hood).
7. Snapshots engine counters into the stats collector and returns a
   BacktestReport.

WHAT WE DELIBERATELY DO NOT CHANGE
The engine, risk manager, liquidity signal, and sentiment signal all
run unchanged — the runner plugs into the same seams the production
runner uses. If the backtest passes but production doesn't, the gap
is in one of the edge pieces (exchange adapter, user data stream,
actual fill semantics) — not in the strategy logic.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import yaml

from src.backtest.exchange import BacktestExchange
from src.backtest.sources import PriceSource, PriceTick
from src.backtest.stats import BacktestReport, StatsCollector
from src.grid.config import GridEngineConfig
from src.grid.engine import GridEngine
from src.risk.config import RiskConfig
from src.risk.manager import RiskManager
from src.signals.liquidity import LiquidityConfig, LiquiditySignal
from src.signals.sentiment import FileSentimentSource, SentimentConfig, SentimentSignal
from src.utils.logger import get_logger

logger = get_logger("backtest_runner")


@dataclass
class BacktestOptions:
    """User-tunable knobs for one backtest run."""
    settings_path: str = "config/settings.yaml"
    symbols: Optional[list[str]] = None         # defaults to settings.trading.pairs
    # How many ticks between drift checks. Lower = more CPU + potentially
    # more recenters caught; higher = faster but might miss a recenter
    # that fired mid-burst. 10 is a sane default.
    drift_check_every: int = 10
    # Synthetic-book spread in bps used by the exchange's book synthesiser.
    synthetic_spread_bps: float = 1.0
    # Enable the risk manager? True means the daily-loss floor and
    # kill-switch semantics are active. For pure strategy tuning usually
    # leave on so backtest matches production; turn off to isolate.
    enable_risk: bool = True
    # Enable the liquidity signal? In v1 the synthetic book has a fixed
    # spread so liquidity rarely changes anything, but leaving the path
    # wired means the stats include any paused_by_liquidity counts.
    enable_liquidity: bool = True
    # Enable sentiment? Usually off for backtests — no pipeline is feeding
    # the sentinel file — but can be toggled for what-if runs.
    enable_sentiment: bool = False


class BacktestRunner:

    def __init__(self, source: PriceSource, options: Optional[BacktestOptions] = None):
        self._source = source
        self._options = options or BacktestOptions()
        self._settings: dict = {}
        self._symbols: list[str] = []

    async def run(self) -> BacktestReport:
        self._load_settings()

        # Stats collector: fed for every tick AND every fill.
        stats = StatsCollector(self._symbols)

        # Build the exchange. We'll set initial prices from the first tick
        # we see (below) so the engine's initialise() gets a valid ticker.
        exchange = BacktestExchange(
            symbols=self._symbols,
            synthetic_spread_bps=self._options.synthetic_spread_bps,
        )
        await exchange.connect()

        # Risk + signals — same wiring as production grid_runner, just
        # fed the backtest exchange instead of the real one.
        risk = None
        if self._options.enable_risk:
            risk = RiskManager(RiskConfig.from_yaml(self._settings))

        liquidity = None
        if self._options.enable_liquidity:
            liq_raw = self._settings.get("signals", {}).get("liquidity", {}) or {}
            liquidity = LiquiditySignal(exchange=exchange, config=LiquidityConfig.from_dict(liq_raw))

        sentiment = None
        if self._options.enable_sentiment:
            signals_raw = self._settings.get("signals", {}) or {}
            sent_raw = signals_raw.get("sentiment", {}) or {}
            sent_cfg = SentimentConfig.from_dict(
                sent_raw,
                pause_threshold=signals_raw.get("sentiment_pause_threshold"),
            )
            sentiment = SentimentSignal(
                source=FileSentimentSource(sent_cfg.sentiment_path),
                config=sent_cfg,
            )

        # Build engines. drift_check_interval_s is set huge so the engine's
        # internal drift monitor effectively never fires — we call
        # engine.check_drift_now() manually from the replay loop.
        engines: dict[str, GridEngine] = {}
        for sym in self._symbols:
            cfg_base = GridEngineConfig.from_yaml(sym, self._settings)
            cfg = GridEngineConfig(
                symbol=cfg_base.symbol,
                levels_above=cfg_base.levels_above,
                levels_below=cfg_base.levels_below,
                base_spacing_pct=cfg_base.base_spacing_pct,
                min_spacing_pct=cfg_base.min_spacing_pct,
                max_spacing_pct=cfg_base.max_spacing_pct,
                order_size=cfg_base.order_size,
                recenter_threshold_pct=cfg_base.recenter_threshold_pct,
                drift_check_interval_s=10_000.0,   # effectively disabled
            )
            engines[sym] = GridEngine(
                exchange=exchange, config=cfg,
                signal=None,   # S&D not used in v1 backtest — ingestor DB
                               # may be empty / stale and the synthetic
                               # tape doesn't have zones anyway.
                risk=risk, liquidity=liquidity, sentiment=sentiment,
            )

        # Fan-out dispatcher: the exchange sees fills first (it owns the
        # fill event), mirrors to stats, and fans to every engine.on_fill
        # (engines filter by symbol themselves — cheap, and keeps us from
        # maintaining a symbol→engine lookup here).
        async def _dispatch(event: dict) -> None:
            # Stats: record the fill. Convert Binance string fields back.
            symbol = event.get("s", "")
            side = event.get("S", "")
            try:
                price = float(event.get("L", 0))
                qty = float(event.get("l", 0))
            except (TypeError, ValueError):
                price = qty = 0.0
            if symbol and side and qty > 0:
                stats.record_fill(symbol, side, price, qty)
            # Engines.
            await asyncio.gather(*(e.on_fill(event) for e in engines.values()))

        exchange.on_fill_event = _dispatch

        # ---- Pre-seed phase ----
        # Engines crash-land if they init against price=0 (every level
        # rounds to 0.00 and fails min_notional). So BEFORE we start
        # any engine task, drain the source until we've seen at least
        # one tick per symbol — seed those prices into the exchange,
        # buffer the ticks for replay, THEN start engines.
        wall_start = time.perf_counter()
        virtual_start: Optional[float] = None
        virtual_end: Optional[float] = None
        total_ticks = 0
        seeded: set[str] = set()
        buffered_ticks: list[PriceTick] = []

        tick_iter = self._source.ticks().__aiter__()

        # Pull ticks until every symbol has a seed price. If the source
        # runs out before that happens, we give up — can't backtest a
        # symbol we never see a price for.
        while seeded != set(self._symbols):
            try:
                tick = await tick_iter.__anext__()
            except StopAsyncIteration:
                missing = set(self._symbols) - seeded
                logger.warning("source exhausted before seeding all symbols",
                               missing=sorted(missing))
                break
            buffered_ticks.append(tick)
            if tick.symbol in self._symbols and tick.symbol not in seeded:
                exchange.set_price(tick.symbol, tick.price)
                seeded.add(tick.symbol)
                logger.info("seeded symbol", symbol=tick.symbol, price=tick.price)

        # Now start engines — their _initialize will read the seeded prices.
        stop_event = asyncio.Event()
        engine_tasks = [
            asyncio.create_task(e.run(stop_event), name=f"engine:{sym}")
            for sym, e in engines.items()
        ]
        # Yield enough times for engines to complete _initialize and place
        # their initial grids. _initialize has a few awaits (filters,
        # ticker, place_order loop) — sleep(0) a few times lets each
        # engine progress through them without blocking on wall time.
        for _ in range(20):
            await asyncio.sleep(0)

        # ---- Replay phase ----
        ticks_since_drift_check = 0

        async def _pump_tick(tick: PriceTick) -> None:
            nonlocal virtual_start, virtual_end, total_ticks, ticks_since_drift_check
            if virtual_start is None:
                virtual_start = tick.timestamp
            virtual_end = tick.timestamp
            stats.record_tick(tick.symbol, tick.price)
            await exchange.tick_trade(
                symbol=tick.symbol, price=tick.price,
                qty=tick.qty, timestamp=tick.timestamp,
            )
            total_ticks += 1
            ticks_since_drift_check += 1
            if ticks_since_drift_check >= self._options.drift_check_every:
                ticks_since_drift_check = 0
                for eng in engines.values():
                    await eng.check_drift_now()
                await asyncio.sleep(0)

        try:
            # Drain the pre-seed buffer first.
            for tick in buffered_ticks:
                await _pump_tick(tick)
            # Then continue with whatever the source still has.
            async for tick in tick_iter:
                await _pump_tick(tick)
        finally:
            # Let the final batch of work settle, then stop.
            for eng in engines.values():
                await eng.check_drift_now()
            stop_event.set()

            # Each engine's shutdown cancels all its orders — that's the
            # normal path. Give them a generous timeout: a healthy shutdown
            # takes <1s.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*engine_tasks, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("engine shutdown timed out; forcing cancel")
                for t in engine_tasks:
                    t.cancel()

            await exchange.disconnect()

        wall_end = time.perf_counter()

        # Snapshot engine counters into stats.
        for sym, eng in engines.items():
            stats.record_engine_counters(sym, {
                "placed_total": eng._placed,
                "filled_total": eng._filled,
                "recentered_total": eng._recentered,
                "blocked_by_risk": eng._blocked_by_risk,
                "paused_by_liquidity": eng._paused_by_liquidity,
                "paused_by_sentiment": eng._paused_by_sentiment,
            })

        duration_virtual = (virtual_end - virtual_start) if virtual_start is not None else 0.0
        report = stats.build_report(
            duration_virtual_s=duration_virtual,
            duration_wall_s=wall_end - wall_start,
            total_ticks=total_ticks,
        )
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        with open(self._options.settings_path, "r", encoding="utf-8") as f:
            self._settings = yaml.safe_load(f)
        if self._options.symbols is not None:
            self._symbols = list(self._options.symbols)
        else:
            self._symbols = [p["symbol"] for p in self._settings["trading"]["pairs"]]

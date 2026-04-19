"""
Grid Engine — places the initial grid, reacts to fills with replacements,
recenters on price drift, and tears down cleanly on stop.

====================================================================
LIFECYCLE
====================================================================
    await engine.run(stop_event)

1. INIT
   - fetch symbol filters (tick, step, min_notional)
   - fetch current price → becomes grid center
   - optionally fetch S&D zones → bias level placement
   - compute_grid(...) → list of GridLevel
   - place each level (rounded + filter-validated), track in GridState

2. RUN
   - drift_monitor task polls price every N sec; recenters if drift ≥ threshold
   - on_fill() is invoked by the UserDataStream for every executionReport
     - FILLED   → remove from state, place mirror order (buy fill → sell up, sell fill → buy down)
     - CANCELED → remove (no replacement — we or the exchange asked for it)
     - NEW / PARTIALLY_FILLED → ignored (state unchanged)

3. SHUTDOWN
   - cancel_all_orders(symbol) — guarantees no dangling orders on the book

====================================================================
DESIGN NOTES
====================================================================
* The engine takes `ExchangeBase`, not `BinanceExchange`. Placement and
  cancellation go through the abstract interface so the engine is
  exchange-agnostic. The user data stream is wired externally (runner
  code) and feeds events via on_fill(...).

* Replacement math uses the CURRENT center and base spacing — not the
  filled order's original offset. That way a recentered grid's
  replacements land on the new grid, not the old one. This is the
  boring-correct choice; more elaborate schemes (size laddering,
  profit-locking) can replace this method in v2.

* Every place_order call is individually try/except'd. One bad order
  (e.g. filter violation we didn't catch) must not abort the whole grid.

* Fill handling is IDEMPOTENT on the tracking side: if an event is
  delivered twice, the second one finds no order in state and no-ops.
"""

import asyncio
import time
from typing import Optional

from src.exchange.base import ExchangeBase, OrderSide, OrderType
from src.exchange.filters import SymbolFilter, meets_min_notional, round_to_step, round_to_tick
from src.grid.config import GridEngineConfig
from src.grid.levels import GridParams, compute_grid
from src.grid.state import GridState
from src.risk.manager import RiskManager
from src.signals.base import Signal
from src.utils.logger import get_logger

logger = get_logger("grid_engine")


class GridEngine:
    """
    One engine per (symbol, exchange). The caller constructs + runs it.

    USAGE:
        engine = GridEngine(exchange, config, signal=optional_sd_signal)
        stream = UserDataStream(async_client, on_event=engine.on_fill)
        # run both concurrently — stream.run() feeds engine.on_fill,
        # engine.run() owns initial placement + drift monitoring + shutdown
        await asyncio.gather(
            stream.run(stop_event),
            engine.run(stop_event),
        )
    """

    def __init__(
        self,
        exchange: ExchangeBase,
        config: GridEngineConfig,
        signal: Optional[Signal] = None,
        risk: Optional[RiskManager] = None,
    ):
        self._exchange = exchange
        self._config = config
        self._signal = signal
        # RiskManager is shared across engines (one instance per process).
        # Optional so existing tests + dry-run rehearsals without a risk
        # config keep working — production runner always passes one.
        self._risk = risk
        self._state = GridState()
        self._filters: Optional[SymbolFilter] = None
        # Counters for observability at shutdown.
        self._placed = 0
        self._filled = 0
        self._recentered = 0
        self._blocked_by_risk = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop. Initialises grid, runs drift monitor, tears down on stop."""
        try:
            await self._initialize()
        except Exception:
            # Initialisation failure is fatal — bot has nothing to do.
            # Log via exception chain, then re-raise so the runner sees it.
            logger.exception("grid engine init failed")
            raise

        drift_task = asyncio.create_task(self._drift_monitor(stop_event))
        try:
            await stop_event.wait()
        finally:
            drift_task.cancel()
            try:
                await drift_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._shutdown()

    async def on_fill(self, event: dict) -> None:
        """
        Consume one user-data event. Safe to call with any payload — the
        engine ignores events that are not executionReports for its symbol.
        """
        if event.get("e") != "executionReport":
            return
        if event.get("s") != self._config.symbol:
            return

        status = event.get("X")
        order_id = str(event.get("i", ""))
        if not order_id:
            return

        if status == "FILLED":
            await self._handle_filled(order_id, event)
        elif status == "CANCELED":
            # Someone (us or exchange) cancelled. Drop from state, no replace.
            removed = self._state.remove_order(order_id)
            if self._risk is not None:
                self._risk.record_closed(order_id)
            if removed:
                logger.info("order cancelled (event)",
                            order_id=order_id, side=removed.side, price=removed.price)
        # NEW / PARTIALLY_FILLED / REJECTED / EXPIRED: no action in v1.
        # PARTIAL fills could update filled_qty but the engine only cares
        # about full fills for placement symmetry — partial-aware sizing
        # is a v2 concern.

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        self._filters = await self._exchange.get_symbol_filters(self._config.symbol)
        current = await self._exchange.get_ticker_price(self._config.symbol)
        self._state.center_price = current
        zones = await self._compute_zones_safely()
        logger.info("grid init",
                    symbol=self._config.symbol,
                    center=current,
                    levels_above=self._config.levels_above,
                    levels_below=self._config.levels_below,
                    zones=len(zones) if zones else 0)
        await self._place_grid(current, zones)

    async def _place_grid(self, center: float, zones) -> None:
        params = GridParams(
            symbol=self._config.symbol,
            center_price=center,
            base_spacing_pct=self._config.base_spacing_pct,
            min_spacing_pct=self._config.min_spacing_pct,
            max_spacing_pct=self._config.max_spacing_pct,
            levels_above=self._config.levels_above,
            levels_below=self._config.levels_below,
            order_size=self._config.order_size,
            zones=zones,
        )
        levels = compute_grid(params)
        placed = skipped = failed = 0
        for lvl in levels:
            ok = await self._place_level(lvl.side, lvl.price, lvl.size, lvl.reason)
            if ok is True:
                placed += 1
            elif ok is False:
                failed += 1
            else:   # None → skipped by filter
                skipped += 1
        self._placed += placed
        logger.info("grid placed",
                    symbol=self._config.symbol,
                    placed=placed, skipped=skipped, failed=failed,
                    tracked=len(self._state))

    async def _place_level(
        self, side: str, price: float, size: float, reason: str
    ) -> Optional[bool]:
        """
        Place one limit order with filter validation + rounding.

        Returns True on success, False on exchange rejection, None if the
        level was skipped BEFORE calling the exchange (sub-minimum qty or
        notional). This lets the caller tally each bucket separately.
        """
        price = round_to_tick(price, self._filters.tick_size)
        size = round_to_step(size, self._filters.step_size)
        if size <= 0 or size < self._filters.min_qty:
            logger.warning("level skipped: qty below min_qty",
                           side=side, price=price, size=size,
                           min_qty=self._filters.min_qty)
            return None
        if not meets_min_notional(price, size, self._filters.min_notional):
            logger.warning("level skipped: below min_notional",
                           side=side, price=price, size=size,
                           notional=price * size,
                           min_notional=self._filters.min_notional)
            return None

        # Risk gate runs AFTER filter checks so we don't burn the
        # max-open-orders budget on levels that would fail at the exchange
        # anyway. Order matters: filters → risk → place.
        if self._risk is not None:
            blocked = self._risk.check_can_place(self._config.symbol, side, price, size)
            if blocked:
                self._blocked_by_risk += 1
                logger.warning("level blocked by risk",
                               side=side, price=price, size=size, reason=blocked)
                return None

        try:
            order = await self._exchange.place_order(
                symbol=self._config.symbol,
                side=OrderSide(side),
                order_type=OrderType.LIMIT,
                quantity=size,
                price=price,
            )
            self._state.add_order(order.order_id, side, price, size)
            if self._risk is not None:
                self._risk.record_open(order.order_id)
            logger.debug("level placed",
                         order_id=order.order_id,
                         side=side, price=price, size=size, reason=reason)
            return True
        except Exception as e:
            logger.error("level placement failed",
                         side=side, price=price, size=size, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Fill handling
    # ------------------------------------------------------------------

    async def _handle_filled(self, order_id: str, event: dict) -> None:
        original = self._state.remove_order(order_id)
        if not original:
            # Unknown order — duplicate delivery, or an order we never
            # tracked (e.g. manual trade from another client). Either way
            # we do nothing. This is the idempotency guarantee.
            return

        self._filled += 1
        if self._risk is not None:
            # Use Binance's executed price (`L`) and qty (`l`) when present
            # — these are the actual trade prints. Fall back to our tracked
            # values if the event is missing them (shouldn't happen).
            fill_price = float(event.get("L", original.price) or original.price)
            fill_qty = float(event.get("l", original.size) or original.size)
            self._risk.record_closed(order_id)
            self._risk.record_fill(self._config.symbol, original.side, fill_price, fill_qty)
        logger.info("fill",
                    order_id=order_id,
                    side=original.side,
                    price=original.price,
                    size=original.size)

        # Replacement: mirror across grid center-spacing.
        # We use the CURRENT configured spacing rather than the original
        # offset, so that post-recenter fills snap to the new grid.
        spacing = self._state.center_price * self._config.base_spacing_pct / 100.0
        if original.side == "BUY":
            new_side = "SELL"
            new_price = original.price + spacing
        else:
            new_side = "BUY"
            new_price = original.price - spacing

        await self._place_level(new_side, new_price, original.size, reason="replacement")

    # ------------------------------------------------------------------
    # Drift monitor + recenter
    # ------------------------------------------------------------------

    async def _drift_monitor(self, stop_event: asyncio.Event) -> None:
        """
        Periodically check whether price has drifted past the recenter
        threshold. If it has, cancel the old grid and build a new one
        around the fresh center.
        """
        while not stop_event.is_set():
            try:
                await asyncio.sleep(self._config.drift_check_interval_s)
            except asyncio.CancelledError:
                return

            try:
                current = await self._exchange.get_ticker_price(self._config.symbol)
            except Exception as e:
                # Transient ticker error — log and try again next tick.
                logger.warning("drift ticker fetch failed", error=str(e))
                continue

            center = self._state.center_price
            if center <= 0:
                continue
            drift_pct = abs(current - center) / center * 100.0
            if drift_pct >= self._config.recenter_threshold_pct:
                logger.info("drift threshold hit — recentering",
                            old_center=center,
                            new_center=current,
                            drift_pct=round(drift_pct, 3))
                try:
                    await self._recenter(current)
                except Exception as e:
                    logger.error("recenter failed", error=str(e))

    async def _recenter(self, new_center: float) -> None:
        # Snapshot ids BEFORE clearing — we need to release them from the
        # risk ledger immediately so the new grid doesn't hit the open-
        # orders cap waiting for CANCELED events to drift in over the WS.
        ids_to_release = list(self._state.orders.keys())
        await self._exchange.cancel_all_orders(self._config.symbol)
        self._state.clear()
        if self._risk is not None:
            for oid in ids_to_release:
                self._risk.record_closed(oid)
        self._state.center_price = new_center
        zones = await self._compute_zones_safely()
        await self._place_grid(new_center, zones)
        self._recentered += 1

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """
        Cancel every open order for the symbol. MUST succeed or at least
        be attempted — leaving orders dangling is the single worst outcome
        on shutdown. Even if cancel_all fails we log and re-raise so the
        operator sees a loud error.
        """
        ids_to_release = list(self._state.orders.keys())
        try:
            n = await self._exchange.cancel_all_orders(self._config.symbol)
            self._state.clear()
            if self._risk is not None:
                for oid in ids_to_release:
                    self._risk.record_closed(oid)
            logger.info("engine shutdown",
                        symbol=self._config.symbol,
                        cancelled=n,
                        placed_total=self._placed,
                        filled_total=self._filled,
                        recentered_total=self._recentered,
                        blocked_by_risk=self._blocked_by_risk)
        except Exception:
            logger.exception("SHUTDOWN cancel_all FAILED — check exchange for dangling orders",
                             symbol=self._config.symbol)
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _compute_zones_safely(self):
        """Fetch S&D zones if a signal is wired. Never raise — the grid
        must still place even if the signal fails."""
        if not self._signal:
            return None
        try:
            result = await self._signal.compute(self._config.symbol)
            return result.zones
        except Exception as e:
            logger.warning("zone compute failed — placing unbiased grid",
                           error=str(e))
            return None

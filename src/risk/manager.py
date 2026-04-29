"""
RiskManager — the guardrail layer between strategy and exchange.

WHY this layer exists:
Phase 5 placed real orders end-to-end. A single bug in the engine — a
runaway placement loop, a recenter that doesn't cancel, a fill handler
that doubles up — could carpet the order book or quietly drain the
account. The strategy layer is the wrong place to enforce safety: it
already has too many concerns, and a check inside the layer being
guarded provides no protection if the layer itself misbehaves.

So safety lives here, owned by a single object that:
  1. Tracks open orders globally (so a cap applies across all symbols).
  2. Tracks realized PnL via FIFO inventory matching (so a sustained
     losing run trips a circuit breaker).
  3. Watches a sentinel file (`runtime/HALT`) so an operator can halt
     the bot from outside the process without sending SIGINT.

Halt is STICKY: once tripped, the manager refuses placements until the
runner explicitly clears it (typically by exiting). The kill switch is a
one-way trapdoor on purpose — auto-recovery from a tripped safety check
is exactly the wrong default.

DESIGN NOTES
* RiskManager is shared across engines (one instance, passed to each).
  This is why it lives in `src/risk/`, not `src/grid/`.
* The manager never CALLS the exchange — it only authorises or denies.
  The runner's monitor task is what flips stop_event when halt fires.
* PnL accounting is FIFO per symbol. Grid bots have no notion of "the
  matching buy" for a given sell, so we use the standard inventory rule:
  each SELL fill consumes the oldest outstanding BUY. This is the same
  accounting pattern used by exchanges for spot tax reporting.
* Pre-existing inventory (from before the bot started) is invisible. We
  only see fills we observe, so PnL is "PnL since this process started"
  — not "lifetime PnL on the account". Good enough for a circuit breaker.
"""

import collections
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.notifications.alerter import Alerter, NullAlerter
from src.risk.config import RiskConfig
from src.utils.logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class _Lot:
    """One outstanding BUY waiting to be matched against a future SELL."""
    price: float
    qty: float


class RiskManager:
    """
    Thread-safe guardrail. Engines consult `check_can_place(...)` before
    every placement and call `record_fill(...)` after every fill.

    USAGE:
        risk = RiskManager(RiskConfig.from_yaml(settings))
        # in engine:
        if (reason := risk.check_can_place(symbol, side, price, qty)):
            logger.warning("placement blocked", reason=reason)
            return None
        order = await exchange.place_order(...)
        risk.record_open(order.order_id)
        # later, on fill:
        risk.record_fill(symbol, side, fill_price, fill_qty)
        risk.record_closed(order_id)
    """

    def __init__(self, config: RiskConfig, alerter: Optional[Alerter] = None):
        self._cfg = config
        # Lock protects every mutable field. on_fill arrives on the WS
        # dispatch task; check_can_place runs on engine tasks. Multiple
        # engines run concurrently. A single coarse lock is fine — these
        # operations are O(small) and not on a hot path.
        self._lock = threading.Lock()
        self._open_order_ids: set[str] = set()
        # Per-symbol FIFO queue of outstanding BUY lots.
        self._inventory: dict[str, collections.deque[_Lot]] = collections.defaultdict(
            collections.deque
        )
        # Realized PnL ledger: list of (timestamp, usd_delta).
        # Trimmed to the last 24h on every read so the daily window is rolling.
        self._pnl_ledger: list[tuple[float, float]] = []
        # Sticky halt state. Once True, stays True for the life of this
        # manager. The runner is expected to flip stop_event and exit.
        self._halted = False
        self._halt_reason: str = ""
        # Alerter is shared with the rest of the bot; trip() emits a [HALT]
        # alert. Default NullAlerter keeps existing tests / CLI rehearsals
        # working with no behaviour change.
        self._alerter: Alerter = alerter or NullAlerter()

    # ------------------------------------------------------------------
    # Halt state
    # ------------------------------------------------------------------

    def trip(self, reason: str) -> None:
        """
        Trip the kill switch. Idempotent — first reason wins so post-mortem
        sees the actual cause, not a downstream symptom.
        """
        with self._lock:
            if self._halted:
                return
            self._halted = True
            self._halt_reason = reason
            # Snapshot for the alert while we still hold the lock — avoids
            # tearing if another thread reads/writes between unlock and emit.
            realized_24h = self._realized_pnl_locked()
            open_count = len(self._open_order_ids)
        logger.error("RISK HALT", reason=reason)
        # Fire-and-forget; the alerter.halt contract is sync + non-blocking.
        self._alerter.halt(
            reason=reason,
            realized_24h_usd=realized_24h,
            daily_loss_limit_usd=self._cfg.daily_loss_limit_usd,
            open_orders=open_count,
        )

    @property
    def halted(self) -> bool:
        with self._lock:
            return self._halted

    @property
    def halt_reason(self) -> str:
        with self._lock:
            return self._halt_reason

    def poll_kill_switch(self) -> bool:
        """
        Check the sentinel file. Returns True if the halt was newly tripped
        by this call. Safe to call repeatedly — sticky-halt semantics make
        the second call a no-op.
        """
        if self.halted:
            return False
        if pathlib.Path(self._cfg.kill_switch_path).exists():
            self.trip(f"kill switch file present: {self._cfg.kill_switch_path}")
            return True
        return False

    # ------------------------------------------------------------------
    # Pre-placement check
    # ------------------------------------------------------------------

    def check_can_place(
        self, symbol: str, side: str, price: float, qty: float
    ) -> str | None:
        """
        Pre-flight check. Returns None if placement is allowed; otherwise a
        short string describing why it's blocked. The engine logs and skips
        the level when blocked — the order is never sent to the exchange.

        Side-effect free: this method does NOT increment any counter. The
        engine must call record_open(order_id) AFTER a successful placement.
        Splitting the check from the recording avoids over-counting orders
        that the exchange ultimately rejects.
        """
        with self._lock:
            if self._halted:
                return f"halted: {self._halt_reason}"
            if len(self._open_order_ids) >= self._cfg.max_open_orders:
                return (
                    f"max_open_orders cap reached "
                    f"({len(self._open_order_ids)} >= {self._cfg.max_open_orders})"
                )
            # Daily loss check uses the current ledger; trim outside the
            # placement path is fine since record_fill trims on write.
            realized = self._realized_pnl_locked()
            if realized <= -self._cfg.daily_loss_limit_usd:
                return (
                    f"daily loss floor breached "
                    f"({realized:.2f} <= -{self._cfg.daily_loss_limit_usd:.2f})"
                )
        return None

    # ------------------------------------------------------------------
    # Open-orders accounting
    # ------------------------------------------------------------------

    def record_open(self, order_id: str) -> None:
        """Mark an order as in-flight. Idempotent."""
        with self._lock:
            self._open_order_ids.add(order_id)

    def record_closed(self, order_id: str) -> None:
        """Mark an order as no-longer-in-flight (fill or cancel). Idempotent."""
        with self._lock:
            self._open_order_ids.discard(order_id)

    @property
    def open_order_count(self) -> int:
        with self._lock:
            return len(self._open_order_ids)

    # ------------------------------------------------------------------
    # PnL accounting (FIFO)
    # ------------------------------------------------------------------

    def record_fill(
        self, symbol: str, side: str, price: float, qty: float
    ) -> Optional[float]:
        """
        Record a fill against the FIFO inventory book.

        BUY fills push a lot onto the queue.
        SELL fills consume from the front, accumulating realized PnL as
        (sell_price - buy_price) * matched_qty for each matched lot.

        Sells that exceed inventory (e.g. pre-existing balance, or if we
        missed observing a buy) consume what's available and discard the
        rest — we don't manufacture phantom buys at $0.

        Returns the realized PnL delta in USD for SELL fills (None for
        BUY). The engine forwards this to the [FILL] alert so the operator
        sees direction at a glance without re-running FIFO matching.
        """
        if qty <= 0:
            return None
        side_upper = side.upper()
        with self._lock:
            inv = self._inventory[symbol]
            if side_upper == "BUY":
                inv.append(_Lot(price=price, qty=qty))
                return None
            if side_upper != "SELL":
                return None
            remaining = qty
            realized_delta = 0.0
            while remaining > 0 and inv:
                lot = inv[0]
                matched = min(lot.qty, remaining)
                realized_delta += (price - lot.price) * matched
                lot.qty -= matched
                remaining -= matched
                if lot.qty <= 0:
                    inv.popleft()
            # Unmatched SELL qty (remaining > 0) means we sold inventory we
            # never observed buying. We log it and move on — counting it as
            # a windfall vs zero would inflate PnL.
            if remaining > 0:
                logger.warning("sell exceeded tracked inventory",
                               symbol=symbol,
                               unmatched_qty=remaining,
                               sell_price=price)
            if realized_delta != 0.0:
                self._pnl_ledger.append((time.time(), realized_delta))
                self._trim_ledger_locked()
                logger.info("pnl",
                            symbol=symbol,
                            realized_delta=round(realized_delta, 6),
                            realized_24h=round(self._realized_pnl_locked(), 6))
        return realized_delta

    @property
    def realized_pnl_24h(self) -> float:
        """Sum of realized PnL deltas in the trailing 24h window."""
        with self._lock:
            self._trim_ledger_locked()
            return self._realized_pnl_locked()

    # ------------------------------------------------------------------
    # Internal helpers (caller must hold the lock)
    # ------------------------------------------------------------------

    def _trim_ledger_locked(self) -> None:
        cutoff = time.time() - 24 * 3600
        # Ledger is append-only and time-ordered, so a forward scan is fine.
        i = 0
        for ts, _ in self._pnl_ledger:
            if ts >= cutoff:
                break
            i += 1
        if i:
            del self._pnl_ledger[:i]

    def _realized_pnl_locked(self) -> float:
        return sum(delta for _, delta in self._pnl_ledger)

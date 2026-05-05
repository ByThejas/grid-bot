"""
balance_poller.py — Phase 11D dynamic sizing.

WHY this exists
The bot's order_size and daily_loss_limit_usd are operator-set fixed
values. At small wallet sizes that's fine, but as the wallet grows (or
shrinks) the operator has to manually edit settings.yaml to keep risk
proportional. This module computes both as percentages of current wallet
equity and refreshes periodically — a wallet that 10x's also 10x's its
order sizes and daily floor automatically.

ARCHITECTURE
  BalancePollerBase    — interface every component talks to
  NullBalancePoller    — returns static-config values (when dynamic sizing disabled)
  BalancePoller        — live polling implementation
  make_balance_poller  — factory that picks one based on settings

Pure functions at the top of the module (compute_equity_usdt,
compute_target_notional, etc.) do the math without I/O. Easy to test
against canned inputs without spinning up an exchange or async task.

THE FAIL-CLOSED INVARIANT
If the FIRST equity poll fails (bot can't reach Binance at startup),
start() raises and the runner exits with code 4. systemd restarts.
Without equity we cannot compute sizes; better to retry than to guess.

After the first successful poll, subsequent failures are tolerated up to
N consecutive failures (configurable). During that window the
last-known-good equity is used. After N failures, fires a [PAUSE] alert
(`source=balance_poller`). When polls succeed again, fires [RESUME].
"""

import asyncio
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from src.exchange.base import ExchangeBase
from src.notifications.alerter import Alerter, NullAlerter
from src.utils.logger import get_logger

logger = get_logger("balance_poller")


# Tracked symbols. Phase 11D ships supporting BTC + ETH only — same as the
# bot's actual trading pairs. Other altcoins held in the wallet are NOT
# counted toward equity (BNB might be held for fee discounts; USDC has
# peg / bridging quirks; etc.). If we ever add a third trading pair, this
# tuple grows alongside `_TRACKED_PAIRS`.
_TRACKED_BASE_ASSETS: tuple[str, ...] = ("BTC", "ETH")
_TRACKED_PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BalancePollerConfig:
    """
    Parsed settings.yaml `dynamic_sizing` block. All knobs have defaults
    that match the design doc.
    """
    enabled: bool = False
    poll_interval_seconds: float = 1800.0           # 30 min
    order_size_pct: float = 0.5                     # 0.5% of equity per level
    daily_loss_pct: float = 5.0                     # 5% of equity per UTC day
    max_order_size_pct: Optional[float] = None      # cap; None = no cap
    flash_crash_pause_pct: float = 20.0             # equity drop threshold
    flash_crash_window_seconds: float = 3600.0      # rolling lookback
    failover_warn_after_consecutive_failures: int = 3

    @classmethod
    def from_yaml(cls, raw: Optional[dict]) -> "BalancePollerConfig":
        """
        Build from settings.yaml's `dynamic_sizing` block. Missing block
        or empty dict → fully-default config with enabled=False (the
        backward-compatible "do nothing" path).
        """
        raw = raw or {}
        max_pct = raw.get("max_order_size_pct")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            poll_interval_seconds=float(
                raw.get("poll_interval_minutes", 30)
            ) * 60.0,
            order_size_pct=float(raw.get("order_size_pct", 0.5)),
            daily_loss_pct=float(raw.get("daily_loss_pct", 5.0)),
            max_order_size_pct=(
                float(max_pct) if max_pct is not None else None
            ),
            flash_crash_pause_pct=float(raw.get("flash_crash_pause_pct", 20.0)),
            flash_crash_window_seconds=float(
                raw.get("flash_crash_window_minutes", 60)
            ) * 60.0,
            failover_warn_after_consecutive_failures=int(
                raw.get("failover_warn_after_consecutive_failures", 3)
            ),
        )


@dataclass(frozen=True)
class _Snapshot:
    """One equity reading. Internal — held in a rolling history list."""
    timestamp: float
    equity_usdt: float


# ---------------------------------------------------------------------------
# Pure functions — no I/O, no async, easy to test
# ---------------------------------------------------------------------------


def compute_equity_usdt(
    balances: dict[str, float], prices: dict[str, float],
) -> float:
    """
    Sum of USDT-equivalent value for tracked assets.

      balances : {'USDT': 95.36, 'BTC': 0.0008, 'ETH': 0.013}  (free+locked)
      prices   : {'BTCUSDT': 76000.0, 'ETHUSDT': 2250.0}

    Raises ValueError if a tracked base asset has a positive balance but
    we don't have a price for it — that's a "can't compute" condition,
    not a default-to-zero case.
    """
    eq = float(balances.get("USDT", 0.0))
    for asset in _TRACKED_BASE_ASSETS:
        amount = float(balances.get(asset, 0.0))
        if amount <= 0:
            continue
        pair = asset + "USDT"
        if pair not in prices:
            raise ValueError(
                f"missing price for {pair} but wallet has {amount} {asset}"
            )
        eq += amount * float(prices[pair])
    return eq


def compute_target_notional(
    equity_usdt: float,
    order_size_pct: float,
    max_order_size_pct: Optional[float] = None,
) -> float:
    """
    USDT notional we want each grid level to be.

    Optional cap via max_order_size_pct — useful when the wallet grows
    fast (deposit, fortunate run of fills) and the operator doesn't want
    risk to scale linearly past a point.
    """
    target = equity_usdt * (order_size_pct / 100.0)
    if max_order_size_pct is not None:
        cap = equity_usdt * (max_order_size_pct / 100.0)
        target = min(target, cap)
    return max(target, 0.0)


def compute_order_size_in_base(
    target_notional_usdt: float,
    mid_price: float,
    step_size: float,
    min_notional: float,
) -> float:
    """
    Convert USDT notional → base-asset quantity.

    Rounds DOWN to step_size for the normal case. If the resulting order
    would fall below the exchange's min_notional, floors at
    min_notional / mid_price (rounded UP to step_size) so very small
    wallets still trade — at the minimum-allowed size — rather than
    producing zero-quantity orders that all fail.
    """
    if mid_price <= 0:
        raise ValueError(f"mid_price must be positive, got {mid_price}")
    if step_size <= 0:
        raise ValueError(f"step_size must be positive, got {step_size}")
    qty = target_notional_usdt / mid_price
    qty_rounded = math.floor(qty / step_size) * step_size
    if qty_rounded * mid_price >= min_notional:
        return qty_rounded
    # Floor at min_notional. Round UP to step_size this time.
    floor_qty = math.ceil((min_notional / mid_price) / step_size) * step_size
    return floor_qty


def compute_daily_loss_limit_usd(
    equity_usdt: float, daily_loss_pct: float,
) -> float:
    """Daily loss floor as USDT-equivalent. Just a percentage of equity."""
    return max(0.0, equity_usdt * (daily_loss_pct / 100.0))


def is_equity_flash_crashed(
    history: list[_Snapshot],
    threshold_pct: float,
    window_seconds: float,
    now: float,
) -> bool:
    """
    True if the most recent equity is more than threshold_pct below the
    PEAK equity inside the trailing window_seconds.

    Compares to peak (not earliest) so a slow drift down doesn't trigger;
    only a fast drop from a recent high does. Needs at least 2 snapshots
    in the window to draw a conclusion.
    """
    if not history or threshold_pct <= 0:
        return False
    cutoff = now - window_seconds
    recent = [s for s in history if s.timestamp >= cutoff]
    if len(recent) < 2:
        return False
    latest_equity = recent[-1].equity_usdt
    peak_equity = max(s.equity_usdt for s in recent)
    if peak_equity <= 0:
        return False
    drop_pct = (peak_equity - latest_equity) / peak_equity * 100.0
    return drop_pct >= threshold_pct


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class BalancePollerBase(ABC):
    """
    Interface that GridEngine + RiskManager + HeartbeatMonitor talk to.
    Live implementation polls Binance; null implementation returns
    static-config values. Engine and risk code don't know which is wired.
    """

    @abstractmethod
    async def start(self) -> None:
        """Begin polling. Live impls perform the first refresh inline."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the polling task. Idempotent."""

    @abstractmethod
    def order_size_in_base(
        self,
        symbol: str,
        mid_price: float,
        step_size: float,
        min_notional: float,
    ) -> float:
        """Per-level order size in base-asset units, rounded to step_size."""

    @abstractmethod
    def daily_loss_limit_usd(self) -> float:
        """Current absolute daily-loss floor in USDT."""

    @abstractmethod
    def is_flash_crashed(self) -> bool:
        """
        True when equity has dropped > flash_crash_pause_pct in the
        configured window. Engine reads this on each placement and skips
        placement while True.
        """

    @abstractmethod
    def current_equity_usdt(self) -> Optional[float]:
        """Last-known-good equity. None if no successful poll yet."""


# ---------------------------------------------------------------------------
# Null implementation — backward compatibility path
# ---------------------------------------------------------------------------


class NullBalancePoller(BalancePollerBase):
    """
    Returns the existing static-config values verbatim. No polling, no
    equity tracking, no async tasks. Used when dynamic_sizing.enabled is
    False or the block is absent — preserves pre-Phase-11D behavior
    exactly.
    """

    def __init__(
        self,
        static_order_sizes_in_base: dict[str, float],
        static_daily_loss_limit_usd: float,
    ):
        self._sizes = dict(static_order_sizes_in_base)
        self._floor = float(static_daily_loss_limit_usd)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def order_size_in_base(
        self, symbol: str, mid_price: float, step_size: float, min_notional: float,
    ) -> float:
        # Static path: ignore market-side args, return the configured value.
        return self._sizes.get(symbol, 0.0)

    def daily_loss_limit_usd(self) -> float:
        return self._floor

    def is_flash_crashed(self) -> bool:
        return False

    def current_equity_usdt(self) -> Optional[float]:
        # No equity tracking in null mode — heartbeat/dashboard see None.
        return None


# ---------------------------------------------------------------------------
# Live implementation
# ---------------------------------------------------------------------------


class BalancePoller(BalancePollerBase):
    """
    Live polling implementation. Refreshes equity from Binance at the
    configured interval, keeps a rolling history for flash-crash
    detection, exposes current values via the BalancePollerBase
    interface.

    USAGE (in grid_runner):
        poller = BalancePoller(exchange=exchange, config=cfg, alerter=alerter)
        await poller.start()    # also performs the first refresh — raises on failure
        # ... pass to engine + risk ...
        await poller.stop()     # at shutdown
    """

    def __init__(
        self,
        exchange: ExchangeBase,
        config: BalancePollerConfig,
        alerter: Optional[Alerter] = None,
        now_fn=time.time,
    ):
        self._exchange = exchange
        self._cfg = config
        self._alerter: Alerter = alerter or NullAlerter()
        self._now = now_fn

        self._task: Optional[asyncio.Task] = None
        self._latest: Optional[_Snapshot] = None
        self._history: list[_Snapshot] = []
        self._consecutive_failures: int = 0
        self._failover_alert_active: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Perform the first refresh inline (raises on failure — caller
        decides whether to abort the bot), then spawn the periodic
        polling task.
        """
        await self._refresh_once()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name="balance_poller",
            )
        logger.info(
            "balance poller started",
            interval_s=self._cfg.poll_interval_seconds,
            initial_equity=round(self._latest.equity_usdt, 2)
                if self._latest else None,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("balance poller stopped")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._cfg.poll_interval_seconds)
            except asyncio.CancelledError:
                return
            try:
                await self._refresh_once()
            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(
                    "balance poll failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    consecutive=self._consecutive_failures,
                )
                if (
                    self._consecutive_failures
                    >= self._cfg.failover_warn_after_consecutive_failures
                    and not self._failover_alert_active
                ):
                    self._alerter.pause(
                        source="balance_poller", symbol="*",
                        consecutive_failures=str(self._consecutive_failures),
                    )
                    self._failover_alert_active = True
            else:
                if self._consecutive_failures > 0:
                    logger.info(
                        "balance poll recovered",
                        prior_failures=self._consecutive_failures,
                    )
                self._consecutive_failures = 0
                if self._failover_alert_active:
                    self._alerter.resume(
                        source="balance_poller", symbol="*",
                    )
                    self._failover_alert_active = False

    async def _refresh_once(self) -> None:
        """
        One round of: fetch balances, fetch prices, compute equity, append
        to history, prune old. Raises on failure — caller decides.
        """
        balances = await self._exchange.get_account_balances()
        prices: dict[str, float] = {}
        for symbol in _TRACKED_PAIRS:
            prices[symbol] = await self._exchange.get_ticker_price(symbol)

        equity = compute_equity_usdt(balances, prices)
        if equity <= 0:
            raise ValueError(f"computed non-positive equity: {equity}")

        snapshot = _Snapshot(timestamp=self._now(), equity_usdt=equity)
        self._latest = snapshot
        self._history.append(snapshot)
        # Keep history at 2× the flash-crash window so detection has
        # enough data even when polls are dropped.
        cutoff = self._now() - (2.0 * self._cfg.flash_crash_window_seconds)
        self._history = [s for s in self._history if s.timestamp >= cutoff]

        logger.info(
            "balance refreshed",
            equity_usdt=round(equity, 2),
            history_len=len(self._history),
        )

    # ------------------------------------------------------------------
    # Accessors (BalancePollerBase implementation)
    # ------------------------------------------------------------------

    def order_size_in_base(
        self,
        symbol: str,
        mid_price: float,
        step_size: float,
        min_notional: float,
    ) -> float:
        if self._latest is None:
            return 0.0  # shouldn't happen in production — start() ensures a snapshot
        target_notional = compute_target_notional(
            self._latest.equity_usdt,
            self._cfg.order_size_pct,
            self._cfg.max_order_size_pct,
        )
        return compute_order_size_in_base(
            target_notional, mid_price, step_size, min_notional,
        )

    def daily_loss_limit_usd(self) -> float:
        if self._latest is None:
            return 0.0
        return compute_daily_loss_limit_usd(
            self._latest.equity_usdt, self._cfg.daily_loss_pct,
        )

    def is_flash_crashed(self) -> bool:
        return is_equity_flash_crashed(
            self._history,
            self._cfg.flash_crash_pause_pct,
            self._cfg.flash_crash_window_seconds,
            self._now(),
        )

    def current_equity_usdt(self) -> Optional[float]:
        return self._latest.equity_usdt if self._latest else None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_balance_poller(
    settings: dict,
    exchange: ExchangeBase,
    alerter: Alerter,
    static_order_sizes_in_base: dict[str, float],
    static_daily_loss_limit_usd: float,
) -> BalancePollerBase:
    """
    Build the right poller for the configured mode. Disabled or absent
    `dynamic_sizing` block → NullBalancePoller (returns static config).
    Enabled → live BalancePoller.

    Always returns a working poller — never raises. Misconfiguration
    falls back to the null path with a logged warning.
    """
    cfg = BalancePollerConfig.from_yaml(settings.get("dynamic_sizing"))
    if not cfg.enabled:
        logger.info(
            "dynamic sizing disabled — using static order sizes",
            symbols=len(static_order_sizes_in_base),
        )
        return NullBalancePoller(
            static_order_sizes_in_base=static_order_sizes_in_base,
            static_daily_loss_limit_usd=static_daily_loss_limit_usd,
        )
    logger.info(
        "dynamic sizing enabled",
        order_size_pct=cfg.order_size_pct,
        daily_loss_pct=cfg.daily_loss_pct,
        poll_interval_s=cfg.poll_interval_seconds,
    )
    return BalancePoller(exchange=exchange, config=cfg, alerter=alerter)

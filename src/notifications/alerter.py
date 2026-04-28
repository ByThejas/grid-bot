"""
alerter.py — the contract that every alert-emitting component talks to.

WHY AN ABC AND A NULL IMPLEMENTATION
The bot must keep trading even when alerts are disabled, misconfigured, or
broken. The cleanest way to guarantee that is a small abstract contract
plus a `NullAlerter` that no-ops every method. Components import only
`Alerter` and don't know whether the live one or the null one is wired in;
tests construct the null implementation and exercise components with zero
network or async machinery.

THE FIRE-AND-FORGET INVARIANT
Every event method on `Alerter` is synchronous and MUST return immediately
without awaiting I/O. Live implementations enqueue work onto an internal
queue; the actual HTTP call to Telegram happens on a separate async task.
This is why `RiskManager.trip()` (synchronous, may be called from any
thread) can call `alerter.halt(...)` safely: there's no await, no lock
contention, no chance of blocking the trading hot path.

`start()` and `stop()` ARE async because they spin up / tear down the
sender and listener tasks (in the live impl). The null impl makes them
no-ops so callers can `await` them uniformly.

FACTORY DECISION TABLE (make_alerter)
  telegram section absent             → NullAlerter (back-compat)
  telegram.enabled = false            → NullAlerter
  TELEGRAM_BOT_TOKEN missing/empty    → NullAlerter + warn
  TELEGRAM_ALLOWED_CHAT_IDS missing   → NullAlerter + warn
  all conditions met                  → TelegramAlerter (added in step 4)

The factory always returns a working alerter — never raises — so a
broken alerts config can never prevent the bot from booting.
"""

import os
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

from src.utils.logger import get_logger

logger = get_logger("alerter")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class Alerter(ABC):
    """
    Interface every component talks to. Methods are synchronous and
    fire-and-forget — they must never block the caller. Live implementations
    enqueue and return; failure to send is the alerter's problem, not the
    caller's.

    Method-set mirrors the event taxonomy in
    `docs/design/phase_11a_telegram.md` §3 and the formatters in
    `src.notifications.format`.
    """

    # -- lifecycle -----------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Start any background tasks. Idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down background tasks. Idempotent. Pending alerts are dropped."""

    # -- events --------------------------------------------------------

    @abstractmethod
    def boot(
        self,
        *,
        env_mode: str,
        symbols: Iterable[str],
        max_open_orders: int,
        daily_loss_limit_usd: float,
    ) -> None: ...

    @abstractmethod
    def shutdown(
        self,
        *,
        final_open_orders: int,
        session_realized_usd: float,
        uptime_seconds: float,
    ) -> None: ...

    @abstractmethod
    def crash(self, *, component: str, exc: BaseException) -> None: ...

    @abstractmethod
    def fill(
        self,
        *,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        realized_delta_usd: Optional[float] = None,
    ) -> None: ...

    @abstractmethod
    def halt(
        self,
        *,
        reason: str,
        realized_24h_usd: Optional[float] = None,
        daily_loss_limit_usd: Optional[float] = None,
        open_orders: Optional[int] = None,
    ) -> None: ...

    @abstractmethod
    def pause(self, *, source: str, symbol: str, **fields: str) -> None: ...

    @abstractmethod
    def resume(self, *, source: str, symbol: str, **fields: str) -> None: ...

    @abstractmethod
    def daily_summary(
        self,
        *,
        utc_day: str,
        fills: int,
        realized_usd: float,
        recenters: int,
        halts: int,
        uptime_seconds: float,
    ) -> None: ...

    @abstractmethod
    def recenter(
        self, *, symbol: str, old_mid: float, new_mid: float, drift_pct: float,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Null implementation
# ---------------------------------------------------------------------------


class NullAlerter(Alerter):
    """
    Every method is a no-op. This is what callers receive when alerts are
    disabled or misconfigured — there's no fallback queue, no swallowed
    network code, just zero work.
    """

    async def start(self) -> None:  # noqa: D401 - intentional no-op
        return None

    async def stop(self) -> None:
        return None

    def boot(self, **_: Any) -> None: return None
    def shutdown(self, **_: Any) -> None: return None
    def crash(self, **_: Any) -> None: return None
    def fill(self, **_: Any) -> None: return None
    def halt(self, **_: Any) -> None: return None
    def pause(self, **_: Any) -> None: return None
    def resume(self, **_: Any) -> None: return None
    def daily_summary(self, **_: Any) -> None: return None
    def recenter(self, **_: Any) -> None: return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _telegram_section(settings: dict) -> dict:
    """Return the telegram block (or an empty dict). Never None."""
    return dict(settings.get("telegram", {}) or {})


def _parse_chat_ids(raw: str) -> list[int]:
    """
    Parse the comma-separated `TELEGRAM_ALLOWED_CHAT_IDS` value.

    Quietly drops empty entries and non-integer fragments — we'd rather
    boot with a slightly-shorter allow-list than fail on a typo. Anything
    we drop is logged at WARN level.
    """
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            logger.warning("ignoring malformed chat id in TELEGRAM_ALLOWED_CHAT_IDS",
                           value=piece)
    return out


def make_alerter(settings: dict) -> Alerter:
    """
    Build the right Alerter for the current configuration. Never raises;
    a broken alerts config returns a NullAlerter and logs why.

    Decision order matches the docstring decision table at the top of
    this module.
    """
    cfg = _telegram_section(settings)

    if not cfg:
        # No telegram block in settings.yaml at all → backward compat mode.
        # The bot has been shipping without alerts for ten phases; absence
        # of the section keeps that behavior.
        logger.info("telegram section absent in settings; alerts disabled")
        return NullAlerter()

    if not bool(cfg.get("enabled", False)):
        logger.info("telegram.enabled is false; alerts disabled")
        return NullAlerter()

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning(
            "telegram.enabled=true but TELEGRAM_BOT_TOKEN is missing/empty in env; "
            "falling back to NullAlerter (bot will trade, no Telegram alerts)"
        )
        return NullAlerter()

    chat_ids_raw = (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    chat_ids = _parse_chat_ids(chat_ids_raw)
    if not chat_ids:
        logger.warning(
            "telegram.enabled=true but TELEGRAM_ALLOWED_CHAT_IDS is missing/empty; "
            "falling back to NullAlerter (no chat to send to, no chat to listen from)"
        )
        return NullAlerter()

    # All preconditions met. The live TelegramAlerter lands in step 4 of
    # Phase 11A. Until then, log a clear warning and return NullAlerter so
    # the bot keeps running. This branch is unreachable in CI tests today
    # because no test env sets a real bot token.
    try:
        from src.notifications.telegram import TelegramAlerter  # noqa: F401
    except ImportError:
        logger.warning(
            "telegram alerts requested but TelegramAlerter not yet implemented; "
            "falling back to NullAlerter"
        )
        return NullAlerter()

    return TelegramAlerter(
        bot_token=token,
        allowed_chat_ids=chat_ids,
        config=cfg,
    )

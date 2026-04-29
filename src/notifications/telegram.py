"""
telegram.py — `TelegramAlerter`: live Alerter implementation that sends
formatted messages to Telegram via the Bot API.

WHAT THIS FILE OWNS (Phase 11A step 5)
This step adds the SENDER side only. Every Alerter event method
(`boot`, `fill`, `halt`, ...) formats its arguments via `src.notifications.format`
and enqueues the resulting string onto a bounded `asyncio.Queue`. A
single background task (`_sender_loop`) drains the queue and POSTs each
message to Telegram. The listener task (commands `/status`, `/halt`,
`/help`) lands in step 6 and shares this same class.

THE FIRE-AND-FORGET INVARIANT (cf. alerter.py)
Event methods are sync and MUST return immediately. They do exactly two
things: format the message, then `_enqueue()` it. Any I/O — HTTP, retry,
backoff — happens inside the sender task, never on the caller's stack.
This is why `RiskManager.trip()` (sync, called under a lock from any
thread) can fire `alerter.halt(...)` without risk of deadlock or the
trading hot path waiting on a network round-trip to Telegram.

QUEUE OVERFLOW
The queue is bounded (default 100). When full, `_enqueue` drops the
OLDEST queued message and increments `_dropped_count`. The next
successfully-sent message gets prefixed with
`(N alerts dropped due to queue overflow)`. Rationale: an operator
catching up after a network outage cares about the latest state, not the
earliest stale alerts. Tail-drop preserves freshness; head-drop preserves
ordering. We pick freshness — the alternative is the operator's phone
buzzing about a 30-minute-old [PAUSE] while the actual [HALT] sits behind it.

RETRY POLICY (per design doc §7)
  5xx / network error  → retry with exponential backoff 1s, 2s, 4s, 8s, 16s
                         (capped at max_delay=30s). Five attempts max,
                         then we give up on THAT message and move on.
  4xx (auth, banned)   → set `_broken=True`, drop all further alerts,
                         log loudly. Restart fixes. We do NOT retry —
                         a bad token won't become good in 30 seconds.

GRACEFUL DISABLE
The factory in `alerter.py` only constructs us if (a) telegram.enabled,
(b) TELEGRAM_BOT_TOKEN is non-empty, (c) at least one valid chat id was
parsed. Any of those failing → factory returns NullAlerter. So by the
time we're alive, we have credentials + at least one destination.

WHY aiohttp INSTEAD OF httpx
The design doc default was httpx on the assumption that it was already a
transitive dep of python-binance — it is not. aiohttp IS in
requirements.txt (currently unused; carried for future async HTTP work),
so using it here adds zero new deps. The choice is deliberately not
python-telegram-bot / aiogram — those bring heavy class hierarchies for
features we do not use, and pin us to their release schedule.
"""

import asyncio
from typing import Any, Callable, Iterable, Optional

import aiohttp

from src.notifications.alerter import Alerter
from src.notifications.format import (
    format_boot,
    format_crash,
    format_daily_summary,
    format_fill,
    format_halt,
    format_pause,
    format_recenter,
    format_resume,
    format_shutdown,
)
from src.utils.logger import get_logger

logger = get_logger("telegram_alerter")


# Default toggles for telegram.alert_on. Mirrors the design doc Section 8
# defaults — heartbeat and recenter off, everything else on. The
# operator's settings.yaml block is merged over these in __init__.
_DEFAULT_ALERT_ON: dict[str, bool] = {
    "boot": True,
    "shutdown": True,
    "crash": True,
    "fill": True,
    "halt": True,
    "pause": True,
    "resume": True,
    "heartbeat": False,
    "daily_summary": True,
    "recenter": False,
}

# Sender retry policy. Five attempts, exponential backoff, capped.
# Fixed in code rather than configurable: every other phase has shown
# that knobs the operator never tunes are footguns. If we need to change
# the policy we change the code; YAML stays clean.
_RETRY_INITIAL_DELAY_S: float = 1.0
_RETRY_MAX_DELAY_S: float = 30.0
_RETRY_MAX_ATTEMPTS: int = 5


class TelegramAlerter(Alerter):
    """
    Live alerter that sends formatted alerts to Telegram via the Bot API.

    USAGE (production path runs through the factory):
        alerter = make_alerter(settings)   # returns TelegramAlerter or NullAlerter
        await alerter.start()              # spawns sender task
        alerter.boot(...)                  # sync, fire-and-forget
        ...
        await alerter.stop()               # drains nothing — pending alerts dropped

    Multiple chat IDs: every alert fans out to every id in
    `allowed_chat_ids`. So if the operator wires both their personal chat
    and a shared group, both receive every event.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        bot_token: str,
        allowed_chat_ids: Iterable[int],
        config: Optional[dict] = None,
    ):
        # Token never logged — see _redacted_url for url-formatting that
        # avoids token exposure in tracebacks/error logs.
        self._bot_token = bot_token
        # List, not iterator — we walk it multiple times (once per send).
        self._chat_ids: list[int] = list(allowed_chat_ids)
        cfg = dict(config or {})
        # Per-event toggle map. Defaults filled in for any key the operator
        # didn't override; explicit False wins over default True.
        alert_on_user = dict(cfg.get("alert_on", {}) or {})
        self._alert_on: dict[str, bool] = {**_DEFAULT_ALERT_ON, **alert_on_user}

        self._send_timeout_s: float = float(cfg.get("send_timeout_seconds", 5.0))
        max_q = int(cfg.get("max_queue_size", 100))
        # Bounded queue. asyncio.Queue's put_nowait raises QueueFull on
        # overflow which we catch in _enqueue.
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_q)

        self._session: Optional[aiohttp.ClientSession] = None
        self._sender_task: Optional[asyncio.Task] = None
        # Set when Telegram returns 4xx — we stop retrying and stop
        # accepting new enqueues. Trading continues normally.
        self._broken: bool = False
        # Counter for messages dropped due to queue overflow. The next
        # successful send carries a "(N dropped)" prefix and resets this.
        self._dropped_count: int = 0

    async def start(self) -> None:
        """
        Idempotent. Creates the aiohttp session and starts the sender task.
        Called once by grid_runner; safe to call again (no-op).
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._send_timeout_s),
            )
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name="telegram_sender",
            )
        logger.info("telegram alerter started",
                    chat_ids=len(self._chat_ids),
                    queue_max=self._queue.maxsize)

    async def stop(self) -> None:
        """
        Idempotent. Cancels the sender task and closes the session.
        Pending alerts in the queue are dropped — Telegram alerts are
        transient by design.
        """
        if self._sender_task and not self._sender_task.done():
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):
                # Sender either cancelled cleanly or raised on the way out;
                # either way, we're done with it.
                pass
        self._sender_task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                # Session close should never raise in practice; if it does
                # there's nothing useful to do.
                pass
            self._session = None
        logger.info("telegram alerter stopped")

    # ------------------------------------------------------------------
    # Event methods (sync, fire-and-forget)
    # ------------------------------------------------------------------

    def boot(self, **kwargs: Any) -> None:
        if not self._alert_on.get("boot", True):
            return
        self._enqueue(format_boot(**kwargs))

    def shutdown(self, **kwargs: Any) -> None:
        if not self._alert_on.get("shutdown", True):
            return
        self._enqueue(format_shutdown(**kwargs))

    def crash(self, **kwargs: Any) -> None:
        if not self._alert_on.get("crash", True):
            return
        self._enqueue(format_crash(**kwargs))

    def fill(self, **kwargs: Any) -> None:
        if not self._alert_on.get("fill", True):
            return
        self._enqueue(format_fill(**kwargs))

    def halt(self, **kwargs: Any) -> None:
        if not self._alert_on.get("halt", True):
            return
        self._enqueue(format_halt(**kwargs))

    def pause(self, **kwargs: Any) -> None:
        if not self._alert_on.get("pause", True):
            return
        self._enqueue(format_pause(**kwargs))

    def resume(self, **kwargs: Any) -> None:
        if not self._alert_on.get("resume", True):
            return
        self._enqueue(format_resume(**kwargs))

    def daily_summary(self, **kwargs: Any) -> None:
        if not self._alert_on.get("daily_summary", True):
            return
        self._enqueue(format_daily_summary(**kwargs))

    def recenter(self, **kwargs: Any) -> None:
        if not self._alert_on.get("recenter", False):
            return
        self._enqueue(format_recenter(**kwargs))

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _enqueue(self, message: str) -> None:
        """
        Push a formatted message onto the send queue.

        If the alerter is in the broken state (a previous 4xx), we drop
        silently — there's no point queueing what we know can't ship.

        If the queue is full, we drop the OLDEST message and enqueue the
        new one. See module docstring for the freshness-vs-ordering choice.
        """
        if self._broken:
            return
        try:
            self._queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Evict oldest, then push.
        try:
            self._queue.get_nowait()
            self._dropped_count += 1
        except asyncio.QueueEmpty:
            # Race: queue went from full to empty between put_nowait and
            # get_nowait. Just retry put.
            pass
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            # Still full after evicting one — extremely unlikely (would
            # require concurrent writers), but better to lose this message
            # than spin.
            self._dropped_count += 1

    # ------------------------------------------------------------------
    # Sender loop
    # ------------------------------------------------------------------

    async def _sender_loop(self) -> None:
        """
        Drain the queue. One outstanding message at a time so retries
        don't starve fresher alerts (a stuck send only blocks itself, not
        the queue).
        """
        while True:
            try:
                message = await self._queue.get()
            except asyncio.CancelledError:
                return

            if self._broken:
                # Don't bother — we already know this won't go through.
                continue

            # Prepend a one-shot dropped-count notice so the operator knows
            # they missed alerts. Reset BEFORE the send so a retry doesn't
            # double-prepend.
            if self._dropped_count > 0:
                message = (
                    f"({self._dropped_count} alerts dropped due to "
                    f"queue overflow)\n{message}"
                )
                self._dropped_count = 0

            try:
                await self._send_with_retry(message)
            except asyncio.CancelledError:
                return
            except Exception as e:
                # Defensive: _send_with_retry is supposed to swallow
                # everything, but we never want sender_loop to die from a
                # send. Log and keep going so subsequent alerts aren't lost.
                logger.error("sender loop unexpected error",
                             error=str(e), error_type=type(e).__name__)

    # ------------------------------------------------------------------
    # HTTP — split out so tests can override _http_post without aiohttp
    # ------------------------------------------------------------------

    async def _http_post(
        self, url: str, payload: dict,
    ) -> tuple[int, str]:
        """
        One HTTP POST. Returns (status_code, body_text).

        Split into its own method so unit tests can override it without
        spinning up an HTTP server or mocking aiohttp internals.
        """
        if self._session is None:
            # Defensive: should be unreachable in production because the
            # sender task only runs after start() set this up.
            raise RuntimeError("telegram session not initialised")
        async with self._session.post(url, json=payload) as resp:
            body = await resp.text()
            return resp.status, body

    async def _send_with_retry(self, message: str) -> None:
        """
        Send `message` to every chat id with exponential-backoff retry on
        5xx / network errors. Returns once the message either fully sent,
        or we hit a 4xx (sets _broken), or we exhausted retries.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        delay = _RETRY_INITIAL_DELAY_S
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            all_ok = True
            for chat_id in self._chat_ids:
                payload = {"chat_id": chat_id, "text": message}
                try:
                    status, body = await self._http_post(url, payload)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    # Network-level failure — retry path.
                    logger.warning("telegram request failed",
                                   chat_id=chat_id,
                                   error=str(e),
                                   error_type=type(e).__name__,
                                   attempt=attempt)
                    all_ok = False
                    continue
                if status == 200:
                    continue
                if 400 <= status < 500:
                    # Auth / banned / chat not found — no point retrying.
                    # Body trim avoids dumping huge HTML pages into logs.
                    logger.error("telegram 4xx — disabling alerter",
                                 status=status,
                                 chat_id=chat_id,
                                 body=body[:200])
                    self._broken = True
                    return
                # 5xx or unexpected status: retry.
                logger.warning("telegram 5xx — will retry",
                               status=status,
                               chat_id=chat_id,
                               body=body[:200],
                               attempt=attempt)
                all_ok = False
            if all_ok:
                return
            if attempt >= _RETRY_MAX_ATTEMPTS:
                logger.warning("telegram send giving up after retries",
                               attempts=attempt)
                return
            try:
                await asyncio.sleep(min(delay, _RETRY_MAX_DELAY_S))
            except asyncio.CancelledError:
                return
            delay *= 2

    # ------------------------------------------------------------------
    # Test introspection (do not use from production code)
    # ------------------------------------------------------------------

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

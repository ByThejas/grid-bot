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
import json
import time
from typing import Any, Callable, Iterable, Optional

import aiohttp

from src.notifications.alerter import Alerter
from src.notifications.format import (
    format_boot,
    format_crash,
    format_daily_summary,
    format_fill,
    format_halt,
    format_halt_ack,
    format_help,
    format_pause,
    format_recenter,
    format_resume,
    format_shutdown,
    format_status,
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

# Listener policy. getUpdates uses Telegram's long-poll: server holds the
# request open up to LISTENER_LONG_POLL_S waiting for new messages. The
# HTTP-level timeout adds a small buffer so the request finishes cleanly
# instead of timing out on our side just before the server replies.
_LISTENER_LONG_POLL_S: int = 25
_LISTENER_HTTP_TIMEOUT_S: float = float(_LISTENER_LONG_POLL_S) + 5.0
# Restart governance for the listener task: at most LISTENER_MAX_RESTARTS
# crashes within LISTENER_RESTART_WINDOW_S seconds before we permanently
# disable the listener (sender keeps running). Without this, a poisoned
# response could spin a hot crash-loop and saturate the log.
_LISTENER_MAX_RESTARTS: int = 5
_LISTENER_RESTART_WINDOW_S: float = 300.0
_LISTENER_RESTART_DELAY_S: float = 1.0


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

        # --- Listener (two-way commands, Phase 11A step 6) -------------
        # Set via set_command_handlers(). If unset, start() will not spawn
        # the listener task — TelegramAlerter then operates send-only.
        self._status_provider: Optional[Callable[[], dict]] = None
        self._halt_handler: Optional[Callable[[], None]] = None
        self._listener_task: Optional[asyncio.Task] = None
        # Long-poll offset: the next update_id we want from getUpdates.
        # Telegram drops everything < offset on next call, so this is how
        # we ack consumed updates.
        self._listener_offset: int = 0
        # Sliding window of recent crash timestamps for restart governance.
        self._listener_crash_times: list[float] = []
        # True once we've burned through _LISTENER_MAX_RESTARTS in window.
        # Sender keeps running; we just stop trying to listen.
        self._listener_disabled: bool = False

    async def start(self) -> None:
        """
        Idempotent. Creates the aiohttp session, starts the sender task,
        and (if command handlers are attached) starts the listener task.
        Called once by grid_runner; safe to call again — already-running
        tasks are not respawned.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession()
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name="telegram_sender",
            )
        # Listener only spawns when handlers are wired AND not disabled by
        # restart-governance. grid_runner calls set_command_handlers BEFORE
        # start() in production; tests can leave it unset for send-only.
        if (
            self._listener_task is None or self._listener_task.done()
        ) and self._status_provider is not None and self._halt_handler is not None and not self._listener_disabled:
            self._listener_task = asyncio.create_task(
                self._listener_loop(), name="telegram_listener",
            )
        logger.info("telegram alerter started",
                    chat_ids=len(self._chat_ids),
                    queue_max=self._queue.maxsize,
                    listener=self._listener_task is not None)

    async def stop(self, drain_timeout_s: float = 5.0) -> None:
        """
        Idempotent. Drains pending alerts (up to drain_timeout_s seconds),
        cancels background tasks, then closes the session.

        Why drain matters: the runner's finally block calls alerter.shutdown
        and alerter.halt right before stop(). Without a drain, those final
        alerts are cancelled mid-send and never reach the operator's phone.
        Found in the live testnet smoke test on 2026-04-29.

        The listener is cancelled immediately — any pending command reply
        was already sent direct (bypasses queue), so there's nothing in
        flight worth waiting for.
        """
        # Cancel listener first — its long-poll has nothing the user is
        # waiting for, and cancelling it frees the event loop to drain
        # the sender queue faster.
        if self._listener_task is not None and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
        self._listener_task = None

        # Drain the sender. queue.join() blocks until task_done() has
        # been called for every put — both the sender's own acks and
        # _enqueue's eviction acks. Bounded by drain_timeout_s so a dead
        # network can't hang process shutdown.
        if self._sender_task is not None and not self._sender_task.done():
            try:
                await asyncio.wait_for(
                    self._queue.join(), timeout=drain_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "telegram sender drain timed out — pending alerts will be lost",
                    remaining=self._queue.qsize(),
                    timeout_s=drain_timeout_s,
                )
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):
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

    def set_command_handlers(
        self,
        *,
        status_provider: Optional[Callable[[], dict]] = None,
        halt_handler: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Attach the callbacks that the listener uses to answer /status and
        /halt. Called by grid_runner BEFORE start(). See Alerter ABC for
        the callback contract.
        """
        self._status_provider = status_provider
        self._halt_handler = halt_handler

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

        Every put has a matching task_done() somewhere — either when the
        sender finishes processing it, or here in the eviction path. Without
        that pairing, queue.join() (used by stop() to drain) hangs forever.
        """
        if self._broken:
            return
        try:
            self._queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Evict oldest, then push. Mark the evicted message done so the
        # unfinished-tasks counter stays balanced.
        try:
            self._queue.get_nowait()
            self._queue.task_done()
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

        Every successful queue.get() has a paired task_done() in the
        finally block so stop()'s queue.join() drain can complete. If
        cancellation arrives mid-send, we still ack the message — the
        intent of join() is "is the sender caught up?", not "did every
        message get through?".
        """
        while True:
            try:
                message = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                if self._broken:
                    # Don't bother — we already know this won't go through.
                    continue

                # Prepend a one-shot dropped-count notice so the operator
                # knows they missed alerts. Reset BEFORE the send so a
                # retry doesn't double-prepend.
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
                    # everything, but we never want sender_loop to die from
                    # a send. Log and keep going.
                    logger.error("sender loop unexpected error",
                                 error=str(e), error_type=type(e).__name__)
            finally:
                # Always ack the message — even on cancellation or unhandled
                # exception. Without this, queue.join() in stop() can hang.
                self._queue.task_done()

    # ------------------------------------------------------------------
    # HTTP — split out so tests can override _http_post without aiohttp
    # ------------------------------------------------------------------

    async def _http_post(
        self, url: str, payload: dict, *, timeout_s: Optional[float] = None,
    ) -> tuple[int, str]:
        """
        One HTTP POST. Returns (status_code, body_text).

        Split into its own method so unit tests can override it without
        spinning up an HTTP server or mocking aiohttp internals.

        timeout_s: per-call HTTP timeout in seconds. sendMessage uses the
            sender's configured short timeout. getUpdates uses a longer
            timeout to ride out the server-side long-poll without our
            client side cancelling first.
        """
        if self._session is None:
            # Defensive: should be unreachable in production because the
            # tasks only run after start() set this up.
            raise RuntimeError("telegram session not initialised")
        effective_timeout = (
            timeout_s if timeout_s is not None else self._send_timeout_s
        )
        client_timeout = aiohttp.ClientTimeout(total=effective_timeout)
        async with self._session.post(url, json=payload, timeout=client_timeout) as resp:
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
            # No CancelledError handler here on purpose: if stop() cancels
            # the sender mid-backoff, we want the cancellation to propagate
            # all the way up to _sender_loop's `except CancelledError` so
            # the task actually terminates. Catching and converting to a
            # normal return swallowed the signal — found in 2026-04-29
            # smoke test debugging.
            await asyncio.sleep(min(delay, _RETRY_MAX_DELAY_S))
            delay *= 2

    # ------------------------------------------------------------------
    # Listener loop (two-way commands)
    # ------------------------------------------------------------------

    async def _listener_loop(self) -> None:
        """
        Resilient outer loop. Calls _poll_once() repeatedly. If it raises,
        we log and restart — but only up to _LISTENER_MAX_RESTARTS times
        within _LISTENER_RESTART_WINDOW_S seconds, after which we
        permanently disable the listener (sender keeps running).

        Restart governance protects against poisoned-response crash loops
        — a malformed update that consistently triggers an exception in
        our parser shouldn't burn 100% CPU forever.
        """
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                now = time.time()
                # Trim old crash entries outside the rolling window.
                cutoff = now - _LISTENER_RESTART_WINDOW_S
                self._listener_crash_times = [
                    t for t in self._listener_crash_times if t >= cutoff
                ]
                self._listener_crash_times.append(now)
                if len(self._listener_crash_times) > _LISTENER_MAX_RESTARTS:
                    logger.error(
                        "telegram listener disabled — too many crashes",
                        crashes=len(self._listener_crash_times),
                        window_s=_LISTENER_RESTART_WINDOW_S,
                        last_error=str(e),
                    )
                    self._listener_disabled = True
                    return
                logger.warning(
                    "telegram listener crashed — restarting",
                    error=str(e),
                    error_type=type(e).__name__,
                    crashes_in_window=len(self._listener_crash_times),
                )
                try:
                    await asyncio.sleep(_LISTENER_RESTART_DELAY_S)
                except asyncio.CancelledError:
                    return

    async def _poll_once(self) -> None:
        """
        One getUpdates round-trip. Telegram holds the request open up to
        _LISTENER_LONG_POLL_S waiting for new messages; we ack with
        offset = max_seen_update_id + 1 on the next call.

        Network / server errors are logged and swallowed at this level so
        the outer loop doesn't see them as "crashes" worth counting toward
        restart-governance — only parse / dispatch bugs should hit that.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        payload = {
            "offset": self._listener_offset,
            "timeout": _LISTENER_LONG_POLL_S,
        }
        try:
            status, body = await self._http_post(
                url, payload, timeout_s=_LISTENER_HTTP_TIMEOUT_S,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Transient — short sleep then the outer loop calls us again.
            logger.warning("telegram getUpdates failed",
                           error=str(e), error_type=type(e).__name__)
            await asyncio.sleep(2.0)
            return
        if status != 200:
            logger.warning("telegram getUpdates non-200",
                           status=status, body=body[:200])
            await asyncio.sleep(2.0)
            return
        try:
            data = json.loads(body)
        except ValueError as e:
            # Malformed JSON from Telegram. Extremely rare; log and move on.
            logger.warning("telegram getUpdates json parse failed",
                           error=str(e), body=body[:200])
            return
        if not data.get("ok"):
            logger.warning("telegram getUpdates not ok",
                           description=data.get("description", "")[:200])
            return
        for update in data.get("result", []) or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Ack everything we see, even messages we ignore (unknown
                # chat, unknown command). Otherwise Telegram redelivers
                # them on every poll forever.
                self._listener_offset = max(
                    self._listener_offset, update_id + 1,
                )
            await self._handle_update(update)

    async def _handle_update(self, update: dict) -> None:
        """
        One Telegram update → maybe one command dispatch.

        Reject silently if:
          * No `message` field (edited messages, channel posts, etc.)
          * Sender chat id not in allow-list
          * Text doesn't start with one of our three commands

        Silent rejection (vs an error reply) is deliberate: responding
        invites probing of the bot.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        if chat_id not in self._chat_ids:
            # Logged at WARN so an operator can audit attempted access.
            logger.warning("telegram unauthorised chat — ignoring",
                           chat_id=chat_id,
                           text=str(message.get("text", ""))[:100])
            return
        text_raw = message.get("text")
        if not isinstance(text_raw, str):
            return
        text = text_raw.strip()
        # Allow `/status@MyBotName` style by splitting on first space and
        # stripping any @suffix from the command word.
        head = text.split(maxsplit=1)[0] if text else ""
        cmd = head.split("@", 1)[0].lower()
        if cmd == "/status":
            await self._cmd_status(chat_id)
        elif cmd == "/halt":
            await self._cmd_halt(chat_id)
        elif cmd == "/help":
            await self._cmd_help(chat_id)
        # Anything else: silent. No reply, no log spam.

    async def _cmd_status(self, chat_id: int) -> None:
        if self._status_provider is None:
            return
        try:
            data = self._status_provider() or {}
        except Exception as e:
            # Provider must never crash the listener loop. Surface in log
            # and reply with a short error so the operator knows their
            # /status isn't reflecting reality.
            logger.error("status_provider raised",
                         error=str(e), error_type=type(e).__name__)
            await self._send_reply(chat_id, "[STATUS] error gathering state — check logs")
            return
        try:
            text = format_status(**data)
        except TypeError as e:
            logger.error("status_provider returned wrong shape",
                         error=str(e), keys=list(data.keys()))
            await self._send_reply(chat_id, "[STATUS] internal error — check logs")
            return
        await self._send_reply(chat_id, text)

    async def _cmd_halt(self, chat_id: int) -> None:
        if self._halt_handler is None:
            return
        # Acknowledge to the operator FIRST. The handler creates the
        # sentinel file; the existing risk monitor flips stop_event within
        # ~poll_interval seconds and the sender's own [HALT] alert will
        # follow. Two confirmations is intentional per design doc §5.
        await self._send_reply(chat_id, format_halt_ack())
        try:
            self._halt_handler()
        except Exception as e:
            logger.error("halt_handler raised",
                         error=str(e), error_type=type(e).__name__)
            await self._send_reply(
                chat_id,
                "[HALT] handler error — check logs and run `touch runtime/HALT` manually",
            )

    async def _cmd_help(self, chat_id: int) -> None:
        await self._send_reply(chat_id, format_help())

    async def _send_reply(self, chat_id: int, text: str) -> None:
        """
        Direct send — bypasses the alert queue because replies are
        targeted at the requesting chat (not fanned out) and have no
        retry budget worth queueing for. If a reply fails, log and move
        on; the operator can re-issue the command.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        try:
            status, body = await self._http_post(url, payload)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("telegram reply send failed",
                           chat_id=chat_id,
                           error=str(e),
                           error_type=type(e).__name__)
            return
        if status != 200:
            logger.warning("telegram reply non-200",
                           status=status, chat_id=chat_id, body=body[:200])

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

    @property
    def listener_offset(self) -> int:
        return self._listener_offset

    @property
    def listener_disabled(self) -> bool:
        return self._listener_disabled

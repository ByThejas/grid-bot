"""
Tests for src.notifications.telegram — Phase 11A step 5 (sender side).

The class talks to Telegram over aiohttp. Rather than mock aiohttp's
internals (fragile, version-dependent), each test subclasses
`TelegramAlerter` and overrides `_http_post` to capture / shape the
"network" response. This keeps the unit-test surface narrow and the
fixtures readable.

Coverage targets:
  * factory still returns NullAlerter when configured to (covered in
    test_notifications_alerter.py — not retested here)
  * start/stop are idempotent and don't leak tasks
  * event methods enqueue exactly one message (when toggle is on) or
    zero (when toggle is off)
  * the sender drains the queue and POSTs the formatted message to every
    allowed chat id
  * retry: 5xx + transient errors retry up to _RETRY_MAX_ATTEMPTS, then
    drop the message and continue
  * fail-fast: 4xx flips _broken=True and stops further sends
  * queue overflow: oldest evicted, _dropped_count tracks, next send
    carries a "(N dropped)" prefix exactly once
  * the listener path is NOT exercised here — that lands in step 6.
"""

import asyncio

import aiohttp
import pytest

from src.notifications.alerter import Alerter
from src.notifications.telegram import (
    _RETRY_MAX_ATTEMPTS,
    TelegramAlerter,
)


# ---------------------------------------------------------------------------
# Test harness — subclass that swaps real HTTP for a programmable response.
# ---------------------------------------------------------------------------


class _RecordingAlerter(TelegramAlerter):
    """
    TelegramAlerter wired to a fake HTTP layer.

    `responses` is the queue of (status, body) tuples returned by
    successive `_http_post` calls. If we run off the end, the last entry
    is reused (so a test that expects unlimited 200s only needs one
    entry). If `responses` is empty, every call raises whatever sits in
    `raise_exc` (an exception or None for default ConnectionError).

    `posts` records every call as (url, payload) so tests can assert on
    payload shape / chat-id fan-out.
    """

    def __init__(self, *args, responses=None, raise_exc=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._responses = list(responses or [])
        self._raise_exc = raise_exc
        self.posts: list[tuple[str, dict]] = []

    async def _http_post(self, url: str, payload: dict) -> tuple[int, str]:
        self.posts.append((url, dict(payload)))
        if not self._responses:
            if self._raise_exc is not None:
                raise self._raise_exc
            raise aiohttp.ClientConnectionError("no response programmed")
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def _make_alerter(**kwargs) -> _RecordingAlerter:
    """Build an alerter with sensible defaults; tests override what they need."""
    return _RecordingAlerter(
        bot_token="abc:def",
        allowed_chat_ids=kwargs.pop("allowed_chat_ids", [12345]),
        config=kwargs.pop("config", {}),
        responses=kwargs.pop("responses", [(200, "ok")]),
        raise_exc=kwargs.pop("raise_exc", None),
        **kwargs,
    )


async def _wait_for_posts(
    alerter: "_RecordingAlerter", expected: int, *, timeout: float = 2.0,
) -> None:
    """
    Poll until alerter.posts reaches the expected count (or fail).

    More robust than draining the queue, because the sender may still be
    mid-retry when the queue empties. Waiting on the post count means we
    only return once the network-side activity we care about has happened.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while len(alerter.posts) < expected:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"expected >={expected} posts within {timeout}s, "
                f"got {len(alerter.posts)} (queue={alerter.queue_size}, "
                f"broken={alerter.broken})"
            )
        await asyncio.sleep(0.005)


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    """Generic poll-until-predicate-true with a deadline."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"predicate did not become true within {timeout}s")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_telegram_alerter_implements_alerter_abc():
    a = _make_alerter()
    assert isinstance(a, Alerter)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_idempotent():
    a = _make_alerter()
    await a.start()
    await a.start()  # no-op second call
    await a.stop()
    await a.stop()  # no-op second call


@pytest.mark.asyncio
async def test_stop_cancels_sender_task():
    a = _make_alerter()
    await a.start()
    sender = a._sender_task
    assert sender is not None and not sender.done()
    await a.stop()
    assert a._sender_task is None
    assert sender.cancelled() or sender.done()


# ---------------------------------------------------------------------------
# Event method enqueue / toggle
# ---------------------------------------------------------------------------


def test_event_methods_enqueue_when_toggle_on():
    a = _make_alerter()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    a.fill(symbol="BTCUSDT", side="BUY", price=70000.0, qty=0.0003)
    assert a.queue_size == 2


def test_event_methods_skip_when_toggle_off():
    cfg = {"alert_on": {"fill": False, "boot": False}}
    a = _make_alerter(config=cfg)
    a.fill(symbol="BTCUSDT", side="BUY", price=70000.0, qty=0.0003)
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    assert a.queue_size == 0


def test_recenter_off_by_default_per_design():
    a = _make_alerter()
    a.recenter(symbol="BTCUSDT", old_mid=70000.0, new_mid=71500.0, drift_pct=2.14)
    assert a.queue_size == 0


def test_recenter_on_when_explicitly_enabled():
    a = _make_alerter(config={"alert_on": {"recenter": True}})
    a.recenter(symbol="BTCUSDT", old_mid=70000.0, new_mid=71500.0, drift_pct=2.14)
    assert a.queue_size == 1


# ---------------------------------------------------------------------------
# Send fan-out + payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_fans_out_to_every_allowed_chat_id():
    a = _make_alerter(allowed_chat_ids=[111, 222, 333])
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_for_posts(a, 3)
    await a.stop()

    chat_ids_seen = [p["chat_id"] for _, p in a.posts]
    assert chat_ids_seen == [111, 222, 333]


@pytest.mark.asyncio
async def test_send_payload_carries_formatted_text():
    a = _make_alerter()
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_for_posts(a, 1)
    await a.stop()

    assert len(a.posts) == 1
    url, payload = a.posts[0]
    assert "sendMessage" in url
    assert "abc:def" in url   # bot token in the URL path (matches Telegram API)
    assert payload["chat_id"] == 12345
    assert payload["text"].startswith("[BOOT]")
    assert "testnet" in payload["text"]


# ---------------------------------------------------------------------------
# Retry on transient failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    # Two 503s, then 200. Sender should retry until success.
    a = _make_alerter(responses=[(503, "busy"), (503, "busy"), (200, "ok")])
    # Skip real backoff in tests — we don't want to wait 7 seconds.
    monkeypatch.setattr("src.notifications.telegram._RETRY_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr("src.notifications.telegram._RETRY_MAX_DELAY_S", 0.0)
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_for_posts(a, 3)
    await a.stop()
    # Three POSTs: two 503s, one 200.
    assert len(a.posts) == 3
    assert a.broken is False


@pytest.mark.asyncio
async def test_5xx_gives_up_after_max_attempts(monkeypatch):
    # All 503 — should retry up to _RETRY_MAX_ATTEMPTS and then move on.
    a = _make_alerter(responses=[(503, "busy")])
    monkeypatch.setattr("src.notifications.telegram._RETRY_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr("src.notifications.telegram._RETRY_MAX_DELAY_S", 0.0)
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_for_posts(a, _RETRY_MAX_ATTEMPTS)
    # Small extra wait to ensure no further attempts happen.
    await asyncio.sleep(0.05)
    await a.stop()
    assert len(a.posts) == _RETRY_MAX_ATTEMPTS
    # Important: we did NOT mark broken — only 4xx triggers that.
    assert a.broken is False


@pytest.mark.asyncio
async def test_network_error_retries(monkeypatch):
    # First call raises ClientConnectionError, second returns 200.
    calls = {"n": 0}

    async def _once(url, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aiohttp.ClientConnectionError("temporary")
        return (200, "ok")

    a = _make_alerter(responses=[(200, "ok")])
    a._http_post = _once  # type: ignore[assignment]
    monkeypatch.setattr("src.notifications.telegram._RETRY_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr("src.notifications.telegram._RETRY_MAX_DELAY_S", 0.0)
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_until(lambda: calls["n"] >= 2)
    await a.stop()
    assert calls["n"] == 2
    assert a.broken is False


# ---------------------------------------------------------------------------
# Fail-fast on 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_marks_broken_and_stops(monkeypatch):
    # 401 unauthorized — bad token / banned bot. We must NOT retry.
    a = _make_alerter(responses=[(401, "Unauthorized")])
    monkeypatch.setattr("src.notifications.telegram._RETRY_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr("src.notifications.telegram._RETRY_MAX_DELAY_S", 0.0)
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    await _wait_until(lambda: a.broken)
    # Subsequent enqueues should be silently dropped.
    a.fill(symbol="BTCUSDT", side="BUY", price=70000.0, qty=0.0003)
    await asyncio.sleep(0.02)
    await a.stop()

    assert a.broken is True
    # Exactly one POST attempt — no retries on 4xx.
    assert len(a.posts) == 1
    # And the second event never even hit the queue.
    assert a.queue_size == 0


# ---------------------------------------------------------------------------
# Queue overflow
# ---------------------------------------------------------------------------


def test_queue_overflow_drops_oldest_and_counts():
    # max_queue_size=2 → after 4 enqueues the queue holds the latest 2
    # and dropped_count=2. (No sender running, so nothing drains.)
    a = _make_alerter(config={"max_queue_size": 2})
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    a.fill(symbol="BTCUSDT", side="BUY", price=70000.0, qty=0.0003)
    a.fill(symbol="ETHUSDT", side="SELL", price=2300.0, qty=0.005,
           realized_delta_usd=0.18)
    a.halt(reason="test")
    assert a.queue_size == 2
    assert a.dropped_count == 2


@pytest.mark.asyncio
async def test_dropped_prefix_lands_on_next_send(monkeypatch):
    # Stuff three messages into a max=1 queue, then start the sender and
    # let it drain. The single message that survives should carry the
    # "(N alerts dropped...)" prefix.
    a = _make_alerter(config={"max_queue_size": 1})
    monkeypatch.setattr("src.notifications.telegram._RETRY_INITIAL_DELAY_S", 0.0)
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    a.halt(reason="floor")
    a.halt(reason="kill switch")
    assert a.dropped_count == 2
    await a.start()
    await _wait_for_posts(a, 1)
    await a.stop()
    assert len(a.posts) == 1
    text = a.posts[0][1]["text"]
    assert text.startswith("(2 alerts dropped due to queue overflow)")
    # And dropped_count is reset so the next message wouldn't double-prefix.
    assert a.dropped_count == 0


# ---------------------------------------------------------------------------
# Broken state stops accepting new enqueues
# ---------------------------------------------------------------------------


def test_enqueue_drops_silently_when_broken():
    a = _make_alerter()
    a._broken = True
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    assert a.queue_size == 0


@pytest.mark.asyncio
async def test_stop_drains_pending_messages_before_cancelling_sender():
    """
    Regression: in the 2026-04-29 testnet smoke test, the [HALT] alert
    fired by RiskManager.trip() and the [STOP] alert fired by the runner's
    finally block never arrived on Telegram because stop() cancelled the
    sender task immediately, killing in-flight HTTP requests. stop() must
    drain the queue first.

    Setup: enqueue 3 messages with a fake _http_post that takes ~50ms each
    (simulates real-world Telegram RTT). Without the drain fix, stop() is
    called milliseconds after enqueue and cancels the sender before any
    of them ship — len(posts) would be 0 or 1. With the drain, all 3 land.
    """
    a = _RecordingAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        responses=[(200, "ok")],
    )
    # Replace _http_post with a slow version that mimics network RTT.
    async def slow_post(url, payload, *, timeout_s=None):
        await asyncio.sleep(0.05)
        a.posts.append((url, dict(payload)))
        return 200, "ok"
    a._http_post = slow_post  # type: ignore[assignment]
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    a.halt(reason="kill switch tripped")
    a.shutdown(final_open_orders=0, session_realized_usd=0.0,
               uptime_seconds=10.0)
    # Three messages queued; stop() should drain all three before cancel.
    await a.stop()
    assert len(a.posts) == 3
    # Sanity: tags appear in enqueue order.
    tags = [p[1]["text"].split()[0] for p in a.posts]
    assert tags == ["[BOOT]", "[HALT]", "[STOP]"]


@pytest.mark.asyncio
async def test_stop_drain_timeout_does_not_hang_on_dead_network(monkeypatch):
    """
    If Telegram is unreachable, stop() must not hang forever — the drain
    is bounded by drain_timeout_s. We pass a tiny timeout so the test
    finishes fast without making the real timeout shorter than 5s.
    """
    a = _RecordingAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        responses=[(503, "busy")],
    )
    monkeypatch.setattr(
        "src.notifications.telegram._RETRY_INITIAL_DELAY_S", 10.0,
    )
    await a.start()
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    # Sender will be stuck retrying behind a 10s sleep; drain should bail
    # out at 0.2s. The exact post count is implementation-dependent (0 or 1
    # attempt before the first sleep); what matters is stop() returns fast.
    deadline = asyncio.get_event_loop().time() + 2.0
    await a.stop(drain_timeout_s=0.2)
    assert asyncio.get_event_loop().time() < deadline


@pytest.mark.asyncio
async def test_stop_drains_evicted_messages_too():
    """
    Eviction calls task_done() on the dropped message — without it,
    queue.join() would hang forever waiting for an unfinished task that
    no one is going to finish.
    """
    a = _RecordingAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={"max_queue_size": 1},
        responses=[(200, "ok")],
    )
    # Three enqueues into a max=1 queue forces 2 evictions.
    a.boot(env_mode="testnet", symbols=["BTCUSDT"],
           max_open_orders=10, daily_loss_limit_usd=20.0)
    a.halt(reason="first")
    a.halt(reason="second")
    assert a.dropped_count == 2
    await a.start()
    # If task_done() weren't called on evicted messages, this would hang.
    await a.stop(drain_timeout_s=2.0)
    assert len(a.posts) == 1   # only the surviving message ships
    assert a.posts[0][1]["text"].startswith("(2 alerts dropped")


# ---------------------------------------------------------------------------
# Listener (Phase 11A step 6)
# ---------------------------------------------------------------------------
#
# The listener loop polls Telegram's getUpdates and dispatches /status,
# /halt, /help. Tests use a programmable _http_post that distinguishes
# `getUpdates` URLs (returns canned update batches) from `sendMessage`
# URLs (records the reply). The listener offset advances as updates are
# acked. Polling is paused after one batch so tests don't loop.


import json as _json  # noqa: E402  -- intentionally late, listener tests only


class _ListenerAlerter(TelegramAlerter):
    """
    Subclass that lets a test feed a single batch of Telegram updates and
    capture every reply the listener tries to send.

    `update_batches` is a list of lists. Each call to getUpdates pops
    the next batch (formatted as Telegram's `{"ok":true,"result":[...]}`
    wrapper). When batches run out we return an empty result, then sleep
    in the test loop until torn down. This avoids the test ever needing
    to wait on a real long-poll.
    """

    def __init__(self, *args, update_batches=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_batches = list(update_batches or [])
        self.replies: list[dict] = []
        self.get_calls = 0
        # If set, the next sendMessage POST returns this status/body
        # instead of (200, "ok"). Lets tests simulate reply-send failure.
        self.reply_response: tuple[int, str] = (200, "ok")

    async def _http_post(self, url, payload, *, timeout_s=None):
        # Real aiohttp.post always yields control; our fake must too,
        # otherwise the listener's tight `await _poll_once()` loop starves
        # the test's polling helper and the test hangs.
        await asyncio.sleep(0)
        if "getUpdates" in url:
            self.get_calls += 1
            if self._update_batches:
                batch = self._update_batches.pop(0)
            else:
                batch = []
            body = _json.dumps({"ok": True, "result": batch})
            return 200, body
        if "sendMessage" in url:
            self.replies.append(dict(payload))
            return self.reply_response
        return 404, "unexpected url"


def _msg(*, update_id: int, chat_id: int, text: str) -> dict:
    """Build a minimal Telegram update with one message."""
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _status_provider_stub() -> dict:
    return {
        "env_mode": "testnet",
        "open_orders": 7,
        "placed_total": 12,
        "filled_total": 4,
        "realized_24h_usd": 0.46,
        "halted": False,
        "uptime_seconds": 8060.0,  # 2h14m
    }


def test_set_command_handlers_is_noop_on_null_alerter():
    from src.notifications.alerter import NullAlerter
    a = NullAlerter()
    # Must not raise; default impl in ABC returns None.
    a.set_command_handlers(
        status_provider=_status_provider_stub,
        halt_handler=lambda: None,
    )


@pytest.mark.asyncio
async def test_listener_does_not_start_without_handlers():
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[],
    )
    await a.start()
    try:
        assert a._listener_task is None
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_listener_handles_status(monkeypatch):
    halt_called = {"n": 0}

    def _halt():
        halt_called["n"] += 1

    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [_msg(update_id=1, chat_id=12345, text="/status")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=_halt,
    )
    await a.start()
    try:
        await _wait_until(lambda: len(a.replies) >= 1)
    finally:
        await a.stop()
    assert halt_called["n"] == 0
    assert len(a.replies) == 1
    reply = a.replies[0]
    assert reply["chat_id"] == 12345
    assert reply["text"].startswith("[STATUS]")
    assert "testnet" in reply["text"]
    # offset acked one past the last update_id we processed.
    assert a.listener_offset == 2


@pytest.mark.asyncio
async def test_listener_handles_halt_and_calls_handler():
    halt_called = {"n": 0}

    def _halt():
        halt_called["n"] += 1

    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [_msg(update_id=42, chat_id=12345, text="/halt")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=_halt,
    )
    await a.start()
    try:
        await _wait_until(lambda: halt_called["n"] >= 1)
        await _wait_until(lambda: len(a.replies) >= 1)
    finally:
        await a.stop()
    assert halt_called["n"] == 1
    assert a.replies[0]["text"].startswith("[HALT]")
    assert a.listener_offset == 43


@pytest.mark.asyncio
async def test_listener_handles_help():
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [_msg(update_id=5, chat_id=12345, text="/help")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=lambda: None,
    )
    await a.start()
    try:
        await _wait_until(lambda: len(a.replies) >= 1)
    finally:
        await a.stop()
    assert a.replies[0]["text"].startswith("[HELP]")


@pytest.mark.asyncio
async def test_listener_accepts_command_with_bot_username_suffix():
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [_msg(update_id=1, chat_id=12345, text="/status@MyGridBot")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=lambda: None,
    )
    await a.start()
    try:
        await _wait_until(lambda: len(a.replies) >= 1)
    finally:
        await a.stop()
    assert a.replies[0]["text"].startswith("[STATUS]")


@pytest.mark.asyncio
async def test_listener_silently_ignores_unauthorised_chat():
    halt_called = {"n": 0}
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],   # 99999 below is NOT allowed
        config={},
        update_batches=[
            [_msg(update_id=1, chat_id=99999, text="/halt")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub,
        halt_handler=lambda: halt_called.__setitem__("n", halt_called["n"] + 1),
    )
    await a.start()
    # Wait for the update to be acked; no reply should be sent.
    try:
        await _wait_until(lambda: a.listener_offset >= 2)
        # Give the listener a moment in case it (incorrectly) sends a reply.
        await asyncio.sleep(0.05)
    finally:
        await a.stop()
    assert halt_called["n"] == 0
    assert a.replies == []


@pytest.mark.asyncio
async def test_listener_silently_ignores_unknown_command():
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [_msg(update_id=1, chat_id=12345, text="/unknown")],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=lambda: None,
    )
    await a.start()
    try:
        await _wait_until(lambda: a.listener_offset >= 2)
        await asyncio.sleep(0.05)
    finally:
        await a.stop()
    assert a.replies == []


@pytest.mark.asyncio
async def test_listener_advances_offset_even_on_ignored_messages():
    # Mixed batch: one allowed /status, one denied chat. Offset must
    # cover BOTH so we don't redeliver either on the next poll.
    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
        update_batches=[
            [
                _msg(update_id=10, chat_id=99999, text="/halt"),
                _msg(update_id=11, chat_id=12345, text="/status"),
            ],
        ],
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=lambda: None,
    )
    await a.start()
    try:
        await _wait_until(lambda: len(a.replies) >= 1)
    finally:
        await a.stop()
    # offset must be one past the LARGEST update_id in the batch.
    assert a.listener_offset == 12


@pytest.mark.asyncio
async def test_listener_disables_after_too_many_crashes(monkeypatch):
    # Force _poll_once to always raise. After _LISTENER_MAX_RESTARTS+1
    # crashes within the window, the listener flips _listener_disabled.
    monkeypatch.setattr(
        "src.notifications.telegram._LISTENER_RESTART_DELAY_S", 0.0,
    )

    crash_count = {"n": 0}

    async def _broken_poll():
        crash_count["n"] += 1
        raise RuntimeError("intentional crash for test")

    a = _ListenerAlerter(
        bot_token="abc:def",
        allowed_chat_ids=[12345],
        config={},
    )
    a.set_command_handlers(
        status_provider=_status_provider_stub, halt_handler=lambda: None,
    )
    a._poll_once = _broken_poll  # type: ignore[assignment]
    await a.start()
    try:
        await _wait_until(lambda: a.listener_disabled, timeout=3.0)
    finally:
        await a.stop()
    # We get at LEAST max_restarts+1 crashes (the +1 is the one that
    # tips len(crash_times) past the threshold).
    from src.notifications.telegram import _LISTENER_MAX_RESTARTS
    assert crash_count["n"] >= _LISTENER_MAX_RESTARTS + 1
    assert a.listener_disabled is True

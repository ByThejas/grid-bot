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

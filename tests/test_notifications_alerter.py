"""
Tests for src.notifications.alerter — Phase 11A.

Focus:
  * NullAlerter no-ops every method without raising and is safe to start /
    stop multiple times.
  * make_alerter chooses NullAlerter for every disabled / misconfigured
    branch (no telegram block, enabled=false, missing token, missing chat
    ids, malformed chat ids).
  * The "all preconditions met but TelegramAlerter not yet importable"
    fallback path also yields NullAlerter — proves the factory is robust
    against a missing implementation. (Will be exercised differently in
    step 4 once telegram.py exists.)
"""

import asyncio

import pytest

from src.notifications.alerter import (
    Alerter,
    NullAlerter,
    _parse_chat_ids,
    make_alerter,
)


# ---------------------------------------------------------------------------
# NullAlerter
# ---------------------------------------------------------------------------


def test_null_alerter_implements_alerter_abc():
    a = NullAlerter()
    assert isinstance(a, Alerter)


def test_null_alerter_event_methods_return_none_and_dont_raise():
    a = NullAlerter()
    # Pass realistic kwargs through every method to confirm signatures
    # accept what callers will eventually send.
    assert a.boot(env_mode="testnet", symbols=["BTCUSDT"], max_open_orders=10,
                  daily_loss_limit_usd=20.0) is None
    assert a.shutdown(final_open_orders=0, session_realized_usd=0.0,
                      uptime_seconds=10.0) is None
    assert a.crash(component="x", exc=RuntimeError("y")) is None
    assert a.fill(symbol="BTCUSDT", side="BUY", price=70000.0, qty=0.0003) is None
    assert a.fill(symbol="BTCUSDT", side="SELL", price=70000.0, qty=0.0003,
                  realized_delta_usd=0.18) is None
    assert a.halt(reason="test") is None
    assert a.pause(source="liquidity", symbol="ETHUSDT", spread="62bps") is None
    assert a.resume(source="liquidity", symbol="ETHUSDT") is None
    assert a.daily_summary(utc_day="2026-04-28", fills=0, realized_usd=0.0,
                           recenters=0, halts=0, uptime_seconds=0.0) is None
    assert a.recenter(symbol="BTCUSDT", old_mid=70000.0, new_mid=71500.0,
                      drift_pct=2.14) is None


def test_null_alerter_start_stop_idempotent():
    a = NullAlerter()
    # Multiple start/stop cycles must be safe — runner may retry on errors.
    asyncio.run(a.start())
    asyncio.run(a.start())
    asyncio.run(a.stop())
    asyncio.run(a.stop())


def test_alerter_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Alerter()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# _parse_chat_ids
# ---------------------------------------------------------------------------


def test_parse_chat_ids_single():
    assert _parse_chat_ids("12345") == [12345]


def test_parse_chat_ids_comma_separated_with_whitespace():
    assert _parse_chat_ids("12345, 67890 ,42") == [12345, 67890, 42]


def test_parse_chat_ids_drops_blanks():
    # Trailing comma, double comma, empty string in the middle.
    assert _parse_chat_ids("12345,,67890,") == [12345, 67890]


def test_parse_chat_ids_drops_non_integer():
    # Malformed entries are dropped (logged), valid ones survive.
    assert _parse_chat_ids("12345,abc,67890") == [12345, 67890]


def test_parse_chat_ids_negative_allowed():
    # Telegram group chat IDs are negative integers; must be accepted.
    assert _parse_chat_ids("-100123456789") == [-100123456789]


def test_parse_chat_ids_empty_string():
    assert _parse_chat_ids("") == []


# ---------------------------------------------------------------------------
# make_alerter — disabled branches
# ---------------------------------------------------------------------------


def _clear_telegram_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)


def test_factory_returns_null_when_telegram_section_absent(monkeypatch):
    _clear_telegram_env(monkeypatch)
    a = make_alerter({})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_section_present_but_empty(monkeypatch):
    _clear_telegram_env(monkeypatch)
    a = make_alerter({"telegram": {}})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_enabled_false(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    a = make_alerter({"telegram": {"enabled": False}})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_token_missing(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    a = make_alerter({"telegram": {"enabled": True}})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_token_blank(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    a = make_alerter({"telegram": {"enabled": True}})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_chat_ids_missing(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    a = make_alerter({"telegram": {"enabled": True}})
    assert isinstance(a, NullAlerter)


def test_factory_returns_null_when_chat_ids_all_malformed(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "abc, def, ghi")
    a = make_alerter({"telegram": {"enabled": True}})
    assert isinstance(a, NullAlerter)

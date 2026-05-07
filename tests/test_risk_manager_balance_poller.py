"""
Tests for the BalancePoller wiring inside RiskManager — Phase 11D step 5.

Covers:
  - RiskManager constructed without poller behaves as pre-Phase-11D
    (uses static config daily_loss_limit_usd).
  - With a wired poller returning a positive equity-derived floor, the
    property returns the dynamic value.
  - When the poller returns 0.0 (e.g. before first refresh), the property
    falls back to static config — so we don't accidentally enforce a
    $0 floor and trip on every fill.
  - check_can_place uses the dynamic floor — a daily-loss check passes
    or trips based on the equity-derived value.
  - trip() emits the dynamic value via alerter.halt(daily_loss_limit_usd=...).

Both static-config and dynamic-poller code paths are exercised so we
catch regressions in either.
"""

import time

import pytest

from src.risk.balance_poller import BalancePollerBase
from src.risk.config import RiskConfig
from src.risk.manager import RiskManager


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FixedFloorPoller(BalancePollerBase):
    """Returns a constant daily_loss_limit_usd; trivial to test against."""
    def __init__(self, floor: float, equity: float = 1000.0):
        self._floor = floor
        self._equity = equity

    async def start(self): return None
    async def stop(self): return None
    def order_size_in_base(self, *a, **k): return 0.0
    def daily_loss_limit_usd(self): return self._floor
    def is_flash_crashed(self): return False
    def current_equity_usdt(self): return self._equity


class _RecordingAlerter:
    """Captures the kwargs passed to alerter.halt for assertion."""
    def __init__(self):
        self.halt_calls: list[dict] = []
    def halt(self, **k): self.halt_calls.append(k)
    # Other event methods unused; default no-op-ish behaviour:
    def boot(self, **k): pass
    def shutdown(self, **k): pass
    def crash(self, **k): pass
    def fill(self, **k): pass
    def pause(self, **k): pass
    def resume(self, **k): pass
    def daily_summary(self, **k): pass
    def recenter(self, **k): pass


def _basic_cfg(daily_loss_limit_usd: float = 20.0) -> RiskConfig:
    return RiskConfig(
        max_open_orders=10,
        daily_loss_limit_usd=daily_loss_limit_usd,
        kill_switch_path="runtime/HALT_TEST",
        poll_interval_s=1.0,
    )


# ---------------------------------------------------------------------------
# Property: daily_loss_limit_usd
# ---------------------------------------------------------------------------


def test_floor_uses_static_config_without_poller():
    """No poller → property returns static config value."""
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0))
    assert risk.daily_loss_limit_usd == 20.0


def test_floor_uses_poller_when_wired_and_positive():
    """Poller returning a positive value wins over static config."""
    poller = _FixedFloorPoller(floor=50.0)
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0), balance_poller=poller)
    assert risk.daily_loss_limit_usd == 50.0


def test_floor_falls_back_to_static_when_poller_returns_zero():
    """
    Important fallback: poller returns 0.0 before its first successful
    refresh. We MUST NOT enforce a $0 floor — that would trip on the
    first negative fill. Static config is the right fallback.
    """
    poller = _FixedFloorPoller(floor=0.0)
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0), balance_poller=poller)
    assert risk.daily_loss_limit_usd == 20.0


# ---------------------------------------------------------------------------
# check_can_place — dynamic floor enforcement
# ---------------------------------------------------------------------------


def _seed_realized_pnl(risk: RiskManager, amount_usd: float) -> None:
    """
    Inject a synthetic realized PnL entry so we can exercise the
    daily-loss-floor check without having to drive real fills through
    the FIFO matcher.
    """
    risk._pnl_ledger.append((time.time(), amount_usd))


def test_can_place_passes_when_realized_above_dynamic_floor():
    poller = _FixedFloorPoller(floor=50.0)
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0), balance_poller=poller)
    # Realized -$30: above the dynamic -$50 floor, below the static -$20 floor.
    # If the dynamic floor isn't being used, this test FAILS (would block).
    _seed_realized_pnl(risk, -30.0)
    assert risk.check_can_place("BTCUSDT", "BUY", 70000.0, 0.001) is None


def test_can_place_blocks_when_realized_below_dynamic_floor():
    poller = _FixedFloorPoller(floor=10.0)
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=100.0), balance_poller=poller)
    # Realized -$15: below the dynamic -$10 floor, above the static -$100 floor.
    # Dynamic floor must trip here.
    _seed_realized_pnl(risk, -15.0)
    blocked = risk.check_can_place("BTCUSDT", "BUY", 70000.0, 0.001)
    assert blocked is not None
    assert "daily loss floor breached" in blocked


def test_can_place_uses_static_when_poller_returns_zero():
    """If poller hasn't refreshed (returns 0), check uses static floor."""
    poller = _FixedFloorPoller(floor=0.0)
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0), balance_poller=poller)
    _seed_realized_pnl(risk, -25.0)  # below static -$20
    blocked = risk.check_can_place("BTCUSDT", "BUY", 70000.0, 0.001)
    assert blocked is not None
    assert "20.00" in blocked   # static value should appear in the message


# ---------------------------------------------------------------------------
# trip() — dynamic floor in the [HALT] alert
# ---------------------------------------------------------------------------


def test_trip_emits_dynamic_floor_in_halt_alert():
    """
    When the poller is wired, the [HALT] alert's daily_loss_limit_usd
    should be the dynamic value. Otherwise the operator's phone shows
    a stale static figure they didn't actually configure.
    """
    poller = _FixedFloorPoller(floor=75.0)
    alerter = _RecordingAlerter()
    risk = RiskManager(
        _basic_cfg(daily_loss_limit_usd=20.0),
        alerter=alerter,
        balance_poller=poller,
    )
    risk.trip("test halt")
    assert len(alerter.halt_calls) == 1
    assert alerter.halt_calls[0]["daily_loss_limit_usd"] == 75.0


def test_trip_emits_static_floor_when_no_poller():
    alerter = _RecordingAlerter()
    risk = RiskManager(_basic_cfg(daily_loss_limit_usd=20.0), alerter=alerter)
    risk.trip("test halt")
    assert alerter.halt_calls[0]["daily_loss_limit_usd"] == 20.0

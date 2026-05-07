"""
Tests for src.risk.balance_poller — Phase 11D dynamic sizing.

Coverage:
  - Pure functions (compute_equity_usdt, compute_target_notional,
    compute_order_size_in_base, compute_daily_loss_limit_usd,
    is_equity_flash_crashed) against canned inputs
  - BalancePollerConfig.from_yaml — defaults + override + missing-block path
  - NullBalancePoller — returns static config, ignores live args
  - BalancePoller — async polling, failover behaviour, flash-crash
    detection, restart-after-recovery alert flow
  - make_balance_poller factory — picks Null vs live based on settings
"""

import asyncio

import pytest

from src.risk.balance_poller import (
    BalancePoller,
    BalancePollerBase,
    BalancePollerConfig,
    NullBalancePoller,
    _Snapshot,
    compute_daily_loss_limit_usd,
    compute_equity_usdt,
    compute_order_size_in_base,
    compute_target_notional,
    is_equity_flash_crashed,
    make_balance_poller,
)


# ---------------------------------------------------------------------------
# compute_equity_usdt
# ---------------------------------------------------------------------------


def test_equity_usdt_only():
    assert compute_equity_usdt({"USDT": 100.0}, {}) == 100.0


def test_equity_usdt_plus_btc_eth():
    balances = {"USDT": 100.0, "BTC": 0.001, "ETH": 0.01}
    prices = {"BTCUSDT": 76000.0, "ETHUSDT": 2250.0}
    # 100 + 0.001*76000 + 0.01*2250 = 100 + 76 + 22.50 = 198.50
    assert compute_equity_usdt(balances, prices) == pytest.approx(198.5)


def test_equity_ignores_other_assets():
    """BNB / USDC and friends are NOT counted."""
    balances = {"USDT": 100.0, "BNB": 5.0, "USDC": 50.0, "BTC": 0.0}
    prices = {"BTCUSDT": 76000.0, "ETHUSDT": 2250.0}
    # Only USDT counts; BNB and USDC are deliberately excluded
    assert compute_equity_usdt(balances, prices) == 100.0


def test_equity_zero_btc_does_not_require_price():
    """A balance of 0 BTC means we don't need BTCUSDT in prices."""
    balances = {"USDT": 50.0, "BTC": 0.0}
    # No BTCUSDT in prices — should not raise
    eq = compute_equity_usdt(balances, {})
    assert eq == 50.0


def test_equity_raises_when_price_missing_for_nonzero_balance():
    balances = {"USDT": 50.0, "BTC": 0.001}
    with pytest.raises(ValueError, match="missing price for BTCUSDT"):
        compute_equity_usdt(balances, {"ETHUSDT": 2250.0})


def test_equity_handles_empty_inputs():
    assert compute_equity_usdt({}, {}) == 0.0


# ---------------------------------------------------------------------------
# compute_target_notional
# ---------------------------------------------------------------------------


def test_target_notional_basic_pct():
    # 0.5% of 200 = 1.0
    assert compute_target_notional(200.0, 0.5) == 1.0


def test_target_notional_capped_when_max_pct_lower():
    # 5% of 200 = 10, but max is 1% = 2 → capped at 2
    assert compute_target_notional(200.0, 5.0, max_order_size_pct=1.0) == 2.0


def test_target_notional_uncapped_when_max_pct_higher():
    # 0.5% of 200 = 1, max is 2% = 4 → uncapped at 1
    assert compute_target_notional(200.0, 0.5, max_order_size_pct=2.0) == 1.0


def test_target_notional_no_cap():
    assert compute_target_notional(1000.0, 0.5, max_order_size_pct=None) == 5.0


def test_target_notional_negative_equity_clamps_to_zero():
    """Equity should never be negative in practice; if it is, target=0."""
    assert compute_target_notional(-100.0, 0.5) == 0.0


# ---------------------------------------------------------------------------
# compute_order_size_in_base
# ---------------------------------------------------------------------------


def test_order_size_in_base_normal_case():
    # Target $25, BTC at $76k, step 0.00001, min_notional $10
    # 25 / 76000 = 0.000328947... → round down to 0.00032 (0.00001 step)
    # 0.00032 * 76000 = $24.32, above $10 floor
    qty = compute_order_size_in_base(25.0, 76000.0, 0.00001, 10.0)
    assert qty == pytest.approx(0.00032)


def test_order_size_in_base_floors_at_min_notional():
    # Target $1 (tiny wallet), BTC at $76k, min_notional $10
    # 1/76000 = 0.000013... → round down to 0 (below step)
    # That's below $10 → floor at $10/76000 = 0.0001316 → round UP to 0.00014
    qty = compute_order_size_in_base(1.0, 76000.0, 0.00001, 10.0)
    notional = qty * 76000.0
    assert notional >= 10.0


def test_order_size_in_base_step_size_rounding():
    # Step size 0.001 (coarser); 0.5/2300 = 0.000217 → floored to 0
    # → must floor to min_notional which is $10/2300 = 0.00435 → up to 0.005
    qty = compute_order_size_in_base(0.5, 2300.0, 0.001, 10.0)
    # 0.005 * 2300 = $11.50, satisfies the $10 floor
    assert qty == pytest.approx(0.005)


def test_order_size_in_base_raises_on_zero_price():
    with pytest.raises(ValueError, match="mid_price must be positive"):
        compute_order_size_in_base(10.0, 0.0, 0.00001, 10.0)


def test_order_size_in_base_raises_on_zero_step():
    with pytest.raises(ValueError, match="step_size must be positive"):
        compute_order_size_in_base(10.0, 76000.0, 0.0, 10.0)


# ---------------------------------------------------------------------------
# compute_daily_loss_limit_usd
# ---------------------------------------------------------------------------


def test_daily_loss_limit_basic():
    # 5% of 200 = 10
    assert compute_daily_loss_limit_usd(200.0, 5.0) == 10.0


def test_daily_loss_limit_at_higher_equity():
    # 5% of 20000 = 1000
    assert compute_daily_loss_limit_usd(20000.0, 5.0) == 1000.0


def test_daily_loss_limit_negative_equity_clamps_to_zero():
    assert compute_daily_loss_limit_usd(-100.0, 5.0) == 0.0


# ---------------------------------------------------------------------------
# is_equity_flash_crashed
# ---------------------------------------------------------------------------


def _snap(t: float, eq: float) -> _Snapshot:
    return _Snapshot(timestamp=t, equity_usdt=eq)


def test_flash_crash_no_history():
    assert is_equity_flash_crashed([], 20.0, 3600.0, 1000.0) is False


def test_flash_crash_single_snapshot_not_enough_data():
    assert is_equity_flash_crashed([_snap(0.0, 200.0)], 20.0, 3600.0, 1000.0) is False


def test_flash_crash_drop_below_threshold_does_not_trip():
    # peak 200, latest 190 = 5% drop, threshold 20% → no
    history = [_snap(0.0, 200.0), _snap(60.0, 190.0)]
    assert is_equity_flash_crashed(history, 20.0, 3600.0, 60.0) is False


def test_flash_crash_drop_above_threshold_trips():
    # peak 200, latest 150 = 25% drop, threshold 20% → yes
    history = [_snap(0.0, 200.0), _snap(60.0, 150.0)]
    assert is_equity_flash_crashed(history, 20.0, 3600.0, 60.0) is True


def test_flash_crash_uses_peak_not_earliest():
    """A drop from peak (mid-window) trips even if earliest equity was lower."""
    # earliest 100 → peak 200 → latest 150 = 25% drop FROM PEAK → trip
    history = [_snap(0.0, 100.0), _snap(30.0, 200.0), _snap(60.0, 150.0)]
    assert is_equity_flash_crashed(history, 20.0, 3600.0, 60.0) is True


def test_flash_crash_ignores_snapshots_outside_window():
    # Big peak 100s ago is OUTSIDE the 60s window → only 200 (at t=80) and 195 (at t=100) considered
    # 195 vs 200 = 2.5% drop → no trip
    history = [_snap(0.0, 1000.0), _snap(80.0, 200.0), _snap(100.0, 195.0)]
    assert is_equity_flash_crashed(history, 20.0, 60.0, 100.0) is False


def test_flash_crash_threshold_zero_disables_check():
    history = [_snap(0.0, 200.0), _snap(60.0, 0.01)]
    assert is_equity_flash_crashed(history, 0.0, 3600.0, 60.0) is False


# ---------------------------------------------------------------------------
# BalancePollerConfig.from_yaml
# ---------------------------------------------------------------------------


def test_config_defaults_from_empty_dict():
    c = BalancePollerConfig.from_yaml({})
    assert c.enabled is False
    assert c.poll_interval_seconds == 1800.0
    assert c.order_size_pct == 0.5
    assert c.daily_loss_pct == 5.0
    assert c.max_order_size_pct is None
    assert c.flash_crash_pause_pct == 20.0
    assert c.flash_crash_window_seconds == 3600.0
    assert c.failover_warn_after_consecutive_failures == 3


def test_config_defaults_from_none():
    c = BalancePollerConfig.from_yaml(None)
    assert c.enabled is False


def test_config_overrides_take_effect():
    c = BalancePollerConfig.from_yaml({
        "enabled": True,
        "poll_interval_minutes": 5,
        "order_size_pct": 1.0,
        "daily_loss_pct": 3.0,
        "max_order_size_pct": 2.0,
        "flash_crash_pause_pct": 15.0,
        "flash_crash_window_minutes": 30,
        "failover_warn_after_consecutive_failures": 5,
    })
    assert c.enabled is True
    assert c.poll_interval_seconds == 300.0
    assert c.order_size_pct == 1.0
    assert c.daily_loss_pct == 3.0
    assert c.max_order_size_pct == 2.0
    assert c.flash_crash_pause_pct == 15.0
    assert c.flash_crash_window_seconds == 1800.0
    assert c.failover_warn_after_consecutive_failures == 5


# ---------------------------------------------------------------------------
# NullBalancePoller
# ---------------------------------------------------------------------------


def test_null_poller_implements_base():
    a = NullBalancePoller({}, 0.0)
    assert isinstance(a, BalancePollerBase)


def test_null_poller_returns_static_sizes():
    a = NullBalancePoller(
        static_order_sizes_in_base={"BTCUSDT": 0.0003, "ETHUSDT": 0.005},
        static_daily_loss_limit_usd=20.0,
    )
    # Live args (mid_price, step, min_notional) are ignored in null mode.
    assert a.order_size_in_base("BTCUSDT", 76000.0, 0.00001, 10.0) == 0.0003
    assert a.order_size_in_base("ETHUSDT", 2300.0, 0.0001, 10.0) == 0.005
    assert a.order_size_in_base("UNKNOWN", 1.0, 1.0, 1.0) == 0.0


def test_null_poller_returns_static_floor():
    a = NullBalancePoller({}, 20.0)
    assert a.daily_loss_limit_usd() == 20.0


def test_null_poller_never_flash_crashes():
    a = NullBalancePoller({}, 20.0)
    assert a.is_flash_crashed() is False


def test_null_poller_equity_is_none():
    a = NullBalancePoller({}, 20.0)
    assert a.current_equity_usdt() is None


@pytest.mark.asyncio
async def test_null_poller_start_stop_idempotent():
    a = NullBalancePoller({}, 20.0)
    await a.start()
    await a.start()
    await a.stop()
    await a.stop()


# ---------------------------------------------------------------------------
# BalancePoller (live)
# ---------------------------------------------------------------------------


class _FakeExchange:
    """
    Minimal duck-typed exchange. Configurable balances + prices, optional
    failure injection.
    """
    def __init__(self, balances, prices, fail_count=0):
        self._balances = dict(balances)
        self._prices = dict(prices)
        self._fail_count = fail_count
        self.balance_calls = 0
        self.price_calls = 0

    async def get_account_balances(self):
        self.balance_calls += 1
        if self._fail_count > 0:
            self._fail_count -= 1
            raise RuntimeError("simulated balance fetch failure")
        return dict(self._balances)

    async def get_ticker_price(self, symbol):
        self.price_calls += 1
        return self._prices[symbol]


def _basic_cfg(**overrides):
    base = dict(
        enabled=True,
        poll_interval_seconds=0.05,   # fast for tests
        order_size_pct=0.5,
        daily_loss_pct=5.0,
        max_order_size_pct=None,
        flash_crash_pause_pct=20.0,
        flash_crash_window_seconds=3600.0,
        failover_warn_after_consecutive_failures=3,
    )
    base.update(overrides)
    return BalancePollerConfig(**base)


@pytest.mark.asyncio
async def test_live_poller_first_refresh_populates_equity():
    ex = _FakeExchange(
        balances={"USDT": 100.0, "BTC": 0.001, "ETH": 0.01},
        prices={"BTCUSDT": 76000.0, "ETHUSDT": 2250.0},
    )
    a = BalancePoller(ex, _basic_cfg(), now_fn=lambda: 1000.0)
    await a.start()
    try:
        assert a.current_equity_usdt() == pytest.approx(198.5)
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_live_poller_first_refresh_failure_raises():
    ex = _FakeExchange({}, {}, fail_count=1)
    a = BalancePoller(ex, _basic_cfg())
    with pytest.raises(RuntimeError, match="simulated balance fetch failure"):
        await a.start()
    # Task was not spawned because start() raised before that step.
    assert a._task is None


@pytest.mark.asyncio
async def test_live_poller_order_size_uses_equity():
    ex = _FakeExchange(
        balances={"USDT": 1000.0, "BTC": 0.0, "ETH": 0.0},
        prices={"BTCUSDT": 76000.0, "ETHUSDT": 2250.0},
    )
    a = BalancePoller(ex, _basic_cfg(order_size_pct=1.0), now_fn=lambda: 0.0)
    await a.start()
    try:
        # 1% of $1000 = $10 target, BTC at $76k, step 0.00001
        # 10/76000 = 0.0001315 → floored to 0.00013 (0.00001 step) → $9.88, below $10
        # → floor at min_notional: ceil(10/76000 / 0.00001) * 0.00001 = 0.00014 → $10.64
        qty = a.order_size_in_base("BTCUSDT", 76000.0, 0.00001, 10.0)
        assert qty * 76000.0 >= 10.0
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_live_poller_daily_floor_uses_equity():
    ex = _FakeExchange(
        balances={"USDT": 1000.0},
        prices={"BTCUSDT": 76000.0, "ETHUSDT": 2250.0},
    )
    a = BalancePoller(ex, _basic_cfg(daily_loss_pct=5.0), now_fn=lambda: 0.0)
    await a.start()
    try:
        assert a.daily_loss_limit_usd() == 50.0
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_live_poller_failover_alert_after_threshold():
    """
    After N consecutive poll failures, alerter.pause should fire once.
    Then on a successful poll, alerter.resume should fire once.
    """
    ex = _FakeExchange(
        balances={"USDT": 100.0},
        prices={"BTCUSDT": 76000.0, "ETHUSDT": 2250.0},
    )

    # Capture alerter calls
    alerter_pauses = []
    alerter_resumes = []

    class _CountingAlerter:
        def pause(self, **k): alerter_pauses.append(k)
        def resume(self, **k): alerter_resumes.append(k)

    cfg = _basic_cfg(
        failover_warn_after_consecutive_failures=2,
        poll_interval_seconds=0.01,
    )
    a = BalancePoller(ex, cfg, alerter=_CountingAlerter(), now_fn=lambda: 0.0)
    await a.start()  # successful first refresh
    try:
        # Inject failures into the next several polls.
        ex._fail_count = 3
        # Wait until failover triggers
        for _ in range(100):
            if alerter_pauses:
                break
            await asyncio.sleep(0.01)
        assert len(alerter_pauses) == 1
        assert alerter_pauses[0]["source"] == "balance_poller"
        # Wait for recovery (fail_count exhausted, polls succeed again)
        for _ in range(100):
            if alerter_resumes:
                break
            await asyncio.sleep(0.01)
        assert len(alerter_resumes) == 1
        assert alerter_resumes[0]["source"] == "balance_poller"
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_live_poller_flash_crash_via_history():
    """
    Drive equity through a peak-then-drop scenario by mutating the fake
    exchange's balances between successive polls.
    """
    ex = _FakeExchange(
        balances={"USDT": 100.0, "BTC": 0.001},  # equity ~ 100 + 76 = 176
        prices={"BTCUSDT": 76000.0, "ETHUSDT": 2250.0},
    )

    fake_clock = [0.0]
    def _now():
        return fake_clock[0]

    cfg = _basic_cfg(
        flash_crash_pause_pct=20.0,
        flash_crash_window_seconds=300.0,
        poll_interval_seconds=0.01,
    )
    a = BalancePoller(ex, cfg, now_fn=_now)
    await a.start()  # snapshot at t=0, equity ~ 176
    try:
        assert a.is_flash_crashed() is False
        # Move time forward, drop balances ~30%
        fake_clock[0] = 60.0
        ex._balances = {"USDT": 70.0, "BTC": 0.0007}  # ~ 70 + 53.2 = 123.2 (~30% drop from 176)
        # Force a refresh by waiting for the poll interval to fire
        for _ in range(100):
            if a.is_flash_crashed():
                break
            await asyncio.sleep(0.01)
        assert a.is_flash_crashed() is True
    finally:
        await a.stop()


# ---------------------------------------------------------------------------
# make_balance_poller factory
# ---------------------------------------------------------------------------


def test_factory_returns_null_when_dynamic_sizing_absent():
    """No dynamic_sizing block → null poller, returns static config."""
    settings = {}
    p = make_balance_poller(
        settings, exchange=None, alerter=None,
        static_order_sizes_in_base={"BTCUSDT": 0.0003, "ETHUSDT": 0.005},
        static_daily_loss_limit_usd=20.0,
    )
    assert isinstance(p, NullBalancePoller)
    assert p.daily_loss_limit_usd() == 20.0
    assert p.order_size_in_base("BTCUSDT", 1, 1, 1) == 0.0003


def test_factory_returns_null_when_disabled():
    settings = {"dynamic_sizing": {"enabled": False, "order_size_pct": 99.0}}
    p = make_balance_poller(
        settings, exchange=None, alerter=None,
        static_order_sizes_in_base={"BTCUSDT": 0.0003},
        static_daily_loss_limit_usd=20.0,
    )
    assert isinstance(p, NullBalancePoller)
    # Should ignore dynamic config and return static.
    assert p.order_size_in_base("BTCUSDT", 1, 1, 1) == 0.0003


def test_factory_returns_live_when_enabled():
    settings = {"dynamic_sizing": {"enabled": True, "order_size_pct": 0.5}}
    p = make_balance_poller(
        settings,
        exchange=_FakeExchange({"USDT": 100.0}, {"BTCUSDT": 1, "ETHUSDT": 1}),
        alerter=None,
        static_order_sizes_in_base={},
        static_daily_loss_limit_usd=0.0,
    )
    assert isinstance(p, BalancePoller)
    assert not isinstance(p, NullBalancePoller)

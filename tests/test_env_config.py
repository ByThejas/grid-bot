"""
Tests for src.exchange.env_config — Phase 10 mainnet toggle.

Focus:
  - CLI override > BINANCE_ENV > settings.yaml use_testnet
  - Invalid mode strings raise loudly (we never silently pick a default)
  - Mainnet without --live or BINANCE_LIVE raises — testnet never does
  - Credential loading picks mode-specific first, falls back to generic
  - Missing credentials name the exact missing env vars
"""

import os
import sys
import traceback

from src.exchange.env_config import (
    EnvConfigError,
    load_credentials,
    render_banner,
    require_live_acknowledgement,
    resolve_env_mode,
)
from src.risk.config import RiskConfig


# ---------------------------------------------------------------------------
# Helpers — isolate env vars per test so order doesn't matter.
# ---------------------------------------------------------------------------

_ENV_VARS = [
    "BINANCE_ENV", "BINANCE_LIVE",
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "BINANCE_WS_API_KEY", "BINANCE_WS_PRIVATE_KEY_PATH",
    "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET",
    "BINANCE_TESTNET_WS_API_KEY", "BINANCE_TESTNET_WS_PRIVATE_KEY_PATH",
    "BINANCE_MAINNET_API_KEY", "BINANCE_MAINNET_API_SECRET",
    "BINANCE_MAINNET_WS_API_KEY", "BINANCE_MAINNET_WS_PRIVATE_KEY_PATH",
]


def _clear_env() -> dict:
    saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# resolve_env_mode
# ---------------------------------------------------------------------------


def test_cli_override_wins_over_everything():
    saved = _clear_env()
    try:
        os.environ["BINANCE_ENV"] = "mainnet"
        settings = {"exchange": {"use_testnet": False}}
        assert resolve_env_mode(settings, cli_override="testnet") == "testnet"
        assert resolve_env_mode(settings, cli_override="mainnet") == "mainnet"
    finally:
        _restore_env(saved)


def test_env_var_beats_settings():
    saved = _clear_env()
    try:
        os.environ["BINANCE_ENV"] = "mainnet"
        settings = {"exchange": {"use_testnet": True}}
        assert resolve_env_mode(settings) == "mainnet"
    finally:
        _restore_env(saved)


def test_settings_yaml_fallback():
    saved = _clear_env()
    try:
        assert resolve_env_mode({"exchange": {"use_testnet": True}}) == "testnet"
        assert resolve_env_mode({"exchange": {"use_testnet": False}}) == "mainnet"
    finally:
        _restore_env(saved)


def test_settings_default_is_testnet_when_unspecified():
    saved = _clear_env()
    try:
        assert resolve_env_mode({}) == "testnet"
    finally:
        _restore_env(saved)


def test_invalid_cli_override_raises():
    saved = _clear_env()
    try:
        try:
            resolve_env_mode({}, cli_override="production")
        except EnvConfigError as e:
            assert "production" in str(e)
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


def test_invalid_env_var_raises():
    saved = _clear_env()
    try:
        os.environ["BINANCE_ENV"] = "live"
        try:
            resolve_env_mode({})
        except EnvConfigError:
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


# ---------------------------------------------------------------------------
# require_live_acknowledgement
# ---------------------------------------------------------------------------


def test_testnet_never_requires_ack():
    saved = _clear_env()
    try:
        # No flag, no env — testnet still fine.
        require_live_acknowledgement("testnet", cli_live=False)
    finally:
        _restore_env(saved)


def test_mainnet_without_ack_raises():
    saved = _clear_env()
    try:
        try:
            require_live_acknowledgement("mainnet", cli_live=False)
        except EnvConfigError as e:
            assert "MAINNET" in str(e)
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


def test_mainnet_with_cli_flag_ok():
    saved = _clear_env()
    try:
        require_live_acknowledgement("mainnet", cli_live=True)
    finally:
        _restore_env(saved)


def test_mainnet_with_env_ack_ok():
    saved = _clear_env()
    try:
        for val in ("yes", "YES", "true", "1"):
            os.environ["BINANCE_LIVE"] = val
            require_live_acknowledgement("mainnet", cli_live=False)
    finally:
        _restore_env(saved)


def test_mainnet_with_garbage_env_ack_raises():
    saved = _clear_env()
    try:
        os.environ["BINANCE_LIVE"] = "maybe"
        try:
            require_live_acknowledgement("mainnet", cli_live=False)
        except EnvConfigError:
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


# ---------------------------------------------------------------------------
# load_credentials
# ---------------------------------------------------------------------------


def test_mode_specific_keys_win():
    saved = _clear_env()
    try:
        os.environ["BINANCE_API_KEY"] = "generic-key"
        os.environ["BINANCE_API_SECRET"] = "generic-secret"
        os.environ["BINANCE_WS_API_KEY"] = "generic-ws"
        os.environ["BINANCE_WS_PRIVATE_KEY_PATH"] = "generic-path"
        os.environ["BINANCE_MAINNET_API_KEY"] = "main-key"
        os.environ["BINANCE_MAINNET_API_SECRET"] = "main-secret"
        os.environ["BINANCE_MAINNET_WS_API_KEY"] = "main-ws"
        os.environ["BINANCE_MAINNET_WS_PRIVATE_KEY_PATH"] = "main-path"

        c = load_credentials("mainnet")
        assert c.api_key == "main-key"
        assert c.api_secret == "main-secret"
        assert c.ws_api_key == "main-ws"
        assert c.ws_private_key_path == "main-path"
    finally:
        _restore_env(saved)


def test_generic_fallback_for_testnet():
    # Pre-Phase-10 .env layout (only generic vars) keeps working in testnet.
    saved = _clear_env()
    try:
        os.environ["BINANCE_API_KEY"] = "g-key"
        os.environ["BINANCE_API_SECRET"] = "g-secret"
        os.environ["BINANCE_WS_API_KEY"] = "g-ws"
        os.environ["BINANCE_WS_PRIVATE_KEY_PATH"] = "g-path"

        c = load_credentials("testnet")
        assert c.api_key == "g-key"
        assert c.api_secret == "g-secret"
        assert c.ws_api_key == "g-ws"
        assert c.ws_private_key_path == "g-path"
    finally:
        _restore_env(saved)


def test_mainnet_refuses_generic_fallback():
    # CRITICAL SAFETY TEST. An operator with only generic BINANCE_* vars set
    # (e.g. pre-Phase-10 testnet .env) must NOT be able to run mainnet with
    # those keys. Mainnet demands explicit BINANCE_MAINNET_* vars and errors
    # out otherwise, because sharing a keypair across envs would mean a
    # testnet key could accidentally authorise a live trade.
    saved = _clear_env()
    try:
        os.environ["BINANCE_API_KEY"] = "testnet-key"
        os.environ["BINANCE_API_SECRET"] = "testnet-secret"
        os.environ["BINANCE_WS_API_KEY"] = "testnet-ws"
        os.environ["BINANCE_WS_PRIVATE_KEY_PATH"] = "testnet-path"
        try:
            load_credentials("mainnet")
        except EnvConfigError as e:
            msg = str(e)
            assert "BINANCE_MAINNET_API_KEY" in msg
            # The error message must NOT suggest BINANCE_API_KEY as an alt
            # for mainnet — that would be suggesting the unsafe thing.
            assert "(or BINANCE_API_KEY)" not in msg
            return
        raise AssertionError("expected EnvConfigError — mainnet must refuse generic fallback")
    finally:
        _restore_env(saved)


def test_missing_credentials_names_them():
    saved = _clear_env()
    try:
        try:
            load_credentials("mainnet")
        except EnvConfigError as e:
            msg = str(e)
            assert "BINANCE_MAINNET_API_KEY" in msg
            assert "BINANCE_MAINNET_API_SECRET" in msg
            assert "BINANCE_MAINNET_WS_API_KEY" in msg
            assert "BINANCE_MAINNET_WS_PRIVATE_KEY_PATH" in msg
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


def test_partial_credentials_still_raises():
    saved = _clear_env()
    try:
        # Only REST creds set, WS creds missing.
        os.environ["BINANCE_MAINNET_API_KEY"] = "a"
        os.environ["BINANCE_MAINNET_API_SECRET"] = "b"
        try:
            load_credentials("mainnet")
        except EnvConfigError as e:
            assert "WS_API_KEY" in str(e)
            assert "WS_PRIVATE_KEY_PATH" in str(e)
            return
        raise AssertionError("expected EnvConfigError")
    finally:
        _restore_env(saved)


# ---------------------------------------------------------------------------
# render_banner
# ---------------------------------------------------------------------------


def test_banner_labels_mainnet_as_real_money():
    text = render_banner("mainnet", dry_run=False)
    assert "MAINNET" in text
    assert "REAL MONEY" in text


def test_banner_labels_testnet_as_paper():
    text = render_banner("testnet", dry_run=False)
    assert "TESTNET" in text
    assert "paper" in text.lower()


def test_banner_surfaces_dry_run_flag():
    text = render_banner("mainnet", dry_run=True)
    assert "DRY-RUN" in text


# ---------------------------------------------------------------------------
# RiskConfig mainnet overrides — part of the same Phase 10 toggle
# ---------------------------------------------------------------------------


def test_risk_config_testnet_ignores_overrides():
    settings = {
        "risk": {"max_open_orders": 20, "daily_loss_limit_usd": 500.0},
        "risk_overrides": {"mainnet": {"max_open_orders": 6, "daily_loss_limit_usd": 50.0}},
    }
    c = RiskConfig.from_yaml(settings, env_mode="testnet")
    assert c.max_open_orders == 20
    assert c.daily_loss_limit_usd == 500.0


def test_risk_config_mainnet_merges_overrides():
    settings = {
        "risk": {"max_open_orders": 20, "daily_loss_limit_usd": 500.0},
        "risk_overrides": {"mainnet": {"max_open_orders": 6, "daily_loss_limit_usd": 50.0}},
    }
    c = RiskConfig.from_yaml(settings, env_mode="mainnet")
    assert c.max_open_orders == 6
    assert c.daily_loss_limit_usd == 50.0


def test_risk_config_mainnet_partial_overrides_keep_base():
    # Only override one field — the other inherits from the base block.
    settings = {
        "risk": {"max_open_orders": 20, "daily_loss_limit_usd": 500.0},
        "risk_overrides": {"mainnet": {"max_open_orders": 6}},
    }
    c = RiskConfig.from_yaml(settings, env_mode="mainnet")
    assert c.max_open_orders == 6
    assert c.daily_loss_limit_usd == 500.0  # base value preserved


def test_risk_config_unknown_env_behaves_like_testnet():
    # A typo'd env_mode must NOT accidentally apply mainnet overrides.
    settings = {
        "risk": {"max_open_orders": 20},
        "risk_overrides": {"mainnet": {"max_open_orders": 6}},
    }
    c = RiskConfig.from_yaml(settings, env_mode="prod")
    assert c.max_open_orders == 20


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


_TESTS = [
    test_cli_override_wins_over_everything,
    test_env_var_beats_settings,
    test_settings_yaml_fallback,
    test_settings_default_is_testnet_when_unspecified,
    test_invalid_cli_override_raises,
    test_invalid_env_var_raises,
    test_testnet_never_requires_ack,
    test_mainnet_without_ack_raises,
    test_mainnet_with_cli_flag_ok,
    test_mainnet_with_env_ack_ok,
    test_mainnet_with_garbage_env_ack_raises,
    test_mode_specific_keys_win,
    test_generic_fallback_for_testnet,
    test_mainnet_refuses_generic_fallback,
    test_missing_credentials_names_them,
    test_partial_credentials_still_raises,
    test_banner_labels_mainnet_as_real_money,
    test_banner_labels_testnet_as_paper,
    test_banner_surfaces_dry_run_flag,
    test_risk_config_testnet_ignores_overrides,
    test_risk_config_mainnet_merges_overrides,
    test_risk_config_mainnet_partial_overrides_keep_base,
    test_risk_config_unknown_env_behaves_like_testnet,
]


def _main() -> None:
    passed = failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _main()

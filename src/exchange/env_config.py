"""
env_config — testnet/mainnet resolution and credential loading.

WHY A DEDICATED MODULE
Up to Phase 9, the bot only ever ran against testnet. Phase 10 introduces
a real-money path, so the "which environment am I in, and do I have the
right keys for it" question gains teeth. This module is the single place
that decides:

  1. Am I running testnet or mainnet?
  2. Do I have the credentials for that env?
  3. If mainnet, did the operator explicitly acknowledge it?

Answering those three in one typed, testable helper keeps the grid_runner
free of environment-sniffing logic and makes the decision easy to verify.

RESOLUTION ORDER (most specific wins)
  1. CLI --env flag (explicit, always wins)
  2. BINANCE_ENV env var (good for containers / scheduled runs)
  3. settings.yaml exchange.use_testnet (legacy default — True means testnet)

LIVE-TRADING GATE
Mainnet requires explicit opt-in, via EITHER:
  * CLI --live flag, OR
  * BINANCE_LIVE=yes in the environment
This is the "I understand this is real money" gate. Without it, selecting
mainnet aborts with a clear error message rather than silently trading.

CREDENTIAL SLOTS
Two env-scoped slots, plus generic fallback for backward-compat with the
pre-Phase-10 .env layout:

  Mainnet (highest priority in mainnet mode):
    BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET
    BINANCE_MAINNET_WS_API_KEY / BINANCE_MAINNET_WS_PRIVATE_KEY_PATH

  Testnet (highest priority in testnet mode):
    BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
    BINANCE_TESTNET_WS_API_KEY / BINANCE_TESTNET_WS_PRIVATE_KEY_PATH

  Generic fallback (TESTNET ONLY — never applied in mainnet mode):
    BINANCE_API_KEY / BINANCE_API_SECRET
    BINANCE_WS_API_KEY / BINANCE_WS_PRIVATE_KEY_PATH

This means an existing pre-Phase-10 .env that only has the generic keys
keeps working in testnet mode. Mainnet REQUIRES explicit BINANCE_MAINNET_*
vars — we intentionally refuse to fall back to the generic slot for live
trading, so an operator who set only testnet keys can NEVER accidentally
route orders to mainnet with a stale key. Two separate Binance accounts
(and two distinct API keys) is the correct operational setup.
"""

import os
from dataclasses import dataclass
from typing import Optional


EnvMode = str  # "testnet" | "mainnet" — kept as str for py<3.11 Literal ergonomics


@dataclass(frozen=True)
class BinanceCredentials:
    api_key: str
    api_secret: str
    ws_api_key: str
    ws_private_key_path: str


class EnvConfigError(RuntimeError):
    """Raised when env resolution or credential loading fails loudly."""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_env_mode(settings: dict, cli_override: Optional[str] = None) -> EnvMode:
    """
    Decide testnet vs mainnet. Precedence: CLI > BINANCE_ENV > settings.yaml.

    Raises EnvConfigError on an invalid string (anything other than the two
    accepted values). We refuse to guess.
    """
    if cli_override:
        mode = cli_override.strip().lower()
        if mode not in ("testnet", "mainnet"):
            raise EnvConfigError(
                f"--env must be 'testnet' or 'mainnet', got {cli_override!r}"
            )
        return mode

    env_var = os.getenv("BINANCE_ENV", "").strip().lower()
    if env_var:
        if env_var not in ("testnet", "mainnet"):
            raise EnvConfigError(
                f"BINANCE_ENV must be 'testnet' or 'mainnet', got {env_var!r}"
            )
        return env_var

    use_testnet = bool(settings.get("exchange", {}).get("use_testnet", True))
    return "testnet" if use_testnet else "mainnet"


def require_live_acknowledgement(env: EnvMode, cli_live: bool) -> None:
    """
    Enforce the 'real money' gate for mainnet. No-op for testnet.

    Accepts EITHER --live CLI flag OR BINANCE_LIVE=yes|true|1 in env.
    Anything else on mainnet = EnvConfigError. Testnet ignores this entirely.
    """
    if env != "mainnet":
        return
    env_ack = os.getenv("BINANCE_LIVE", "").strip().lower() in ("yes", "true", "1")
    if cli_live or env_ack:
        return
    raise EnvConfigError(
        "MAINNET selected but the live-trading acknowledgement is MISSING.\n"
        "  Pass --live on the command line, OR set BINANCE_LIVE=yes in the\n"
        "  environment. This is the 'I understand this is real money' gate.\n"
        "  If you didn't mean to run mainnet, switch back with --env testnet\n"
        "  or flip exchange.use_testnet back to true in settings.yaml."
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_credentials(env: EnvMode) -> BinanceCredentials:
    """
    Load the four Binance credential values for the chosen env.

    Testnet: mode-specific var → generic fallback → raise.
    Mainnet: mode-specific var → raise. NO generic fallback.

    The asymmetry is intentional. Falling back to BINANCE_API_KEY on
    mainnet is the exact scenario where an operator with a testnet-only
    .env could accidentally ship a mainnet order — Binance would (usually)
    reject it, but "usually" is not a safety property. By refusing the
    fallback, the worst a misconfigured mainnet run can do is exit 2
    before connecting.
    """
    prefix = "BINANCE_TESTNET_" if env == "testnet" else "BINANCE_MAINNET_"
    allow_generic_fallback = env == "testnet"

    def _pick(specific_name: str, generic_name: str) -> str:
        val = os.getenv(prefix + specific_name)
        if val:
            return val
        if allow_generic_fallback:
            return os.getenv(generic_name, "")
        return ""

    api_key = _pick("API_KEY", "BINANCE_API_KEY")
    api_secret = _pick("API_SECRET", "BINANCE_API_SECRET")
    ws_api_key = _pick("WS_API_KEY", "BINANCE_WS_API_KEY")
    ws_priv = _pick("WS_PRIVATE_KEY_PATH", "BINANCE_WS_PRIVATE_KEY_PATH")

    def _name(specific: str, generic: str) -> str:
        return f"{prefix}{specific} (or {generic})" if allow_generic_fallback else f"{prefix}{specific}"

    missing: list[str] = []
    if not api_key:
        missing.append(_name("API_KEY", "BINANCE_API_KEY"))
    if not api_secret:
        missing.append(_name("API_SECRET", "BINANCE_API_SECRET"))
    if not ws_api_key:
        missing.append(_name("WS_API_KEY", "BINANCE_WS_API_KEY"))
    if not ws_priv:
        missing.append(_name("WS_PRIVATE_KEY_PATH", "BINANCE_WS_PRIVATE_KEY_PATH"))

    if missing:
        hint = ""
        if env == "mainnet":
            hint = ("\n  Note: mainnet does NOT fall back to the generic BINANCE_* vars — "
                    "you must set the BINANCE_MAINNET_* variables explicitly. This is the "
                    "safety belt that stops testnet keys from being sent to mainnet.")
        raise EnvConfigError(
            f"Missing Binance credentials for env={env}:\n  - "
            + "\n  - ".join(missing)
            + "\n  Set these in your .env file. See .env.example for the full layout."
            + hint
        )

    return BinanceCredentials(
        api_key=api_key,
        api_secret=api_secret,
        ws_api_key=ws_api_key,
        ws_private_key_path=ws_priv,
    )


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


def render_banner(env: EnvMode, dry_run: bool) -> str:
    """
    Big loud banner, printed once at startup BEFORE the logger spins up.

    Mainnet gets a red-flag-adjacent banner so it's impossible to miss if
    you glance at the terminal. Dry-run is surfaced too because it changes
    the semantics dramatically: dry-run mainnet is NOT placing orders.
    """
    line = "=" * 64
    if env == "mainnet":
        mode_line = "  Binance environment: MAINNET  (REAL MONEY — orders WILL fill)"
    else:
        mode_line = "  Binance environment: TESTNET  (paper trading)"
    dr = "\n  DRY-RUN: no orders will actually be placed" if dry_run else ""
    return f"\n{line}\n{mode_line}{dr}\n{line}\n"

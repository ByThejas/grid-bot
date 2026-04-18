"""
Grid Trading Bot — Entry Point

This is where everything starts. The boot sequence:
1. Load configuration (settings.yaml)
2. Load secrets (.env file — API keys)
3. Initialize components (exchange, signals, grid, risk)
4. Start the main event loop

We'll fill in steps 3-4 in later phases. For now, this proves
the config pipeline works end-to-end.
"""

import asyncio
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.utils.logger import get_logger

logger = get_logger("main")


def load_config() -> dict:
    """
    Load settings from config/settings.yaml.

    WHY YAML over JSON?
    - YAML supports comments (JSON doesn't) — crucial for a config
      file with 30+ parameters that each need explanation
    - Cleaner syntax for nested structures
    """
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"

    if not config_path.exists():
        logger.error("config not found", path=str(config_path))
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    logger.info("config loaded",
                pairs=[p["symbol"] for p in config["trading"]["pairs"]],
                grid_levels=config["grid"]["levels_above"] + config["grid"]["levels_below"],
                testnet=config["exchange"]["use_testnet"])

    return config


def load_secrets() -> dict:
    """
    Load API keys from .env file.

    WHY .env and not settings.yaml?
    - Secrets must NEVER be in version control
    - .env is gitignored, settings.yaml is committed
    - In production (AWS), these come from Secrets Manager instead
    """
    load_dotenv()

    secrets = {
        "binance_api_key": os.getenv("BINANCE_API_KEY", ""),
        "binance_api_secret": os.getenv("BINANCE_API_SECRET", ""),
    }

    if not secrets["binance_api_key"]:
        logger.warning("no API key found — running in dry-run mode")

    return secrets


def validate_config(config: dict) -> bool:
    """
    Sanity-check configuration before starting.

    This catches typos and bad values BEFORE they cause
    mysterious failures at 3am.
    """
    errors = []

    # Grid spacing must be wide enough to cover fees
    # Binance maker fee = 0.1%, so round-trip = 0.2%
    # Spacing must be > 0.2% to be profitable
    min_profitable = 0.2
    if config["grid"]["base_spacing_pct"] <= min_profitable:
        errors.append(
            f"base_spacing_pct ({config['grid']['base_spacing_pct']}%) "
            f"must be > {min_profitable}% to cover round-trip fees"
        )

    # Signal weights must sum to 1.0
    weights = config["signals"]
    total = (weights["supply_demand_weight"]
             + weights["liquidity_weight"]
             + weights["sentiment_weight"])
    if abs(total - 1.0) > 0.001:
        errors.append(f"signal weights sum to {total}, must be 1.0")

    # Drawdown tiers must be ordered
    dd = config["risk"]["drawdown"]
    if not (dd["soft_limit_pct"] < dd["hard_limit_pct"] < dd["kill_pct"]):
        errors.append("drawdown tiers must be: soft < hard < kill")

    if errors:
        for e in errors:
            logger.error("config validation failed", error=e)
        return False

    logger.info("config validation passed")
    return True


async def main():
    """Main entry point — boots the trading system."""
    logger.info("=" * 50)
    logger.info("grid bot starting")
    logger.info("=" * 50)

    # Step 1: Load config
    config = load_config()

    # Step 2: Load secrets
    secrets = load_secrets()

    # Step 3: Validate
    if not validate_config(config):
        logger.error("aborting due to config errors")
        sys.exit(1)

    # Step 4: Print startup summary
    for pair in config["trading"]["pairs"]:
        symbol = pair["symbol"]
        size = config["grid"]["order_size"].get(symbol, "NOT SET")
        logger.info("pair configured",
                     symbol=symbol,
                     order_size=size,
                     levels=config["grid"]["levels_above"] + config["grid"]["levels_below"])

    logger.info("startup complete",
                mode="TESTNET" if config["exchange"]["use_testnet"] else "LIVE",
                max_exposure=f"{config['risk']['max_exposure_pct']}%",
                kill_switch=f"{config['risk']['drawdown']['kill_pct']}% drawdown")

    # === Phases 2-5 will add: ===
    # - Exchange connection
    # - Market data feeds
    # - Signal engines
    # - Grid engine
    # - Risk manager
    # - Main trading loop

    logger.info("bot is ready — components will be added in next phases")


if __name__ == "__main__":
    asyncio.run(main())

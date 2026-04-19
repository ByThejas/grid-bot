"""
Emergency cleanup — cancel every open order on configured symbols.

WHY this exists:
The first live testnet run left orders dangling because cancel_all_orders
crashed during shutdown. This script uses the (now-fixed) bulk-cancel
endpoint to wipe the slate, so we can re-run the engine cleanly.

USAGE (venv activated, from repo root):
    python -m scripts.cancel_all_dangling
"""

import asyncio
import os
import sys

import yaml
from dotenv import load_dotenv

from src.exchange.binance import BinanceExchange


SETTINGS_PATH = "config/settings.yaml"


async def main() -> None:
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("BINANCE_API_KEY / BINANCE_API_SECRET missing in .env")
        sys.exit(2)

    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    use_testnet = bool(settings.get("exchange", {}).get("use_testnet", True))
    symbols = [p["symbol"] for p in settings["trading"]["pairs"]]

    print(f"Connecting (testnet={use_testnet})")
    exchange = BinanceExchange(api_key=api_key, api_secret=api_secret, testnet=use_testnet)
    await exchange.connect()

    try:
        for symbol in symbols:
            open_orders = await exchange.get_open_orders(symbol)
            print(f"{symbol}: {len(open_orders)} open before cancel")
            n = await exchange.cancel_all_orders(symbol)
            print(f"{symbol}: cancelled {n}")
            after = await exchange.get_open_orders(symbol)
            print(f"{symbol}: {len(after)} open after cancel")
    finally:
        await exchange.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

"""
Quick test to verify Binance testnet connection works.

Run with:
    python -m tests.test_binance_connection

This is NOT a unit test — it's a live integration test that hits
the real Binance testnet API. You need valid testnet keys in .env.
"""

import asyncio
import os

from dotenv import load_dotenv

from src.exchange.binance import BinanceExchange
from src.utils.logger import get_logger

logger = get_logger("connection_test")


async def test_connection():
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if not api_key or api_key == "your_testnet_api_key_here":
        logger.error("set your testnet API keys in .env first!")
        return

    exchange = BinanceExchange(
        api_key=api_key,
        api_secret=api_secret,
        testnet=True,
    )

    # Test 1: Connect
    logger.info("--- TEST 1: Connection ---")
    await exchange.connect()

    # Test 2: Get price
    logger.info("--- TEST 2: Ticker Price ---")
    btc_price = await exchange.get_ticker_price("BTCUSDT")
    eth_price = await exchange.get_ticker_price("ETHUSDT")
    logger.info("prices fetched", btc=btc_price, eth=eth_price)

    # Test 3: Get order book
    logger.info("--- TEST 3: Order Book ---")
    book = await exchange.get_order_book("BTCUSDT", depth=10)
    logger.info("order book snapshot",
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                mid_price=book.mid_price,
                spread=f"${book.spread:.2f}",
                spread_pct=f"{book.spread_pct:.4f}%",
                bid_levels=len(book.bids),
                ask_levels=len(book.asks))

    # Test 4: Get balances
    logger.info("--- TEST 4: Balances ---")
    usdt_balance = await exchange.get_balance("USDT")
    btc_balance = await exchange.get_balance("BTC")
    logger.info("balances", usdt=usdt_balance, btc=btc_balance)

    # Test 5: Get open orders
    logger.info("--- TEST 5: Open Orders ---")
    orders = await exchange.get_open_orders("BTCUSDT")
    logger.info("open orders", count=len(orders))

    await exchange.disconnect()
    logger.info("all tests passed!")


if __name__ == "__main__":
    asyncio.run(test_connection())

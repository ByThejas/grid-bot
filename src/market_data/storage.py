"""
Market Data Storage — SQLite persistence layer.

WHY SQLite for Phase 3?
- Zero-config: file-based, no server to run.
- Plenty fast for our write rates (~20 inserts/sec total).
- Built-in CLI ('sqlite3 data/marketdata.db') lets you inspect data while building.
- WAL mode supports concurrent reads while the ingestor writes.
- We will swap this for DynamoDB in the AWS deployment phase.
  Because all DB calls go through this module, that swap is a one-file change.

WHY async (aiosqlite) instead of plain sqlite3?
- Our entire bot is async. A blocking sqlite3.execute() inside an async loop
  would freeze every other coroutine (WebSocket consumers, signal engines).
- aiosqlite runs queries in a background thread and gives us awaitable APIs.

SCHEMA DESIGN NOTES:
- orderbook_snapshots: one row per snapshot. We store top-N bids/asks as
  JSON strings rather than separate tables. Reasons:
    1. Atomic insert — one row, one transaction.
    2. Easier to read back as a single OrderBook object.
    3. SQLite's JSON1 extension can still query inside if we ever need to.
  Tradeoff: harder to do SQL aggregations across price levels. We don't need
  that for Phase 3, and Phase 4 signal logic will read snapshots into memory anyway.

- trades: one row per executed trade. Indexed on (symbol, timestamp) because
  every signal that consumes trades will query by that pair.
"""

import json
from typing import Optional

import aiosqlite

from src.utils.logger import get_logger

logger = get_logger("storage")


# Schema as a single string so we can run it on init() in one execute_script call.
# IF NOT EXISTS makes init() idempotent — safe to call on every startup.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    timestamp    INTEGER NOT NULL,        -- ms since epoch
    best_bid     REAL    NOT NULL,
    best_ask     REAL    NOT NULL,
    mid_price    REAL    NOT NULL,
    spread       REAL    NOT NULL,
    bids_json    TEXT    NOT NULL,        -- JSON array of [price, qty] pairs
    asks_json    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ob_symbol_ts
    ON orderbook_snapshots (symbol, timestamp);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    trade_id     INTEGER NOT NULL,        -- exchange's trade ID for dedup
    price        REAL    NOT NULL,
    quantity     REAL    NOT NULL,
    is_buyer_maker INTEGER NOT NULL,      -- 1 if buyer was maker (sell aggressor)
    timestamp    INTEGER NOT NULL,        -- ms since epoch
    UNIQUE (symbol, trade_id)             -- prevents duplicate inserts on reconnect
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts
    ON trades (symbol, timestamp);
"""


class MarketDataStore:
    """
    Async SQLite writer for order book snapshots and trades.

    USAGE:
        store = MarketDataStore("data/marketdata.db")
        await store.connect()
        await store.save_orderbook_snapshot(symbol, ts, bids, asks)
        await store.save_trade(...)
        await store.close()
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """
        Open the database and ensure the schema exists.

        WHY WAL mode?
        - Default rollback journal locks the DB during writes.
        - WAL (Write-Ahead Log) lets readers read while a writer writes.
        - We will want to inspect data with the sqlite3 CLI while the
          ingestor is running. Without WAL, that read would block.
        """
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")  # faster, still durable
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("storage opened", path=self._db_path)

    async def close(self) -> None:
        """Flush and close the connection."""
        if self._db:
            await self._db.commit()
            await self._db.close()
            self._db = None
            logger.info("storage closed")

    async def save_orderbook_snapshot(
        self,
        symbol: str,
        timestamp: int,
        bids: list,         # [[price, qty], ...] top N, highest price first
        asks: list,         # [[price, qty], ...] top N, lowest price first
    ) -> None:
        """
        Persist one order book snapshot.

        We compute best_bid/best_ask/mid/spread here so downstream queries
        do not need to parse the JSON columns just to get top-of-book.
        """
        if not bids or not asks:
            # Defensive: never insert a degenerate snapshot.
            # If this happens, the upstream stream is broken — log loudly.
            logger.warning("refusing to save empty book",
                           symbol=symbol, has_bids=bool(bids), has_asks=bool(asks))
            return

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        await self._db.execute(
            """
            INSERT INTO orderbook_snapshots
                (symbol, timestamp, best_bid, best_ask, mid_price, spread, bids_json, asks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                timestamp,
                best_bid,
                best_ask,
                mid,
                spread,
                json.dumps(bids),
                json.dumps(asks),
            ),
        )
        await self._db.commit()

    async def save_trade(
        self,
        symbol: str,
        trade_id: int,
        price: float,
        quantity: float,
        is_buyer_maker: bool,
        timestamp: int,
    ) -> None:
        """
        Persist one executed trade.

        WHY 'INSERT OR IGNORE'?
        On WebSocket reconnect, the exchange may resend recent trades.
        The UNIQUE (symbol, trade_id) constraint plus OR IGNORE means
        duplicates are silently dropped. This is the cheapest possible
        idempotency guarantee.
        """
        await self._db.execute(
            """
            INSERT OR IGNORE INTO trades
                (symbol, trade_id, price, quantity, is_buyer_maker, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (symbol, trade_id, price, quantity, 1 if is_buyer_maker else 0, timestamp),
        )
        await self._db.commit()

    async def count_snapshots(self, symbol: Optional[str] = None) -> int:
        """Diagnostic helper used by tests."""
        if symbol:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM orderbook_snapshots WHERE symbol = ?", (symbol,)
            )
        else:
            cursor = await self._db.execute("SELECT COUNT(*) FROM orderbook_snapshots")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0

    async def count_trades(self, symbol: Optional[str] = None) -> int:
        """Diagnostic helper used by tests."""
        if symbol:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol = ?", (symbol,)
            )
        else:
            cursor = await self._db.execute("SELECT COUNT(*) FROM trades")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0

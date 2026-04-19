"""
User Data Stream — Binance WS API with Ed25519 session.logon.

WHY this flavour (and not REST listen keys):
Binance retired POST/PUT/DELETE `/api/v*/userDataStream` on testnet.
CloudFront now returns 410 Gone for every REST listen-key path. The HMAC
WS API fallback (`userDataStream.start` with an apiKey) is also gone —
Binance's response: "HMAC-SHA-256 API key is not supported." The only
surviving path is:

    1. Open wss://ws-api.<host>/ws-api/v3
    2. Call `session.logon` with an Ed25519-signed request
    3. Call `userDataStream.subscribe` (no params) on the same connection
    4. Consume push events inline — they arrive on the same socket

The Ed25519 key is SEPARATE from the HMAC key that signs REST calls.
Both live on the same Binance account; the HMAC key continues to power
the BinanceExchange REST adapter, and this stream uses Ed25519 purely
for session.logon.

EVENTS WE CARE ABOUT
Binance pushes executionReport (among others). The fields the grid
engine needs: X (status), i (orderId), s (symbol), S (side), p (price),
q (qty), z (executed qty).

WS API push events wrap the payload as `{"event": {...}}`. The engine's
on_fill expects the flat dict, so we unwrap before forwarding.

SESSION LIFETIME
Once session.logon succeeds, the session stays authorised until the
connection drops. There is no 60-minute listen-key TTL, so no periodic
keepalive is needed. The websockets library auto-responds to server
PING frames, which keeps the connection alive. If the socket does drop,
the outer reconnect loop logs in + subscribes again.
"""

import asyncio
import base64
import json
import pathlib
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from src.utils.logger import get_logger

logger = get_logger("user_data_stream")


# Each event is the raw Binance payload. Kept untyped on purpose so new
# fields on Binance's side don't force a migration in our code.
EventHandler = Callable[[dict], Awaitable[None]]


_WS_API_MAINNET = "wss://ws-api.binance.com:443/ws-api/v3"
_WS_API_TESTNET = "wss://ws-api.testnet.binance.vision:443/ws-api/v3"


def _ws_api_url(testnet: bool) -> str:
    return _WS_API_TESTNET if testnet else _WS_API_MAINNET


class UserDataStream:
    """
    Subscribes to the authenticated user data stream and forwards every
    event to a caller-supplied async handler.

    USAGE:
        stream = UserDataStream(
            testnet=True,
            api_key=os.getenv("BINANCE_WS_API_KEY"),
            private_key_path="secrets/binance_ed25519_private.pem",
            on_event=engine.on_fill,
        )
        await stream.run(stop_event)
    """

    # Timeout for a single request/response round-trip on the WS API.
    # session.logon and subscribe are tiny — 10s is generous.
    _WS_REQ_TIMEOUT_S = 10.0

    def __init__(
        self,
        *,
        testnet: bool,
        api_key: str,
        private_key_path: str,
        on_event: EventHandler,
        reconnect_delay_s: float = 1.0,
        max_reconnect_attempts: int = 10,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if not private_key_path:
            raise ValueError("private_key_path is required")
        self._testnet = testnet
        self._api_key = api_key
        self._private_key_path = private_key_path
        self._on_event = on_event
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_attempts = max_reconnect_attempts
        self._private_key: Optional[Ed25519PrivateKey] = None
        self._events_seen = 0

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """
        Main loop. Same reconnect-budget pattern as trade_stream /
        orderbook_stream: healthy sessions reset the attempt counter so
        long-running runs don't get killed by a brief blip.
        """
        # Load + validate the key up front so we fail fast on missing /
        # wrong-type key files rather than N reconnect cycles in.
        self._load_private_key()

        HEALTHY_SESSION_SECS = 30.0
        attempts = 0
        while not stop_event.is_set():
            session_start = time.time()
            try:
                await self._consume_until_disconnect(stop_event)
                break
            except asyncio.CancelledError:
                logger.info("user data stream cancelled")
                raise
            except Exception as e:
                session_duration = time.time() - session_start
                if session_duration >= HEALTHY_SESSION_SECS:
                    attempts = 0
                attempts += 1
                if attempts > self._max_reconnect_attempts:
                    logger.error("user data stream gave up",
                                 attempts=attempts, error=str(e))
                    raise
                logger.warning(
                    "user data stream disconnected, will retry",
                    attempt=attempts,
                    session_duration_s=round(session_duration, 1),
                    error=str(e),
                )
                await asyncio.sleep(self._reconnect_delay_s)

        logger.info("user data stream stopped", events_seen=self._events_seen)

    # ------------------------------------------------------------------
    # Ed25519 key + signing
    # ------------------------------------------------------------------

    def _load_private_key(self) -> None:
        path = pathlib.Path(self._private_key_path)
        if not path.exists():
            raise FileNotFoundError(f"Ed25519 private key not found: {path}")
        key = load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(
                f"Expected Ed25519 private key, got {type(key).__name__}. "
                f"Regenerate with scripts/generate_ed25519_keypair.py."
            )
        self._private_key = key

    def _sign(self, params: dict) -> str:
        """
        Binance's canonical format: alphabetically sorted `k=v` pairs
        joined with `&`, signed with Ed25519, base64 encoded.
        """
        query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sig = self._private_key.sign(query.encode())
        return base64.b64encode(sig).decode()

    # ------------------------------------------------------------------
    # WS request/response + push event dispatch
    # ------------------------------------------------------------------

    async def _request(self, ws, method: str, params: dict) -> dict:
        """
        Send a request with a unique id, wait for the matching response.
        Any push events that arrive interleaved are forwarded to the
        handler — we don't want to lose a fill because a subscribe reply
        was slightly delayed.
        """
        req_id = str(uuid.uuid4())
        payload: dict[str, Any] = {"id": req_id, "method": method, "params": params}
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._WS_REQ_TIMEOUT_S)
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("id") == req_id:
                status = data.get("status")
                if status is not None and status != 200:
                    raise RuntimeError(f"WS API {method} failed: {data}")
                return data
            # Push event arrived before our reply — forward it, keep waiting.
            await self._dispatch_push(data)

    async def _dispatch_push(self, msg: dict) -> None:
        """Forward one push event to the user-supplied handler."""
        if not isinstance(msg, dict):
            return
        # WS API push events arrive as {"event": {...}}. The engine expects
        # the flat payload with "e"/"s"/"i"/"X", so unwrap when present.
        payload = msg.get("event", msg)
        if not isinstance(payload, dict):
            return
        self._events_seen += 1
        try:
            await self._on_event(payload)
        except Exception as e:
            # A buggy handler must never kill the stream — log and move on.
            logger.error("user data event handler crashed",
                         event_type=payload.get("e"),
                         error=str(e))

    # ------------------------------------------------------------------
    # Connect, logon, subscribe, consume
    # ------------------------------------------------------------------

    async def _consume_until_disconnect(self, stop_event: asyncio.Event) -> None:
        url = _ws_api_url(self._testnet)
        async with websockets.connect(url) as ws:
            # 1. session.logon — Ed25519 signed.
            logon_params = {
                "apiKey": self._api_key,
                "timestamp": int(time.time() * 1000),
            }
            logon_params["signature"] = self._sign(logon_params)
            await self._request(ws, "session.logon", logon_params)
            logger.info("session.logon ok (Ed25519)")

            # 2. userDataStream.subscribe — no params, auth carries from
            # the authenticated session.
            await self._request(ws, "userDataStream.subscribe", {})
            logger.info("user data stream connected")

            # 3. Consume push events until stop_event.
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Idle tick — no event in the last second. Normal on
                    # a quiet account.
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                # Stray request replies (shouldn't happen post-setup) are
                # ignored so we don't mis-route them as events.
                if msg.get("id") is not None:
                    continue
                await self._dispatch_push(msg)

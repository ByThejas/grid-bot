"""
WS API probe — figure out what the Binance testnet WS API actually accepts.

WHY this exists:
We don't yet know whether `userDataStream.start` is:
  (a) the wrong method name,
  (b) disabled on this API key,
  (c) now requires a signed request with timestamp+signature, or
  (d) replaced entirely by `session.logon` + `userDataStream.subscribe`.

This script tries three progressively more authenticated variants and
prints every response verbatim, so we can read Binance's own diagnostic
message for each.

USAGE (venv activated):
    python -m scripts.probe_ws_api
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import time
import uuid

import websockets
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv


URL = "wss://ws-api.testnet.binance.vision:443/ws-api/v3"


def _sign_ed25519(private_key_path: str, params: dict) -> str:
    """Sign the canonical query string with the Ed25519 private key.
    Binance expects the signature as base64, not hex."""
    pem = pathlib.Path(private_key_path).read_bytes()
    priv = load_pem_private_key(pem, password=None)
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = priv.sign(query.encode())
    return base64.b64encode(sig).decode()


def _sign(secret: str, params: dict) -> str:
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


async def _send_and_read(ws, payload: dict) -> dict:
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        data = json.loads(raw)
        if data.get("id") == payload["id"]:
            return data


async def main() -> None:
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("BINANCE_API_KEY / BINANCE_API_SECRET missing")
        return

    print(f"Connecting to {URL}")
    async with websockets.connect(URL) as ws:

        # -------- Test 1: unauthenticated ping --------
        # If this fails, the URL itself is wrong.
        resp = await _send_and_read(ws, {"id": str(uuid.uuid4()), "method": "ping"})
        print("\n[1] ping (no auth)")
        print(json.dumps(resp, indent=2))

        # -------- Test 2: userDataStream.start, apiKey only --------
        # This is our current approach. 404 here means apiKey-only auth
        # is no longer sufficient for this method.
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": {"apiKey": api_key},
        })
        print("\n[2] userDataStream.start (apiKey only)")
        print(json.dumps(resp, indent=2))

        # -------- Test 3: userDataStream.start, SIGNED --------
        # If Binance quietly upgraded the security type to SIGNED, the
        # same method with timestamp+signature should work.
        params = {"apiKey": api_key, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(api_secret, params)
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": params,
        })
        print("\n[3] userDataStream.start (signed: apiKey + timestamp + signature)")
        print(json.dumps(resp, indent=2))

        # -------- Test 4: account.status (confirms WS API works at all) --------
        # A known-signed method on every account. If this succeeds but [3]
        # doesn't, the issue is specifically with userDataStream.start.
        params = {"apiKey": api_key, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(api_secret, params)
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "account.status",
            "params": params,
        })
        print("\n[4] account.status (signed — sanity check that auth works)")
        # Truncate — account.status can be huge.
        txt = json.dumps(resp, indent=2)
        print(txt if len(txt) < 2000 else txt[:2000] + "\n...[truncated]")

        # -------- Test 5: userDataStream.start, signature over just apiKey --------
        # Method accepts exactly 2 params. Most natural shape: apiKey + a
        # signature that signs the apiKey string.
        params = {"apiKey": api_key}
        params["signature"] = _sign(api_secret, params)  # signs "apiKey=<key>"
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": params,
        })
        print("\n[5] userDataStream.start (apiKey + signature over apiKey)")
        print(json.dumps(resp, indent=2))

        # -------- Test 6: userDataStream.start, timestamp + signature, no apiKey --------
        # Maybe apiKey is now expected at a different layer (session/header).
        params = {"timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(api_secret, params)
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": params,
        })
        print("\n[6] userDataStream.start (timestamp + signature, no apiKey)")
        print(json.dumps(resp, indent=2))

        # -------- Test 7: userDataStream.start, apiKey + recvWindow --------
        # Long-shot: maybe the 2 expected params are apiKey + recvWindow.
        params = {"apiKey": api_key, "recvWindow": 5000}
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": params,
        })
        print("\n[7] userDataStream.start (apiKey + recvWindow)")
        print(json.dumps(resp, indent=2))

        # -------- Test 8: userDataStream.start with JUST apiKey --------
        # Baseline: is even a 1-param call accepted? If "missing parameter"
        # → we know one param besides apiKey is required. If it works →
        # we've been overthinking this.
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.start",
            "params": {"apiKey": api_key},
        })
        print("\n[8] userDataStream.start (apiKey ONLY)")
        print(json.dumps(resp, indent=2))

        # -------- Test 9: session.logon (HMAC signed) --------
        # Modern Binance auth: session.logon establishes auth on the whole
        # WS connection. If Ed25519 is required, we'll see a specific
        # "unsupported signature scheme" error and know to pivot.
        params = {"apiKey": api_key, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(api_secret, params)
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "session.logon",
            "params": params,
        })
        print("\n[9] session.logon (HMAC signed)")
        print(json.dumps(resp, indent=2))

        # -------- Test 10: userDataStream.subscribe (post-logon method) --------
        # This call should work ONLY after session.logon. If test 9 failed,
        # this will also fail — but the ERROR CODE tells us whether the
        # method exists at all (method-not-found vs auth-required).
        resp = await _send_and_read(ws, {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.subscribe",
            "params": {},
        })
        print("\n[10] userDataStream.subscribe (no params)")
        print(json.dumps(resp, indent=2))

        # -------- Test 11: session.logon with Ed25519 key --------
        # THIS is the modern path. If this succeeds we know the whole
        # Ed25519 chain is wired correctly.
        ws_api_key = os.getenv("BINANCE_WS_API_KEY", "")
        ws_priv_path = os.getenv("BINANCE_WS_PRIVATE_KEY_PATH", "")
        if not ws_api_key or not ws_priv_path:
            print("\n[11] SKIPPED — set BINANCE_WS_API_KEY and BINANCE_WS_PRIVATE_KEY_PATH in .env")
        elif not pathlib.Path(ws_priv_path).exists():
            print(f"\n[11] SKIPPED — private key file not found at {ws_priv_path}")
        else:
            params = {"apiKey": ws_api_key, "timestamp": int(time.time() * 1000)}
            params["signature"] = _sign_ed25519(ws_priv_path, params)
            resp = await _send_and_read(ws, {
                "id": str(uuid.uuid4()),
                "method": "session.logon",
                "params": params,
            })
            print("\n[11] session.logon (Ed25519 signed)")
            print(json.dumps(resp, indent=2))

            # -------- Test 12: userDataStream.subscribe on logged-in session --------
            if resp.get("status") == 200:
                resp = await _send_and_read(ws, {
                    "id": str(uuid.uuid4()),
                    "method": "userDataStream.subscribe",
                    "params": {},
                })
                print("\n[12] userDataStream.subscribe (after Ed25519 logon)")
                print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

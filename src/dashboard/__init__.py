"""
dashboard — local FastAPI-style HTML status page (Phase 11B).

A second short-lived process the operator runs alongside the bot:

    python -m src.dashboard.server   # serves http://127.0.0.1:8080

The page shows everything STATUS.json + the log tail already contain, plus
a HALT button that creates the runtime/HALT sentinel — same path as
`touch runtime/HALT` and Telegram /halt. Three kill switches, one effect.

DESIGN
  * Standalone process. Runs separately from the bot. If the bot crashes,
    the dashboard keeps serving and shows "stale" status. If the dashboard
    crashes, the bot keeps trading.
  * Read-only on the bot. The only write is the HALT button creating the
    sentinel — same atomic op the kill-switch monitor already polls for.
  * Localhost-only (127.0.0.1) by default. No auth, no LAN exposure. Phone
    access goes through Telegram. LAN/cloud access is a 11C concern.
  * aiohttp.web (not FastAPI) so we add zero new dependencies — aiohttp
    came in with Phase 11A's TelegramAlerter.

Layout:
  data.py   — pure functions: read STATUS.json, tail logs, compose view.
              No I/O above the file-read layer. Easy to test.
  server.py — aiohttp.web app, three routes, HTML render, CLI entrypoint.
"""

"""
notifications — out-of-band alerts and remote control (Phase 11A).

The bot is designed to run unattended. STATUS.json + log files give an
operator everything they need IF they're at a terminal. This package adds
the missing piece: a phone notification when something interesting (fill,
halt, pause, crash) happens, and a Telegram-driven remote `/halt` so the
operator can stop the bot from anywhere.

Layout:
  * format.py    — pure stringification of events. No I/O.
  * alerter.py   — `Alerter` ABC + `NullAlerter` no-op + `make_alerter` factory.
  * telegram.py  — `TelegramAlerter` (sender + listener async tasks, httpx).

The trading hot path NEVER awaits Telegram. Sender drops messages into a
bounded queue; the sender task drains it. If Telegram is misconfigured,
broken, or offline, the bot trades exactly as it did before Phase 11A.
"""

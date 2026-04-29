"""
format.py — pure functions that turn alert events into Telegram message strings.

WHY this is a separate module:
The actual sending lives in `TelegramAlerter` (httpx, async, retry, queue).
This file holds the formatting decisions — what tag, which fields, what
punctuation — so they're directly unit-testable without any network or
async machinery. If the format is wrong, the test fails; the bot doesn't.

CONVENTIONS (all enforced by tests):
  * Tags are uppercase and square-bracketed, e.g. [FILL], [HALT].
  * The body is `title — k=v · k=v · ...` with em-dash and middle-dot
    separators. Plain text only — no Markdown / HTML.
  * USD numbers carry thousand separators and 2 decimals. PnL deltas use
    an explicit '+' or '-' sign so the operator can read direction at a
    glance even on a phone-notification preview.
  * Quantities use up to 6 decimals, trailing zeros stripped — works for
    both BTC's tiny qtys (0.0003) and ETH's larger ones (0.005) without a
    per-symbol precision table.
  * Each message aims for ≤ 200 characters; [CRASH] is the exception
    because the recent traceback genuinely needs the lines.
  * Plain text is robust: Telegram's Markdown parser fails the whole
    message on a single missed escape, and our dynamic content (prices,
    error messages) is exactly the kind of thing that triggers that.
    Unicode separators (— ·) are fine in plain text mode.
"""

import traceback
from typing import Iterable

# Truncation limit for [CRASH] tracebacks. The operator gets just enough
# context to recognize the failure mode on a phone; the full trace stays
# in the log file (which we redirect them to).
MAX_TRACEBACK_FRAMES = 5


# ---------------------------------------------------------------------------
# Number / time helpers
# ---------------------------------------------------------------------------


def format_usd(amount: float, *, signed: bool = False) -> str:
    """
    USD with thousand separators and 2 decimals.

    signed=True for deltas (PnL, realized) so positives get an explicit
    '+'. Without signed, negative still carries '-' so the absolute value
    is never ambiguous.
    """
    if signed:
        sign = "+" if amount >= 0 else "-"
        return f"{sign}${abs(amount):,.2f}"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def format_qty(qty: float) -> str:
    """Up to 6 decimals, trailing zeros and trailing dot stripped."""
    s = f"{qty:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def format_uptime(seconds: float) -> str:
    """Human-readable uptime: '0m', '5m', '2h14m', '24h00m', '32h05m'."""
    if seconds < 0:
        seconds = 0
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def short_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC, ETHUSDT -> ETH. Anything else returned unchanged."""
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


# ---------------------------------------------------------------------------
# Internal: line assembly
# ---------------------------------------------------------------------------


def _line(tag: str, title: str, fields: Iterable[tuple[str, str | None]]) -> str:
    """
    Assemble `[TAG] title — k=v · k=v · ...`.

    Fields whose value is None or "" are dropped, so callers can pass
    conditional fields (e.g. realized only on SELL) without per-call
    if/else dancing.
    """
    pairs = " · ".join(f"{k}={v}" for k, v in fields if v not in (None, ""))
    if pairs:
        return f"[{tag}] {title} — {pairs}"
    return f"[{tag}] {title}"


# ---------------------------------------------------------------------------
# Lifecycle: BOOT / STOP / CRASH
# ---------------------------------------------------------------------------


def format_boot(
    *,
    env_mode: str,
    symbols: Iterable[str],
    max_open_orders: int,
    daily_loss_limit_usd: float,
) -> str:
    return _line(
        "BOOT", env_mode,
        [
            ("env", env_mode),
            ("symbols", ",".join(symbols)),
            ("max_open", str(max_open_orders)),
            ("loss_floor", format_usd(daily_loss_limit_usd)),
        ],
    )


def format_shutdown(
    *,
    final_open_orders: int,
    session_realized_usd: float,
    uptime_seconds: float,
) -> str:
    return _line(
        "STOP", "clean shutdown",
        [
            ("final_open", str(final_open_orders)),
            ("session_realized", format_usd(session_realized_usd, signed=True)),
            ("uptime", format_uptime(uptime_seconds)),
        ],
    )


def format_crash(*, component: str, exc: BaseException) -> str:
    """
    Title line + trimmed traceback.

    The exception summary is one-lined (newlines stripped) so the head
    fits in the standard format. The trailing traceback adds context but
    is bounded to MAX_TRACEBACK_FRAMES; deeper history lives in the log.
    """
    exc_type = type(exc).__name__
    summary = f"{exc_type}: {exc}".replace("\n", " ").strip()
    head = _line(
        "CRASH", f"uncaught exception in {component}",
        [("error", summary)],
    )

    tb_lines = traceback.format_tb(exc.__traceback__)
    if not tb_lines:
        return head
    tail = "".join(tb_lines[-MAX_TRACEBACK_FRAMES:]).rstrip()
    return f"{head}\n\nrecent frames:\n{tail}"


# ---------------------------------------------------------------------------
# Trading events: FILL / RECENTER
# ---------------------------------------------------------------------------


def format_fill(
    *,
    symbol: str,
    side: str,
    price: float,
    qty: float,
    realized_delta_usd: float | None = None,
) -> str:
    """
    BUY  → 'X bought ... cost=$N'.
    SELL → 'X sold   ... realized=±$N'  (realized only if provided).

    realized_delta_usd is FIFO-matched PnL on this specific fill. Only
    meaningful on SELL (a BUY by definition has no realized delta yet).
    """
    side_upper = side.upper()
    sym = short_symbol(symbol)
    if side_upper == "SELL":
        realized = (
            format_usd(realized_delta_usd, signed=True)
            if realized_delta_usd is not None else None
        )
        return _line(
            "FILL", f"{sym} sold",
            [
                ("price", format_usd(price)),
                ("qty", format_qty(qty)),
                ("side", side_upper),
                ("realized", realized),
            ],
        )
    return _line(
        "FILL", f"{sym} bought",
        [
            ("price", format_usd(price)),
            ("qty", format_qty(qty)),
            ("side", side_upper),
            ("cost", format_usd(price * qty)),
        ],
    )


def format_recenter(
    *, symbol: str, old_mid: float, new_mid: float, drift_pct: float,
) -> str:
    return _line(
        "RECENTER", short_symbol(symbol),
        [
            ("old_mid", format_usd(old_mid)),
            ("new_mid", format_usd(new_mid)),
            ("drift_pct", f"{drift_pct:.2f}%"),
        ],
    )


# ---------------------------------------------------------------------------
# Pause / Resume (sentiment, liquidity)
# ---------------------------------------------------------------------------


def format_pause(*, source: str, symbol: str, **fields: str) -> str:
    """
    source : 'liquidity' | 'sentiment' | future signals.
    fields : caller-supplied k=v pairs (spread, score, threshold...).
             We don't hard-code per-source schema — let the signal owner
             decide what's worth surfacing on a phone alert.
    """
    sym = short_symbol(symbol)
    return _line(
        "PAUSE", f"{sym} {source}",
        [("symbol", symbol)] + list(fields.items()),
    )


def format_resume(*, source: str, symbol: str, **fields: str) -> str:
    sym = short_symbol(symbol)
    return _line(
        "RESUME", f"{sym} {source} normal",
        [("symbol", symbol)] + list(fields.items()),
    )


# ---------------------------------------------------------------------------
# Risk: HALT
# ---------------------------------------------------------------------------


def format_halt(
    *,
    reason: str,
    realized_24h_usd: float | None = None,
    daily_loss_limit_usd: float | None = None,
    open_orders: int | None = None,
) -> str:
    floor_str = (
        format_usd(-daily_loss_limit_usd, signed=True)
        if daily_loss_limit_usd is not None else None
    )
    realized_str = (
        format_usd(realized_24h_usd, signed=True)
        if realized_24h_usd is not None else None
    )
    return _line(
        "HALT", reason,
        [
            ("realized_24h", realized_str),
            ("floor", floor_str),
            ("open_orders", str(open_orders) if open_orders is not None else None),
        ],
    )


def format_halt_ack() -> str:
    """Reply emitted when /halt arrives via Telegram."""
    return _line(
        "HALT", "manual halt requested via Telegram",
        [("sentinel", "runtime/HALT")],
    )


# ---------------------------------------------------------------------------
# Daily summary + STATUS reply + HELP reply
# ---------------------------------------------------------------------------


def format_daily_summary(
    *,
    utc_day: str,
    fills: int,
    realized_usd: float,
    recenters: int,
    halts: int,
    uptime_seconds: float,
) -> str:
    return _line(
        "DAILY", f"{utc_day} UTC",
        [
            ("fills", str(fills)),
            ("realized", format_usd(realized_usd, signed=True)),
            ("recenters", str(recenters)),
            ("halts", str(halts)),
            ("uptime", format_uptime(uptime_seconds)),
        ],
    )


def format_status(
    *,
    env_mode: str,
    open_orders: int,
    placed_total: int,
    filled_total: int,
    realized_24h_usd: float,
    halted: bool,
    uptime_seconds: float,
) -> str:
    state = "halted" if halted else "running"
    return _line(
        "STATUS", state,
        [
            ("env", env_mode),
            ("open_orders", str(open_orders)),
            ("placed_total", str(placed_total)),
            ("filled_total", str(filled_total)),
            ("realized_24h", format_usd(realized_24h_usd, signed=True)),
            ("halted", "true" if halted else "false"),
            ("uptime", format_uptime(uptime_seconds)),
        ],
    )


def format_help() -> str:
    return "[HELP] commands — /status · /halt · /help"

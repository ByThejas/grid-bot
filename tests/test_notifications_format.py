"""
Tests for src.notifications.format — Phase 11A.

Focus:
  * Number/time helpers (format_usd, format_qty, format_uptime, short_symbol)
    behave correctly on the ranges we'll see in production (BTC qtys,
    negative PnL, sub-minute uptimes, unknown symbols).
  * Line assembly (_line) drops None/empty fields so callers can pass
    conditional values without per-call branching.
  * Each top-level formatter (BOOT, STOP, CRASH, FILL, HALT, PAUSE,
    RESUME, DAILY, STATUS, RECENTER, HELP) renders the expected tag and
    fields; non-CRASH messages stay under 200 characters with realistic
    inputs (the on-phone length budget).
  * format_crash truncates to MAX_TRACEBACK_FRAMES and one-lines the
    exception summary so the head still fits the standard format.
"""

from src.notifications.format import (
    MAX_TRACEBACK_FRAMES,
    _line,
    format_boot,
    format_crash,
    format_daily_summary,
    format_fill,
    format_halt,
    format_halt_ack,
    format_help,
    format_pause,
    format_qty,
    format_recenter,
    format_resume,
    format_shutdown,
    format_status,
    format_uptime,
    format_usd,
    short_symbol,
)


# ---------------------------------------------------------------------------
# format_usd
# ---------------------------------------------------------------------------


def test_usd_unsigned_positive():
    assert format_usd(76213.4) == "$76,213.40"


def test_usd_unsigned_negative_keeps_minus():
    assert format_usd(-21.4) == "-$21.40"


def test_usd_zero_unsigned():
    assert format_usd(0.0) == "$0.00"


def test_usd_signed_positive_gets_plus():
    assert format_usd(0.18, signed=True) == "+$0.18"


def test_usd_signed_negative_gets_minus():
    assert format_usd(-21.4, signed=True) == "-$21.40"


def test_usd_signed_zero_gets_plus():
    # +$0.00 is the right convention here — it indicates direction
    # explicitly; "0.00" without a sign would be ambiguous in a delta
    # column where every other row carries a sign.
    assert format_usd(0.0, signed=True) == "+$0.00"


def test_usd_thousand_separator():
    assert format_usd(1234567.89) == "$1,234,567.89"


# ---------------------------------------------------------------------------
# format_qty
# ---------------------------------------------------------------------------


def test_qty_strips_trailing_zeros():
    assert format_qty(0.000300) == "0.0003"


def test_qty_strips_trailing_dot_for_whole_numbers():
    assert format_qty(1.0) == "1"


def test_qty_zero():
    assert format_qty(0.0) == "0"


def test_qty_keeps_six_decimals_for_tiny():
    assert format_qty(0.000001) == "0.000001"


def test_qty_eth_size():
    assert format_qty(0.005) == "0.005"


# ---------------------------------------------------------------------------
# format_uptime
# ---------------------------------------------------------------------------


def test_uptime_under_one_minute_is_zero_minutes():
    assert format_uptime(45) == "0m"


def test_uptime_minutes_only_under_one_hour():
    assert format_uptime(5 * 60) == "5m"
    assert format_uptime(59 * 60 + 59) == "59m"


def test_uptime_hours_pads_minutes_to_two_digits():
    # 24h00m, not 24h0m — easier to scan in a column.
    assert format_uptime(24 * 3600) == "24h00m"
    assert format_uptime(2 * 3600 + 14 * 60) == "2h14m"
    assert format_uptime(32 * 3600 + 5 * 60) == "32h05m"


def test_uptime_negative_clamped_to_zero():
    # Clock drift could make end-time < start-time briefly. Don't crash.
    assert format_uptime(-5) == "0m"


# ---------------------------------------------------------------------------
# short_symbol
# ---------------------------------------------------------------------------


def test_short_symbol_strips_usdt():
    assert short_symbol("BTCUSDT") == "BTC"
    assert short_symbol("ETHUSDT") == "ETH"


def test_short_symbol_passes_through_unknown():
    assert short_symbol("BTCBUSD") == "BTCBUSD"
    assert short_symbol("DOGE") == "DOGE"


# ---------------------------------------------------------------------------
# _line internal
# ---------------------------------------------------------------------------


def test_line_drops_none_and_empty_values():
    out = _line("TAG", "title", [("a", "1"), ("b", None), ("c", ""), ("d", "4")])
    assert out == "[TAG] title — a=1 · d=4"


def test_line_with_no_surviving_fields_omits_dash():
    out = _line("TAG", "bare", [("x", None), ("y", "")])
    assert out == "[TAG] bare"


# ---------------------------------------------------------------------------
# Lifecycle: BOOT / STOP
# ---------------------------------------------------------------------------


def test_boot_message_shape():
    msg = format_boot(
        env_mode="mainnet",
        symbols=["BTCUSDT", "ETHUSDT"],
        max_open_orders=10,
        daily_loss_limit_usd=20.0,
    )
    assert msg.startswith("[BOOT] mainnet —")
    assert "env=mainnet" in msg
    assert "symbols=BTCUSDT,ETHUSDT" in msg
    assert "max_open=10" in msg
    assert "loss_floor=$20.00" in msg
    assert len(msg) <= 200


def test_shutdown_message_shape():
    msg = format_shutdown(
        final_open_orders=8, session_realized_usd=3.18, uptime_seconds=8 * 3600 + 22 * 60,
    )
    assert msg.startswith("[STOP] clean shutdown —")
    assert "final_open=8" in msg
    assert "session_realized=+$3.18" in msg
    assert "uptime=8h22m" in msg
    assert len(msg) <= 200


# ---------------------------------------------------------------------------
# format_crash
# ---------------------------------------------------------------------------


def test_crash_head_has_tag_and_summary():
    try:
        raise RuntimeError("order book empty for ETHUSDT")
    except RuntimeError as e:
        msg = format_crash(component="grid_engine", exc=e)
    head_line = msg.split("\n", 1)[0]
    assert head_line.startswith("[CRASH] uncaught exception in grid_engine —")
    assert "RuntimeError: order book empty for ETHUSDT" in head_line


def test_crash_truncates_traceback_to_max_frames():
    # Build > MAX_TRACEBACK_FRAMES *distinct* frames. Recursive frames
    # would be collapsed by Python's traceback dedup into one summary
    # entry, which would defeat the test's intent.
    def f1():
        raise RuntimeError("deep error")

    def f2(): f1()      # noqa: E704
    def f3(): f2()      # noqa: E704
    def f4(): f3()      # noqa: E704
    def f5(): f4()      # noqa: E704
    def f6(): f5()      # noqa: E704
    def f7(): f6()      # noqa: E704

    try:
        f7()
    except RuntimeError as e:
        msg = format_crash(component="test", exc=e)
    file_lines = [ln for ln in msg.splitlines() if ln.lstrip().startswith("File ")]
    assert len(file_lines) == MAX_TRACEBACK_FRAMES
    # Tail semantics: the *deepest* frames are kept, outer frames trimmed.
    # f1 is innermost, so it must appear; f7 is one of the trimmed outer
    # frames, so it must not.
    assert ", in f1" in msg
    assert ", in f7" not in msg


def test_crash_one_lines_multiline_exception_message():
    try:
        raise ValueError("first line\nsecond line\nthird")
    except ValueError as e:
        msg = format_crash(component="x", exc=e)
    head_line = msg.split("\n", 1)[0]
    # Newlines must be stripped so the head fits one line.
    assert "\n" not in head_line
    assert "first line second line third" in head_line


def test_crash_without_traceback_returns_only_head():
    # Build an exception that was never raised — __traceback__ is None.
    e = RuntimeError("synthetic, not raised")
    msg = format_crash(component="x", exc=e)
    assert "\n" not in msg
    assert msg.startswith("[CRASH]")


# ---------------------------------------------------------------------------
# format_fill
# ---------------------------------------------------------------------------


def test_fill_buy_shows_cost_not_realized():
    msg = format_fill(symbol="ETHUSDT", side="BUY", price=2269.73, qty=0.005)
    assert msg.startswith("[FILL] ETH bought —")
    assert "side=BUY" in msg
    assert "price=$2,269.73" in msg
    assert "qty=0.005" in msg
    assert "cost=$11.35" in msg
    assert "realized=" not in msg
    assert len(msg) <= 200


def test_fill_sell_with_realized_shows_signed_pnl():
    msg = format_fill(
        symbol="BTCUSDT", side="SELL", price=76213.4, qty=0.0003,
        realized_delta_usd=0.18,
    )
    assert msg.startswith("[FILL] BTC sold —")
    assert "side=SELL" in msg
    assert "price=$76,213.40" in msg
    assert "qty=0.0003" in msg
    assert "realized=+$0.18" in msg
    assert "cost=" not in msg
    assert len(msg) <= 200


def test_fill_sell_without_realized_omits_field():
    msg = format_fill(
        symbol="BTCUSDT", side="SELL", price=76000.0, qty=0.0003,
        realized_delta_usd=None,
    )
    assert "realized=" not in msg


def test_fill_sell_negative_realized_keeps_sign():
    msg = format_fill(
        symbol="BTCUSDT", side="SELL", price=75000.0, qty=0.0003,
        realized_delta_usd=-1.45,
    )
    assert "realized=-$1.45" in msg


def test_fill_normalizes_lowercase_side():
    msg = format_fill(symbol="ETHUSDT", side="buy", price=2000.0, qty=0.005)
    assert "side=BUY" in msg
    assert msg.startswith("[FILL] ETH bought —")


# ---------------------------------------------------------------------------
# format_recenter
# ---------------------------------------------------------------------------


def test_recenter_message_shape():
    msg = format_recenter(symbol="BTCUSDT", old_mid=70000.0, new_mid=71500.0, drift_pct=2.14)
    assert msg.startswith("[RECENTER] BTC —")
    assert "old_mid=$70,000.00" in msg
    assert "new_mid=$71,500.00" in msg
    assert "drift_pct=2.14%" in msg
    assert len(msg) <= 200


# ---------------------------------------------------------------------------
# format_pause / format_resume
# ---------------------------------------------------------------------------


def test_pause_liquidity_keeps_full_symbol_in_fields():
    msg = format_pause(source="liquidity", symbol="ETHUSDT", spread="62bps", threshold="50bps")
    assert msg.startswith("[PAUSE] ETH liquidity —")
    assert "symbol=ETHUSDT" in msg
    assert "spread=62bps" in msg
    assert "threshold=50bps" in msg
    assert len(msg) <= 200


def test_pause_sentiment_forwards_arbitrary_fields():
    msg = format_pause(source="sentiment", symbol="BTCUSDT", score="-0.85", reason="cpi miss")
    assert msg.startswith("[PAUSE] BTC sentiment —")
    assert "score=-0.85" in msg
    assert "reason=cpi miss" in msg


def test_resume_message_shape():
    msg = format_resume(source="liquidity", symbol="ETHUSDT", spread="12bps")
    assert msg.startswith("[RESUME] ETH liquidity normal —")
    assert "symbol=ETHUSDT" in msg
    assert "spread=12bps" in msg
    assert len(msg) <= 200


# ---------------------------------------------------------------------------
# format_halt + format_halt_ack
# ---------------------------------------------------------------------------


def test_halt_full_fields():
    msg = format_halt(
        reason="daily loss floor breached",
        realized_24h_usd=-21.40,
        daily_loss_limit_usd=20.0,
        open_orders=8,
    )
    assert msg.startswith("[HALT] daily loss floor breached —")
    assert "realized_24h=-$21.40" in msg
    # Floor is rendered as the negative of the (positive) limit, signed.
    assert "floor=-$20.00" in msg
    assert "open_orders=8" in msg
    assert len(msg) <= 200


def test_halt_partial_fields_omit_missing():
    msg = format_halt(reason="kill switch file present")
    # Only the reason — no field section.
    assert msg == "[HALT] kill switch file present"


def test_halt_ack_message_shape():
    msg = format_halt_ack()
    assert msg.startswith("[HALT] manual halt requested via Telegram —")
    assert "sentinel=runtime/HALT" in msg
    assert len(msg) <= 200


# ---------------------------------------------------------------------------
# format_daily_summary + format_status + format_help
# ---------------------------------------------------------------------------


def test_daily_summary_shape():
    msg = format_daily_summary(
        utc_day="2026-04-28",
        fills=42,
        realized_usd=3.18,
        recenters=2,
        halts=0,
        uptime_seconds=24 * 3600,
    )
    assert msg.startswith("[DAILY] 2026-04-28 UTC —")
    assert "fills=42" in msg
    assert "realized=+$3.18" in msg
    assert "recenters=2" in msg
    assert "halts=0" in msg
    assert "uptime=24h00m" in msg
    assert len(msg) <= 200


def test_status_running_lowercase_halted_false():
    msg = format_status(
        env_mode="mainnet",
        open_orders=7,
        placed_total=12,
        filled_total=4,
        realized_24h_usd=0.46,
        halted=False,
        uptime_seconds=2 * 3600 + 14 * 60,
    )
    assert msg.startswith("[STATUS] running —")
    assert "env=mainnet" in msg
    assert "open_orders=7" in msg
    assert "placed_total=12" in msg
    assert "filled_total=4" in msg
    assert "realized_24h=+$0.46" in msg
    assert "halted=false" in msg
    assert "uptime=2h14m" in msg
    assert len(msg) <= 200


def test_status_halted_state_word_changes():
    msg = format_status(
        env_mode="mainnet", open_orders=0, placed_total=0, filled_total=0,
        realized_24h_usd=0.0, halted=True, uptime_seconds=10,
    )
    assert msg.startswith("[STATUS] halted —")
    assert "halted=true" in msg


def test_help_is_static_string():
    assert format_help() == "[HELP] commands — /status · /halt · /help"

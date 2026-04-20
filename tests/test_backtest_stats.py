"""
Tests for the backtest stats collector.

Focus:
  - FIFO PnL matching (profit on grid round-trip; loss on adverse move)
  - Open inventory accounting (net long/short tracking)
  - Max drawdown = worst drop from peak of realized-PnL curve
  - Price min/max/first/last tracking
  - Report renders without crashing on empty / no-tick symbols
"""

import sys
import traceback

from src.backtest.stats import StatsCollector


# ---------------------------------------------------------------------------
# FIFO PnL
# ---------------------------------------------------------------------------


def test_single_buy_no_pnl_yet():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)
    st = c._stats["BTCUSDT"]
    assert st.buys_filled == 1
    assert st.realized_pnl_usd == 0.0
    assert abs(st.open_inventory_base - 0.001) < 1e-9


def test_buy_then_sell_realizes_profit():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)
    c.record_fill("BTCUSDT", "SELL", 75_150, 0.001)
    st = c._stats["BTCUSDT"]
    # +150 * 0.001 = +0.15
    assert abs(st.realized_pnl_usd - 0.15) < 1e-9
    assert abs(st.open_inventory_base) < 1e-9


def test_buy_then_sell_realizes_loss():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)
    c.record_fill("BTCUSDT", "SELL", 74_800, 0.001)
    # -200 * 0.001 = -0.20
    assert abs(c._stats["BTCUSDT"].realized_pnl_usd - (-0.20)) < 1e-9


def test_partial_matching_leaves_residual():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.002)
    c.record_fill("BTCUSDT", "SELL", 75_100, 0.001)
    st = c._stats["BTCUSDT"]
    # Realized on matched 0.001 only: +100 * 0.001 = +0.10
    assert abs(st.realized_pnl_usd - 0.10) < 1e-9
    # 0.001 BTC still net long.
    assert abs(st.open_inventory_base - 0.001) < 1e-9


def test_fifo_matches_oldest_lot():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)   # lot A
    c.record_fill("BTCUSDT", "BUY", 75_200, 0.001)   # lot B
    c.record_fill("BTCUSDT", "SELL", 75_300, 0.001)
    # FIFO → matched against lot A: +300 * 0.001 = +0.30
    assert abs(c._stats["BTCUSDT"].realized_pnl_usd - 0.30) < 1e-9
    assert abs(c._stats["BTCUSDT"].open_inventory_base - 0.001) < 1e-9


def test_sell_then_buy_closes_short_with_profit():
    # Short sell at 75200, buy back at 75000 → profit 200 per unit.
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "SELL", 75_200, 0.001)
    c.record_fill("BTCUSDT", "BUY",  75_000, 0.001)
    assert abs(c._stats["BTCUSDT"].realized_pnl_usd - 0.20) < 1e-9


def test_per_symbol_independence():
    c = StatsCollector(["BTCUSDT", "ETHUSDT"])
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)
    c.record_fill("ETHUSDT", "BUY", 2_500, 0.01)
    # BTC sell should NOT match against ETH buy lot.
    c.record_fill("BTCUSDT", "SELL", 75_100, 0.001)
    assert abs(c._stats["BTCUSDT"].realized_pnl_usd - 0.10) < 1e-9
    assert c._stats["ETHUSDT"].realized_pnl_usd == 0.0
    assert abs(c._stats["ETHUSDT"].open_inventory_base - 0.01) < 1e-9


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_captures_worst_drop():
    c = StatsCollector(["BTCUSDT"])
    # Profit sequence: +1.0, +0.5, -2.0, +0.3
    # Cum curve: 1.0, 1.5, -0.5, -0.2. Peak 1.5, trough -0.5 → dd = -2.0
    c.record_fill("BTCUSDT", "BUY",  100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 101, 1.0)  # +1.0
    c.record_fill("BTCUSDT", "BUY",  100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 100.5, 1.0)  # +0.5 (cum 1.5, new peak)
    c.record_fill("BTCUSDT", "BUY",  100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 98, 1.0)   # -2.0 (cum -0.5, dd = -2.0)
    c.record_fill("BTCUSDT", "BUY",  100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 100.3, 1.0)  # +0.3 (cum -0.2)
    assert abs(c._max_drawdown - (-2.0)) < 1e-9


def test_no_drawdown_when_monotone_up():
    c = StatsCollector(["BTCUSDT"])
    c.record_fill("BTCUSDT", "BUY", 100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 101, 1.0)
    c.record_fill("BTCUSDT", "BUY", 100, 1.0)
    c.record_fill("BTCUSDT", "SELL", 102, 1.0)
    assert c._max_drawdown == 0.0


# ---------------------------------------------------------------------------
# Tick tracking
# ---------------------------------------------------------------------------


def test_price_tracking_first_last_min_max():
    c = StatsCollector(["BTCUSDT"])
    c.record_tick("BTCUSDT", 75_000)
    c.record_tick("BTCUSDT", 74_500)
    c.record_tick("BTCUSDT", 75_300)
    c.record_tick("BTCUSDT", 75_100)
    st = c._stats["BTCUSDT"]
    assert st.first_price == 75_000
    assert st.last_price == 75_100
    assert st.min_price == 74_500
    assert st.max_price == 75_300


def test_tick_for_unknown_symbol_silently_ignored():
    c = StatsCollector(["BTCUSDT"])
    c.record_tick("DOGEUSDT", 0.10)   # not configured
    # Nothing should blow up; BTC stats remain empty.
    assert c._stats["BTCUSDT"].first_price is None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_report_renders_without_crash_empty_run():
    c = StatsCollector(["BTCUSDT"])
    r = c.build_report(duration_virtual_s=0, duration_wall_s=0, total_ticks=0)
    text = r.render()
    # Should mention "no ticks" and not crash on inf min/max.
    assert "no ticks received" in text


def test_report_renders_with_fills_and_ticks():
    c = StatsCollector(["BTCUSDT"])
    c.record_tick("BTCUSDT", 75_000)
    c.record_fill("BTCUSDT", "BUY", 75_000, 0.001)
    c.record_fill("BTCUSDT", "SELL", 75_100, 0.001)
    c.record_tick("BTCUSDT", 75_100)
    c.record_engine_counters("BTCUSDT", {
        "placed_total": 20, "filled_total": 2, "recentered_total": 0,
        "blocked_by_risk": 0, "paused_by_liquidity": 0, "paused_by_sentiment": 0,
    })
    r = c.build_report(duration_virtual_s=3600, duration_wall_s=1.2, total_ticks=2)
    text = r.render()
    assert "TOTAL REALIZED PnL" in text
    assert "BTCUSDT" in text
    assert "fills          : 1 BUY / 1 SELL" in text


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


_TESTS = [
    test_single_buy_no_pnl_yet,
    test_buy_then_sell_realizes_profit,
    test_buy_then_sell_realizes_loss,
    test_partial_matching_leaves_residual,
    test_fifo_matches_oldest_lot,
    test_sell_then_buy_closes_short_with_profit,
    test_per_symbol_independence,
    test_max_drawdown_captures_worst_drop,
    test_no_drawdown_when_monotone_up,
    test_price_tracking_first_last_min_max,
    test_tick_for_unknown_symbol_silently_ignored,
    test_report_renders_without_crash_empty_run,
    test_report_renders_with_fills_and_ticks,
]


def _main() -> None:
    passed = failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _main()

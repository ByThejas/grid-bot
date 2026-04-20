"""
Tests for LiquiditySignal.

WHY these tests matter:
The engine consults this signal to decide BOTH whether to place orders
at all (should_pause) AND how wide to space them. Get the spread
calculation wrong and we either run a too-tight grid that bleeds to
fees, or pause healthy markets needlessly. So we lock down the math.

Tests use the pure `compute_liquidity_state` function with synthetic
OrderBook objects — no exchange, no async required. Run as
`python -m tests.test_liquidity` AND under pytest.
"""

import sys
import traceback

from src.exchange.base import OrderBook
from src.signals.liquidity import (
    LiquidityConfig,
    LiquidityState,
    _depth_imbalance,
    _spread_bps,
    compute_liquidity_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _book(bids: list, asks: list, symbol: str = "BTCUSDT") -> OrderBook:
    return OrderBook(symbol=symbol, bids=bids, asks=asks, timestamp=0)


def _cfg(
    *,
    pause_spread_bps: float = 50.0,
    min_spacing_to_spread_ratio: float = 4.0,
    depth_levels: int = 20,
) -> LiquidityConfig:
    return LiquidityConfig(
        pause_spread_bps=pause_spread_bps,
        min_spacing_to_spread_ratio=min_spacing_to_spread_ratio,
        depth_levels=depth_levels,
    )


# ---------------------------------------------------------------------------
# spread_bps
# ---------------------------------------------------------------------------


def test_spread_bps_basic():
    # bid 100, ask 100.10 → spread 0.10 / mid 100.05 = ~9.995 bps
    book = _book([[100.0, 1]], [[100.10, 1]])
    bps = _spread_bps(book)
    assert abs(bps - 9.995) < 0.01


def test_spread_bps_empty_book_returns_zero():
    assert _spread_bps(_book([], [])) == 0.0
    assert _spread_bps(_book([[100, 1]], [])) == 0.0
    assert _spread_bps(_book([], [[100, 1]])) == 0.0


def test_spread_bps_zero_mid_returns_zero():
    book = _book([[0, 1]], [[0, 1]])
    assert _spread_bps(book) == 0.0


# ---------------------------------------------------------------------------
# depth_imbalance
# ---------------------------------------------------------------------------


def test_depth_imbalance_balanced_book():
    book = _book([[100, 1]], [[101, 1]])
    assert _depth_imbalance(book, depth_levels=20) == 0.0


def test_depth_imbalance_bid_heavy():
    book = _book([[100, 3]], [[101, 1]])
    # (3 - 1) / (3 + 1) = 0.5
    assert _depth_imbalance(book, depth_levels=20) == 0.5


def test_depth_imbalance_ask_heavy():
    book = _book([[100, 1]], [[101, 3]])
    assert _depth_imbalance(book, depth_levels=20) == -0.5


def test_depth_imbalance_respects_depth_levels():
    # 5 bid levels × 1 = 5; 5 ask levels × 1 = 5 → balanced if all considered.
    # But with depth_levels=2, only top 2 of each side count → still balanced.
    bids = [[100 - i, 1] for i in range(5)]
    asks = [[101 + i, 1] for i in range(5)]
    assert _depth_imbalance(_book(bids, asks), depth_levels=2) == 0.0


def test_depth_imbalance_truncates_to_depth_levels():
    # 10 bids × 1 = 10 total; 1 ask × 100 = 100. Taking depth=1:
    # top bid qty = 1, top ask qty = 100 → (1-100)/101 ≈ -0.98
    bids = [[100 - i, 1] for i in range(10)]
    asks = [[101, 100]]
    val = _depth_imbalance(_book(bids, asks), depth_levels=1)
    assert abs(val - (-99/101)) < 1e-9


def test_depth_imbalance_empty_book_returns_zero():
    assert _depth_imbalance(_book([], []), depth_levels=20) == 0.0


# ---------------------------------------------------------------------------
# compute_liquidity_state — pause logic
# ---------------------------------------------------------------------------


def test_pauses_when_spread_exceeds_threshold():
    # bid 100, ask 101 → spread 1.00 / mid 100.5 ≈ 99.5 bps, well above 50.
    book = _book([[100, 1]], [[101, 1]])
    state = compute_liquidity_state(
        book=book,
        config=_cfg(pause_spread_bps=50),
        base_spacing_pct=0.3, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert state.should_pause is True
    assert "spread" in state.pause_reason


def test_does_not_pause_just_below_threshold():
    # spread chosen to be ~40 bps: bid 100, ask 100.40 → 39.92 bps.
    book = _book([[100, 1]], [[100.40, 1]])
    state = compute_liquidity_state(
        book=book,
        config=_cfg(pause_spread_bps=50),
        base_spacing_pct=0.3, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert state.should_pause is False


# ---------------------------------------------------------------------------
# compute_liquidity_state — recommended spacing
# ---------------------------------------------------------------------------


def test_recommended_spacing_uses_base_when_market_is_tight():
    # Tight 1 bp spread (= 0.01%). Spread floor = 4 × 0.01 = 0.04%, which is
    # well below base 0.3%. So we should get base back.
    book = _book([[100, 1]], [[100.01, 1]])
    state = compute_liquidity_state(
        book=book, config=_cfg(),
        base_spacing_pct=0.3, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert state.should_pause is False
    assert state.recommended_spacing_pct == 0.3


def test_recommended_spacing_widens_when_spread_is_loose():
    # 20 bp spread → spread_pct = 0.2%; floor = 4 × 0.2 = 0.8%; base = 0.3%.
    # Recommended = max(0.3, 0.8) = 0.8.
    book = _book([[100, 1]], [[100.20, 1]])
    state = compute_liquidity_state(
        book=book, config=_cfg(pause_spread_bps=100),
        base_spacing_pct=0.3, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert abs(state.recommended_spacing_pct - 0.8) < 0.01


def test_recommended_spacing_clamps_to_max():
    # Engineered to ask for spacing > max. 30 bp spread, ratio 4 → 1.2%.
    # max = 1.0%; recommended must clamp to 1.0.
    book = _book([[100, 1]], [[100.30, 1]])
    state = compute_liquidity_state(
        book=book, config=_cfg(pause_spread_bps=100),
        base_spacing_pct=0.3, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert state.recommended_spacing_pct == 1.0


def test_recommended_spacing_clamps_to_min():
    # base below min should clamp UP to min, even on a healthy market.
    book = _book([[100, 1]], [[100.01, 1]])
    state = compute_liquidity_state(
        book=book, config=_cfg(),
        base_spacing_pct=0.05, min_spacing_pct=0.15, max_spacing_pct=1.0,
    )
    assert state.recommended_spacing_pct == 0.15


# ---------------------------------------------------------------------------
# Standalone runner + pytest discovery
# ---------------------------------------------------------------------------


_TESTS = [
    test_spread_bps_basic,
    test_spread_bps_empty_book_returns_zero,
    test_spread_bps_zero_mid_returns_zero,
    test_depth_imbalance_balanced_book,
    test_depth_imbalance_bid_heavy,
    test_depth_imbalance_ask_heavy,
    test_depth_imbalance_respects_depth_levels,
    test_depth_imbalance_truncates_to_depth_levels,
    test_depth_imbalance_empty_book_returns_zero,
    test_pauses_when_spread_exceeds_threshold,
    test_does_not_pause_just_below_threshold,
    test_recommended_spacing_uses_base_when_market_is_tight,
    test_recommended_spacing_widens_when_spread_is_loose,
    test_recommended_spacing_clamps_to_max,
    test_recommended_spacing_clamps_to_min,
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

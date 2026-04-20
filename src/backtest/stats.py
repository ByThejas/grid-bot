"""
Backtest stats collector and report.

WHAT THIS MEASURES (and what it doesn't):
v1 tracks REALIZED PnL only — positions that were opened and closed
within the simulation. Unrealized PnL on inventory still open at the
end of the run is NOT marked to market; we report it as leftover
inventory in base currency and you eyeball the conversion.

The realized-PnL drawdown is the worst-ever drop from the running peak
of cumulative realized PnL. This is the right metric for a grid bot:
"how much of your realized profit could the strategy have given back in
the worst sequence of fills?" It's not a full strategy drawdown (that
would include unrealized MTM losses on open inventory), but it's the
cheap, signal-rich half.

WHY we don't reuse RiskManager's ledger
RiskManager keeps a ROLLING 24-hour window for live operation — trades
older than 24h fall off. The backtest often covers more than 24h of
virtual time, so we'd lose fills. Stats maintains its own per-symbol
inventory + PnL accumulator without time decay.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("backtest_stats")


@dataclass
class _Lot:
    """One open leg of a BUY (long) or SELL (short) yet to be matched."""
    side: str           # "BUY" or "SELL"
    price: float
    qty: float


@dataclass
class SymbolStats:
    """Per-symbol summary of everything that happened in one backtest run."""
    symbol: str
    # Fill counts
    buys_filled: int = 0
    sells_filled: int = 0
    buy_volume_base: float = 0.0
    sell_volume_base: float = 0.0
    buy_volume_quote: float = 0.0    # price * qty, USDT for our pairs
    sell_volume_quote: float = 0.0
    # PnL
    realized_pnl_usd: float = 0.0
    open_inventory_base: float = 0.0  # positive = net long, negative = net short
    # Price path
    first_price: Optional[float] = None
    last_price: Optional[float] = None
    min_price: float = float("inf")
    max_price: float = float("-inf")
    # Engine counters (snapshot at end of run).
    placed_total: int = 0
    filled_total: int = 0
    recentered_total: int = 0
    blocked_by_risk: int = 0
    paused_by_liquidity: int = 0
    paused_by_sentiment: int = 0


class StatsCollector:
    """
    Fed by the runner at every fill and every tick. Owns per-symbol
    state and produces a `BacktestReport` at the end.

    FIFO matching rule (mirrors RiskManager):
      - An incoming BUY is matched against any open SELL lots. That's a
        SHORT being closed, realizing SELL_price - BUY_price per unit.
      - An incoming SELL is matched against any open BUY lots. LONG
        being closed, realizing SELL - BUY.
      - Unmatched quantity becomes a new open lot on the same side.

    Why no rolling window: backtests span arbitrary virtual time. We
    want totals, not 24h totals. RiskManager's window is for live
    operation's daily-loss floor which is a different question.
    """

    def __init__(self, symbols: list[str]):
        self._symbols = list(symbols)
        self._stats: dict[str, SymbolStats] = {s: SymbolStats(symbol=s) for s in symbols}
        self._inventory: dict[str, deque[_Lot]] = {s: deque() for s in symbols}
        # Equity curve for drawdown. Cumulative realized PnL at each fill
        # across ALL symbols. Peak/trough tracked lazily.
        self._equity_curve: list[float] = [0.0]
        self._cum_realized: float = 0.0
        self._peak: float = 0.0
        self._max_drawdown: float = 0.0

    # ------------------------------------------------------------------
    # Tick bookkeeping — called for every price update from the runner.
    # ------------------------------------------------------------------

    def record_tick(self, symbol: str, price: float) -> None:
        st = self._stats.get(symbol)
        if st is None:
            return
        if st.first_price is None:
            st.first_price = price
        st.last_price = price
        if price < st.min_price:
            st.min_price = price
        if price > st.max_price:
            st.max_price = price

    # ------------------------------------------------------------------
    # Fill accounting — called when the backtest exchange fires a fill.
    # ------------------------------------------------------------------

    def record_fill(self, symbol: str, side: str, price: float, qty: float) -> None:
        st = self._stats.get(symbol)
        inv = self._inventory.get(symbol)
        if st is None or inv is None:
            # Not a symbol we're tracking — silently ignore. The runner
            # only reports on its configured symbols.
            return

        if side == "BUY":
            st.buys_filled += 1
            st.buy_volume_base += qty
            st.buy_volume_quote += price * qty
        else:
            st.sells_filled += 1
            st.sell_volume_base += qty
            st.sell_volume_quote += price * qty

        # FIFO match against opposite side lots.
        remaining = qty
        while remaining > 0 and inv and inv[0].side != side:
            lot = inv[0]
            match_qty = min(remaining, lot.qty)
            # Realize PnL for this match:
            #   if incoming BUY closing short SELL lot: realized = (lot.price - price) * qty
            #   if incoming SELL closing long BUY lot:  realized = (price - lot.price) * qty
            if side == "BUY":
                realized = (lot.price - price) * match_qty
            else:
                realized = (price - lot.price) * match_qty
            st.realized_pnl_usd += realized
            self._cum_realized += realized
            self._equity_curve.append(self._cum_realized)
            if self._cum_realized > self._peak:
                self._peak = self._cum_realized
            drop = self._cum_realized - self._peak  # <= 0
            if drop < self._max_drawdown:
                self._max_drawdown = drop

            lot.qty -= match_qty
            remaining -= match_qty
            if lot.qty <= 1e-12:
                inv.popleft()

        # Unmatched quantity opens a new lot on the incoming side.
        if remaining > 0:
            inv.append(_Lot(side=side, price=price, qty=remaining))

        # Net inventory = sum of BUY lots - sum of SELL lots.
        st.open_inventory_base = sum(
            (lot.qty if lot.side == "BUY" else -lot.qty) for lot in inv
        )

    # ------------------------------------------------------------------
    # Engine counter snapshot — called at end of run per engine.
    # ------------------------------------------------------------------

    def record_engine_counters(self, symbol: str, counters: dict) -> None:
        st = self._stats.get(symbol)
        if st is None:
            return
        st.placed_total = int(counters.get("placed_total", 0))
        st.filled_total = int(counters.get("filled_total", 0))
        st.recentered_total = int(counters.get("recentered_total", 0))
        st.blocked_by_risk = int(counters.get("blocked_by_risk", 0))
        st.paused_by_liquidity = int(counters.get("paused_by_liquidity", 0))
        st.paused_by_sentiment = int(counters.get("paused_by_sentiment", 0))

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def build_report(
        self,
        *,
        duration_virtual_s: float,
        duration_wall_s: float,
        total_ticks: int,
    ) -> "BacktestReport":
        return BacktestReport(
            duration_virtual_s=duration_virtual_s,
            duration_wall_s=duration_wall_s,
            total_ticks=total_ticks,
            per_symbol=dict(self._stats),
            total_realized_pnl_usd=self._cum_realized,
            max_drawdown_usd=self._max_drawdown,
        )


@dataclass
class BacktestReport:
    duration_virtual_s: float
    duration_wall_s: float
    total_ticks: int
    per_symbol: dict[str, SymbolStats]
    total_realized_pnl_usd: float
    max_drawdown_usd: float

    def render(self) -> str:
        """Human-readable summary printed by the CLI. Plain ASCII so it
        renders cleanly on Windows cmd and in piped files alike."""
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("BACKTEST REPORT")
        lines.append("=" * 72)
        lines.append(f"virtual time : {self.duration_virtual_s:.1f}s "
                     f"({self.duration_virtual_s / 3600:.2f}h)")
        lines.append(f"wall time    : {self.duration_wall_s:.2f}s")
        lines.append(f"ticks        : {self.total_ticks}")
        lines.append("")
        lines.append(f"TOTAL REALIZED PnL : {self.total_realized_pnl_usd:+.2f} USD")
        lines.append(f"MAX DRAWDOWN       : {self.max_drawdown_usd:+.2f} USD "
                     "(worst drop from peak of realized-PnL curve)")
        lines.append("")
        for sym, st in self.per_symbol.items():
            lines.append(f"[{sym}]")
            # Guard against the no-ticks case so we don't print "inf" / "-inf".
            if st.first_price is None:
                lines.append("  no ticks received")
                continue
            drift = ((st.last_price or 0) - st.first_price) / st.first_price * 100.0
            lines.append(
                f"  price          : {st.first_price:.2f} -> {st.last_price:.2f} "
                f"({drift:+.2f}%), range [{st.min_price:.2f}, {st.max_price:.2f}]"
            )
            lines.append(
                f"  fills          : {st.buys_filled} BUY / {st.sells_filled} SELL "
                f"(total {st.buys_filled + st.sells_filled})"
            )
            lines.append(
                f"  volume         : {st.buy_volume_base:.4f} base bought, "
                f"{st.sell_volume_base:.4f} base sold"
            )
            lines.append(f"  realized PnL   : {st.realized_pnl_usd:+.2f} USD")
            lines.append(
                f"  open at end    : {st.open_inventory_base:+.4f} base "
                "(unrealized, not marked)"
            )
            lines.append(
                f"  engine counts  : placed={st.placed_total} "
                f"filled={st.filled_total} recentered={st.recentered_total}"
            )
            gates = []
            if st.blocked_by_risk:
                gates.append(f"risk={st.blocked_by_risk}")
            if st.paused_by_liquidity:
                gates.append(f"liquidity={st.paused_by_liquidity}")
            if st.paused_by_sentiment:
                gates.append(f"sentiment={st.paused_by_sentiment}")
            if gates:
                lines.append(f"  signal gates   : {' '.join(gates)}")
            lines.append("")
        lines.append("=" * 72)
        return "\n".join(lines)

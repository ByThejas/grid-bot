"""
Integration test for BacktestRunner.

This runs the FULL pipeline — BacktestExchange + GridEngine + RiskManager
+ LiquiditySignal + StatsCollector — against a short synthetic random
walk. It's a smoke test; the heavy math is locked down in the per-module
tests. Here we check the glue:

  - runner initialises without error on real settings.yaml
  - engines place a grid when the first tick seeds a price
  - random-walk replay produces fills (round-trips)
  - shutdown unwinds cleanly
  - report contains the symbols we configured
"""

import asyncio
import sys
import traceback

from src.backtest.runner import BacktestOptions, BacktestRunner
from src.backtest.sources import SyntheticRandomWalkSource


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_runner_synthetic_smoke():
    async def main():
        # 2000 ticks around $75k with enough sigma that fills will happen.
        # 0.3% grid spacing → ~$225 between levels; sigma 0.002 → ~$150
        # per-step std deviation. Over 2000 ticks we definitely hit
        # multiple grid levels.
        source = SyntheticRandomWalkSource(
            symbol="BTCUSDT", start_price=75_000.0, n_ticks=2000,
            sigma=0.002, seed=7,
        )
        opts = BacktestOptions(
            symbols=["BTCUSDT"],     # single symbol keeps the test fast
            drift_check_every=50,
            enable_risk=True,
            enable_liquidity=True,
            enable_sentiment=False,
        )
        runner = BacktestRunner(source=source, options=opts)
        report = await runner.run()

        # Sanity asserts — exact numbers depend on RNG state, so we
        # assert shape not magnitude.
        assert report.total_ticks == 2000
        assert "BTCUSDT" in report.per_symbol
        st = report.per_symbol["BTCUSDT"]
        assert st.first_price == 75_000.0
        # The engine should have placed AT LEAST the initial grid.
        assert st.placed_total >= 1, "engine didn't place any orders"
        # Sigma 0.002 over 2000 ticks — virtually certain some fills happened.
        assert (st.buys_filled + st.sells_filled) > 0, "no fills in a 2000-tick walk"
        # Rendered report should contain the symbol + PnL header.
        text = report.render()
        assert "BTCUSDT" in text
        assert "TOTAL REALIZED PnL" in text

    _run(main())


_TESTS = [test_runner_synthetic_smoke]


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

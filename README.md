# Grid Bot

An automated grid trading system for **BTC/USDT** and **ETH/USDT** on Binance Spot, written in Python.

It places a ladder of buy and sell orders around the current price. When a sell fills it places a new buy below, and when a buy fills it places a new sell above, so each completed round trip captures a small profit from price oscillation, without having to predict direction. On top of the grid sit three independent signals and a layered risk system.

> **Status:** testnet-validated (a 25-hour continuous run on Binance testnet). Real-money use is limited to small canary sizes. See [Risk and disclaimer](#risk-and-disclaimer).

## How it works

```
Market data  ->  Signals  ->  Grid engine  ->  Risk manager  ->  Exchange
(trades, book)   (3 inputs)   (levels, sizing)  (limits, kill)    (Binance)
```

| Layer | What it does | Code |
|---|---|---|
| Market data | Streams trades and the order book, stores them in SQLite | `src/market_data/` |
| Signals | Supply and demand zones, order book liquidity, news sentiment | `src/signals/` |
| Grid engine | Builds levels, sizes orders, re-centres when price drifts | `src/grid/` |
| Risk | Exposure cap, stop loss, drawdown tiers, daily loss kill switch | `src/risk/` |
| Exchange | Binance REST and WebSocket clients, a dry-run exchange, order filters | `src/exchange/` |
| Backtesting | Replays synthetic or recorded market data through the real engine | `src/backtest/` |
| Notifications | Telegram alerts and remote `/status` and `/halt` commands | `src/notifications/` |
| Dashboard | Small local HTTP status page | `src/dashboard/` |

### The three signals

- **Supply and demand** (weight 0.50): finds price zones where large orders reacted, using trade-flow imbalance and persistent order book walls. Defines the grid bounds.
- **Liquidity** (weight 0.30): reads spread and depth. Widens spacing when the book is thin and pauses placement when the spread is abnormal.
- **Sentiment** (weight 0.20): pauses new placement on strongly negative news. Stale readings revert to neutral.

The bot deliberately avoids classic indicators (RSI, MACD, moving averages, Bollinger Bands).

### Risk controls

- Maximum portfolio exposure and a maximum number of open orders
- Per-trade stop loss
- Drawdown tiers: reduce order size, pause the grid, then cancel everything and flatten
- Daily loss limit that trips a kill switch
- Stricter overrides when running on mainnet
- A `HALT` sentinel file and a Telegram `/halt` command to stop it remotely

## Project layout

```
config/      settings.yaml (all behaviour lives here), logging.yaml
src/         bot code (see table above)
scripts/     backtest, sentiment setter, key generation, maintenance tools
tests/       pytest suite
deploy/      systemd units for the cloud VM
docs/        design notes, runbook and a strategy and risk overview
```

## Getting started

Requires Python 3.11+.

```bash
git clone https://github.com/ByThejas/grid-bot.git
cd grid-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in your Binance testnet keys
```

Run on testnet (the default), or without placing any orders:

```bash
python -m src.grid.grid_runner --dry-run     # no orders placed
python -m src.grid.grid_runner               # Binance testnet
python -m src.dashboard.server               # dashboard on http://127.0.0.1:8080
```

Real money requires both `--env mainnet` and an explicit `--live` flag (or `BINANCE_LIVE`):

```bash
python -m src.grid.grid_runner --env mainnet --live
```

### Backtesting

```bash
python -m scripts.backtest --mode synthetic --symbol BTCUSDT --ticks 5000
python -m scripts.backtest --mode db --symbol BTCUSDT --symbol ETHUSDT
```

### Tests

```bash
pytest
```

## Configuration

Behaviour is controlled from `config/settings.yaml`: trading pairs, grid spacing and order sizes, risk limits, signal weights and Telegram settings. Secrets stay in `.env`, which is never committed. Use withdrawal-disabled API keys with an IP whitelist.

## Deployment

`deploy/` contains systemd units and `scripts/provision_vm.sh` sets up a small cloud VM. The full walkthrough is in [`docs/runbook/cloud_deploy.md`](docs/runbook/cloud_deploy.md). Design notes are in [`docs/design/`](docs/design/) and a strategy and risk overview is in [`docs/pitch/`](docs/pitch/).

## Tech stack

Python, asyncio, aiohttp, python-binance, websockets, pandas, numpy, SQLite (aiosqlite), structlog, pytest.

## Risk and disclaimer

This is a personal project for learning and research. Trading cryptocurrency carries a real risk of loss, and past or simulated results do not guarantee future performance. Nothing here is financial advice. Run it on testnet first, start with small sizes and use it at your own risk.

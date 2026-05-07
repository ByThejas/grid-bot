# Phase 11D — Dynamic Balance-Adjusted Sizing

**Status:** design — drafted 2026-05-04
**Owner:** Thejas U
**Depends on:** Phase 11A (Telegram alerts), Phase 11B (dashboard), Phase 11C (cloud VM)
**Successor:** none — last functional phase of the build

---

## 1. Goals & Non-Goals

### Goals
- Order sizes adapt to wallet equity. Wallet grows from $200 → $2000 → $20000, the bot scales without operator intervention.
- Daily loss floor is a percentage of equity, not a fixed USD value. A 5% floor that was $20/day at $400 equity becomes $1000/day at $20k equity — same risk posture, different absolute number.
- Equity is recomputed periodically so accumulated fills and price moves continuously feed back into sizing.
- A flash-crash failsafe — if equity drops sharply within a short window, the bot pauses placement before the daily loss floor would trip. Reduces the window in which a fast move can cascade.
- Backward compatible — the existing `order_size` and `daily_loss_limit_usd` fields keep working if dynamic sizing is disabled.

### Non-Goals
- **Multi-asset rebalancing.** The bot doesn't try to keep BTC/ETH/USDT in some target ratio. It just sizes orders against current equity.
- **Per-symbol sizing differentiation.** All symbols share the same `order_size_pct` and `daily_loss_pct`. Per-symbol overrides could land in a future phase if needed.
- **Margin / leverage / futures.** Equity here means spot wallet value in USDT-equivalent. Phase 12+ if we ever do futures.
- **Smarter equity definitions.** No moving averages, no time-weighted equity, no risk-of-ruin math. Just a periodic snapshot.
- **Per-fill resizing.** Sizes are fixed for the duration of a grid placement; we only recompute on the polling cadence + at recenter. Avoids whipsaw between adjacent levels.
- **Alerts on every sizing change.** Routine adaptation is silent (it'd be noise). Only the flash-crash pause and the at-startup banner show new sizes explicitly.

---

## 2. Architecture

### Where the new logic lives

```
src/risk/balance_poller.py    — NEW. BalancePoller class, async polling task.
src/risk/manager.py           — modified. RiskManager.daily_loss_limit_usd becomes a property/method that reads from the poller.
src/grid/engine.py            — modified. order_size used by _place_grid comes from the poller, not the static config.
src/grid/grid_runner.py       — modified. Constructs BalancePoller, awaits its first equity refresh BEFORE engine init, passes it to RiskManager + GridEngine.
config/settings.yaml          — new dynamic_sizing: block.
```

### Data flow

```
Binance REST API
       ↓
   c.get_account()       ← spot balances (USDT, BTC, ETH)
       ↓
   c.get_ticker()        ← BTC/USDT and ETH/USDT prices
       ↓
   ┌─────────────────────────────────┐
   │ BalancePoller (asyncio task)    │
   │  - polls every 30min            │
   │  - computes equity_usdt         │
   │  - keeps rolling history        │
   │  - exposes accessors            │
   │      current_equity_usdt()      │
   │      order_size_for(symbol)     │
   │      daily_loss_limit_usd()     │
   │      is_flash_crashed()         │
   └─────────────────────────────────┘
       ↓                ↓                 ↓
   GridEngine     RiskManager       HeartbeatMonitor
   (sizing)       (floor)           (STATUS.json)
```

### Why a separate class

The `BalancePoller` owns its own async task and rolling-history state. Could in principle live inside `RiskManager`, but:
- `RiskManager` is sync-only by current design (called from threads, no `await`); polling is async.
- Equity snapshots are useful to multiple consumers (engine, risk, heartbeat). A separate class with clean accessors avoids cross-coupling.
- Easier to test in isolation — fake out the exchange ticker and balance calls.

---

## 3. Equity Calculation

### Formula

```
equity_usdt = free_USDT + locked_USDT
            + (free_BTC + locked_BTC) × BTC_USDT_mid
            + (free_ETH + locked_ETH) × ETH_USDT_mid
```

Locked balances (assets sitting in open orders) DO count — they're still the operator's money, just not currently spendable for new orders. Without including them, the act of placing the grid would shrink "equity" each time and resize itself smaller.

### What's NOT in equity

- BNB, USDC, BUSD, and any other coins. Three reasons:
  - Operator may hold BNB for fee discounts, not as a position. Don't size against fee balance.
  - USDC / BUSD have non-trivial peg behaviour and bridging costs back to USDT.
  - The bot doesn't trade these pairs anyway.
- Earn / Savings / Locked staking balances. The Spot account endpoint doesn't return them.
- Margin / Futures account balances. Different endpoint, out of scope.

If the operator wants "BNB counts toward equity" later, that's a one-line tweak — but as a default we exclude.

### Source of truth

- **Balances:** `client.get_account()['balances']` — single REST call, returns all assets.
- **Prices:** `client.get_symbol_ticker(symbol='BTCUSDT')` and `'ETHUSDT'` — two REST calls. Cheap, low-latency, no signing required.
- **Caching:** the BalancePoller caches the most recent successful snapshot. If a future poll fails, we use the cached value rather than reverting to fixed-config defaults.

### When equity is unavailable

If the FIRST poll fails (bot can't reach Binance at startup), we fail-CLOSED:
- Bot logs an ERROR and exits with code 4 (new exit code; systemd-restartable, since it's a transient network issue).
- Operator's phone gets a `[CRASH]` alert with the underlying error.
- This is intentional — without equity we can't compute sizes; better to retry than to guess.

If a SUBSEQUENT poll fails (transient API blip during normal operation):
- Bot keeps the last-known-good equity.
- Logs a WARN.
- Continues sizing against stale equity until the next successful poll.
- After 3 consecutive failures, fires a `[PAUSE]` alert (`source=balance_poller`).

---

## 4. Order Sizing Math

### From equity to base-asset quantity

```python
target_notional_per_level_usdt = equity_usdt × (order_size_pct / 100)
order_size_in_base = target_notional_per_level_usdt / current_mid_price
order_size_quantized = round_down_to_step(order_size_in_base, step_size)
```

### Floors and caps

- **Floor:** Binance's `min_notional` is the absolute minimum order value (~$10 for BTC/ETH on mainnet). If `target_notional_per_level_usdt < min_notional`, we round UP to `min_notional / current_price`. This means tiny wallets still trade, just with disproportionately-large orders relative to equity. A WARN log line is emitted in that case so the operator notices they're under-funded.
- **Cap:** `max_order_size_pct` (default 2.0%, off by default). If equity grows faster than the operator wants risk to scale, this knob caps the per-level size at a fixed percentage. A wallet that 10x's overnight wouldn't suddenly place 10x-notional orders.

### Concrete numbers (worked examples)

| Equity | order_size_pct=0.5% | BTC mid | BTC qty | Effective notional |
|---|---|---|---|---|
| $200 | $1.00/level (BELOW min_notional) | $76,000 | floored to ~$10/0.000131... → 0.0002 (rounded to step) | $15.20 (above floor) |
| $1,000 | $5.00/level | $76,000 | 0.0001 (still tiny) | enforced floor |
| $5,000 | $25.00/level | $76,000 | 0.00033 | $25.08 |
| $20,000 | $100/level | $76,000 | 0.00131 | $99.56 |

So at low equity the bot operates near the min_notional floor; the dynamic-sizing benefit kicks in once equity > ~$1,000.

### When sizes change

The poller refreshes every 30 min. **The new sizes are NOT applied to existing open orders** (we don't cancel-and-reprice live orders just because the sizing percentage produced a different number). New sizes apply at:
- Startup (always)
- Each recenter (engine already cancels the grid and rebuilds)
- Each fill replacement (engine places a new mirror order; uses the latest size)

This means sizing is stale on ALREADY-OPEN orders but fresh on every NEW order. Acceptable — over a few cycles the grid converges to the current sizing.

---

## 5. Daily Loss Floor

```python
daily_loss_limit_usd = equity_usdt × (daily_loss_pct / 100)
```

Default: 5% of equity. At $200 → $10/day floor (more conservative than current $20). At $20k → $1000/day.

The risk manager already trips on `realized_24h_usd <= -daily_loss_limit_usd`. With this change, `daily_loss_limit_usd` becomes a property/method that reads from the poller's current snapshot rather than the static config value.

### Floor refresh cadence

Same 30-min poll, plus a deliberate refresh at UTC day rollover. The daily floor is meant to track "today's risk budget" — if equity changed overnight (due to fills, deposits, withdrawals), the next day's floor should reflect today's equity, not yesterday's.

### Interaction with the existing 24h ledger

`RiskManager._pnl_ledger` is a rolling 24h window. Today's realized PnL is summed against today's floor. Nothing changes about the ledger itself; only the floor it's compared against.

---

## 6. Flash-Crash Failsafe

### Why this exists

Daily loss floor is `equity × daily_loss_pct`. But equity ITSELF changes during a crash — your BTC and ETH inventory marks down. So if BTC drops 30% in 5 minutes, equity drops with it, and the daily-loss-floor check is now comparing against a SMALLER floor. That's the wrong direction.

Concrete worked example. Pre-crash equity $1000, daily floor $50. BTC crashes 30% over 1h:
- Equity drops to ~$800 (assuming half the wallet was in BTC)
- New floor is now $40, not $50
- Realized PnL hasn't moved (no SELLs filled at crash prices yet)
- But unrealized loss is real and continuing
- Bot keeps trading because realized hasn't hit the (lower) floor

This is a hole. The flash-crash failsafe plugs it.

### Mechanism

The `BalancePoller` keeps the last N equity snapshots (with timestamps). On each refresh:

```python
if equity_now < equity_1h_ago × (1 - flash_crash_pause_pct/100):
    # Equity has dropped >X% in the last hour
    is_flash_crashed = True
    alerter.pause(source="balance_poller", symbol="*", drop="22%", ...)
```

While `is_flash_crashed` is True, `GridEngine._place_grid` skips placement (same code path that liquidity / sentiment use to pause). The bot's existing inventory keeps its exit orders on the book (fills can still happen), but new placements wait.

When equity recovers (drop is no longer > threshold against the rolling 1h reference), `is_flash_crashed` clears and the next recenter or poll re-places the grid. An `[RESUME] balance_poller` alert fires.

### Defaults

```yaml
flash_crash_pause_pct: 20.0       # equity drop threshold
flash_crash_window_minutes: 60    # rolling lookback window
```

20% in an hour is a real "something went wrong" event. Smaller drops are normal market noise; larger drops in shorter windows are exactly the cascade we want to step out of. Operator can tune both knobs.

---

## 7. Configuration

### settings.yaml additions

```yaml
# --- Dynamic Sizing (Phase 11D) ---
# Adapts order_size and daily_loss_limit_usd to current wallet equity.
# Disabled by default for backward compatibility — flip enabled to true
# after you've reviewed the math against your wallet size. See
# docs/design/phase_11d_dynamic_sizing.md for the full reasoning.
dynamic_sizing:
  enabled: false                     # master switch
  poll_interval_minutes: 30          # how often to refresh equity
  order_size_pct: 0.5                # % of equity per grid level
  daily_loss_pct: 5.0                # % of equity for daily floor
  max_order_size_pct: 2.0            # safety cap (if computed > this, clamp)
  flash_crash_pause_pct: 20.0        # pause grid if equity drops > this %
  flash_crash_window_minutes: 60     # rolling lookback for flash-crash check
  failover_warn_after_consecutive_failures: 3   # poll failure tolerance
```

### Backward compatibility

If `dynamic_sizing.enabled: false` (or the block is absent entirely):
- `BalancePoller` is constructed but is a no-op stub (`NullBalancePoller`).
- `GridEngine` falls back to the existing fixed `order_size` block in `grid:`.
- `RiskManager` falls back to the existing `daily_loss_limit_usd` from `risk:` (or the `risk_overrides.mainnet` value).
- Zero behaviour change for an operator who doesn't opt in.

The `enabled: false` default is deliberate: the operator should consciously flip the switch after deploying the new code, ideally after one polling cycle confirms the equity calculation matches what their wallet actually shows.

### Mainnet vs testnet

The poller respects the same env switch as the rest of the bot. On testnet, "equity" is testnet wallet equity, which has no real meaning but lets us validate the code path. The flash-crash failsafe works the same way on both.

---

## 8. Failure Modes

| Scenario | Behaviour |
|---|---|
| Binance balance API down at startup | Bot exits with code 4. systemd restarts. `[CRASH]` alert fires. |
| Binance balance API down mid-run, 1 failure | Last-known-good equity used. WARN log. No alert. |
| Binance balance API down mid-run, 3+ consecutive failures | `[PAUSE] balance_poller` alert. Bot keeps trading on stale equity. |
| Equity calculation produces negative or zero value | Treated as a poll failure (not a sizing input). Last-known-good held. |
| Equity drops > flash_crash_pause_pct in window | `[PAUSE] balance_poller` alert. New placements skip. Existing grid keeps fills. |
| Equity grows past `max_order_size_pct × equity` per-level | Cap kicks in, per-level size is clamped. Single INFO log line. No alert (routine). |
| Equity below `min_notional × levels_above × symbols` | Each placement falls back to the floor. Operator should top up — surfaced via WARN log line every grid placement until equity grows. |
| `dynamic_sizing.enabled: true` but no `dynamic_sizing` keys defined | `BalancePoller.__init__` raises early. Bot exits cleanly with config error. Operator fixes settings.yaml. |
| Operator switches enabled false → true mid-run | Requires bot restart. `systemctl restart grid-bot` is the documented path. |

---

## 9. Testing Plan

### Unit tests
- `BalancePoller.compute_equity_usdt` — pure function over canned balance/ticker dicts. Covers: zero balances, BTC-only, ETH-only, mixed, free+locked combination, missing assets (treated as 0).
- `BalancePoller.order_size_for(symbol)` — equity × pct ÷ price, with floor and cap branches.
- `BalancePoller.daily_loss_limit_usd` — equity × pct.
- `BalancePoller.is_flash_crashed` — synthetic snapshot history, drop threshold tripped / not tripped.
- `BalancePoller` failover — patched exchange that raises N times, verifies stale-but-good behaviour and the 3-failure pause-alert.
- `NullBalancePoller` returns the static-config values unchanged. Ensures backward-compat path.
- `GridEngine._place_grid` reads sizes from poller, not static config, when poller is wired.
- `RiskManager.daily_loss_limit_usd` returns dynamic value when poller present, static otherwise.

### Integration smoke (testnet)
- Launch bot on testnet with `dynamic_sizing.enabled: true`.
- Verify [BOOT] log line shows the computed equity and per-symbol sizes.
- Verify the placed orders' sizes match the computed sizes (within step-size rounding).
- Verify a heartbeat after the first poll cycle includes equity in STATUS.json.

### Manual smoke (mainnet)
- After test net validation, deploy to cloud VM with `dynamic_sizing.enabled: false` first. Observe one polling cycle confirms equity calculation matches Binance UI.
- Then flip `enabled: true`, restart, verify new sizes are sane.

---

## 10. Implementation Plan

```
✅ Step 1   HALT-storm pre-fix (commit f7a15ee — already on this branch)
⏳ Step 2   This design doc                                     [THIS COMMIT]
   Step 3   BalancePoller class + tests                          ~200 lines + 150 lines tests
   Step 4   Wire dynamic order_size into GridEngine + tests      ~50 lines + tests
   Step 5   Wire dynamic daily_loss_limit into RiskManager + tests   ~30 lines + tests
   Step 6   settings.yaml additions + .env updates                  config-only
   Step 7   Testnet smoke (manual)                                  manual; ~30min
   Step 8   Merge to master, deploy on cloud VM                     git pull + systemctl restart
```

Each implementation step is a separate commit on this branch. Tests on master after merge: targeting ~310-320 passing (currently 295; +15-20 new tests).

---

## 11. Out of Scope (captured for future phases)

- **Per-symbol sizing differentiation** — currently both pairs share `order_size_pct`. A future config could have per-symbol overrides if BTC and ETH justify different risk percentages.
- **Equity-targeting on deposits** — when the operator deposits more USDT, equity grows immediately but the existing grid doesn't resize. Adding a "rebalance now" Telegram command could trigger a manual recenter. Future enhancement.
- **VWAP-based equity** — instead of mid-price for non-USDT assets, use 24h VWAP. Smoother equity curve but more complex. Defer until we have evidence we need it.
- **Equity-aware spacing** — currently `base_spacing_pct` is static. A wider grid at higher equity (or vice versa) might be sensible. Out of scope; adjust spacing manually if needed.
- **Multi-account / sub-account support** — dynamic sizing assumes a single Binance account. Multi-account is a Phase 13+ concern.
- **External equity feeds** — for redundancy. Out of scope.

---

## 12. After Phase 11D

Phase 11D is the **last functional phase** of the original phased build plan. After 11D ships and a few weeks of stable operation:

- The bot is feature-complete per the original scope.
- Maintenance becomes monthly: glance at dashboard, top up if needed, review daily-summary lines.
- Quarterly: review risk caps against equity growth.
- Yearly: rotate API keys + Ed25519 keypair.

Future "phases" (if any) would be major scope changes: futures market migration (Phase 12+), additional exchanges, or operator-driven feature requests. None are committed.

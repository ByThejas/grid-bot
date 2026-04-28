# Phase 11A — Telegram Alerts & Remote Control

**Status:** Design (not yet implemented)
**Author:** Grid Bot project
**Date:** 2026-04-28
**Target branch:** `phase-11a-telegram` (off `master @ 1014d06`, the canary commit)

---

## 1. Goals & Non-Goals

### Goals
1. **Phone notifications for important bot events** — fills, halts, pauses, crashes, daily summary — so the operator doesn't need to watch a terminal.
2. **Remote query of bot state** — `/status` from a phone returns the same data as STATUS.json.
3. **Remote halt** — `/halt` from a phone trips the kill switch from anywhere on internet.
4. **Failure-isolated** — if Telegram fails, the bot keeps trading. Alerts are best-effort, never block the hot path.
5. **Disable cleanly** — flipping one config flag or removing the bot token from `.env` returns the bot to its current behavior with zero side effects.

### Non-Goals
- Replace STATUS.json or structured logs (Telegram is human-facing, logs are machine-facing).
- Approval workflow for trades ("should I place this?"). The bot stays autonomous; Telegram is observe + halt only.
- Multi-user / multi-operator support. One operator, one chat ID for v1.
- Trading commands beyond halt (`/buy`, `/cancel`, `/resize`). These are dangerous and would expand the attack surface; they belong in a future phase if at all.
- Trade history or analytics queries (`/pnl`, `/fills`). v1 stops at status + halt. Add later if needed.

---

## 2. Architecture

### Integration model
A single `Alerter` object is constructed at bot startup and passed to the components that emit alert-worthy events. Components call `alerter.fill(...)`, `alerter.halt(...)`, etc. without caring whether the underlying implementation actually sends to Telegram.

```
+-------------------+       +----------------+       +-----------------+
|  GridEngine       |       |  RiskManager   |       |  HeartbeatMon.  |
|  (on fill)        |       |  (on trip)     |       |  (daily summary)|
+---------+---------+       +-------+--------+       +--------+--------+
          |                         |                         |
          v                         v                         v
                 +---------------------------+
                 |   Alerter (interface)     |
                 +-------------+-------------+
                               |
            +------------------+------------------+
            |                                     |
            v                                     v
    NullAlerter (no-op)                  TelegramAlerter (live)
    when telegram.enabled=false          - send queue (async)
    or token missing                     - command listener (async)
                                         - HTTP via httpx
```

### Why an ABC, not a feature flag inside one class
- `NullAlerter` makes the disabled path completely free of network code, queue logic, or async tasks.
- Tests can construct `NullAlerter` and exercise components without mocking.
- A misconfigured Telegram (bad token, banned bot) cannot accidentally affect the trading hot path because the failure is contained within `TelegramAlerter`.

### Two long-running async tasks inside `TelegramAlerter`
1. **Sender task** — drains an `asyncio.Queue` of pending alerts. Calls Telegram's `sendMessage` API. Retries with exponential backoff on transient failures. Drops oldest alerts if queue overflows.
2. **Listener task** — polls Telegram's `getUpdates` API. Filters incoming messages by allowed chat IDs. Dispatches recognized commands. Silently ignores everything else.

Both tasks are started by `grid_runner` alongside the existing tasks (engine, heartbeat, risk monitor).

### Why httpx instead of `python-telegram-bot` or `aiogram`
- The Bot API surface we need is tiny (`sendMessage`, `getUpdates`, `setMyCommands`).
- Adding a heavy dependency for ~50 lines of code is poor cost/benefit.
- httpx is already a dependency (used by python-binance internals).
- Avoids being pinned to a Telegram library's release schedule and breaking changes.

---

## 3. Event Taxonomy

Each alert has: a tag, a priority, a throttling rule, a default message format.

| Event | Tag | Priority | Default | Throttling |
|---|---|---|---|---|
| Bot startup | `[BOOT]` | low | enabled | one per session |
| Bot shutdown (clean) | `[STOP]` | low | enabled | one per session |
| Bot crash (uncaught exception) | `[CRASH]` | critical | enabled | unlimited |
| Order filled | `[FILL]` | medium | enabled | one per fill |
| Kill switch tripped (HALT file or daily loss floor) | `[HALT]` | critical | enabled | unlimited |
| Sentiment pause / liquidity pause activated | `[PAUSE]` | medium | enabled | first per session per symbol; suppress repeats while still paused |
| Pause cleared | `[RESUME]` | low | enabled | only after a corresponding `[PAUSE]` |
| Heartbeat (every 5 min) | `[BEAT]` | low | **disabled** | would be 288/day; too noisy |
| Daily summary at UTC midnight | `[DAILY]` | medium | enabled | one per UTC day |
| Recenter event | `[RECENTER]` | low | **disabled by default** | grid drift > 2%; can be enabled if user wants visibility |

**Throttling rationale.** Telegram has a 30 messages/second limit per bot, but more importantly, an operator's phone has an attention budget. The bot shouldn't push more notifications per day than the operator wants to read. Heartbeats are the obvious one to mute — STATUS.json captures all the same data passively.

**Priority semantics.** All alerts use the same Telegram channel; priority is informational only. The tag prefix lets the operator triage at a glance. (Future v2 could route critical alerts to a different chat ID for louder phone notification settings.)

---

## 4. Message Format

### Plain text only (no Markdown / HTML)
Markdown formatting in Telegram requires escape sequences for special characters in dynamic content (price, symbol, error messages). One missed escape = the entire message fails to send. Plain text is robust at the cost of slightly less visual polish. We can upgrade in v2 if useful.

### Format conventions

```
[TAG] short title — key=value · key=value · key=value
```

- Tags are uppercase, square-bracketed.
- Title is one short phrase.
- Key-value pairs separated by ` · ` (middle dot).
- All numeric fields use 2-decimal USD or appropriate symbol precision.
- Each message ≤ 200 characters when possible.

### Example messages

```
[BOOT] mainnet — env=mainnet · symbols=BTCUSDT,ETHUSDT · max_open=10 · loss_floor=$20

[FILL] BTC sold — price=$76,213.40 · qty=0.0003 · side=SELL · realized=+$0.18

[FILL] ETH bought — price=$2,269.73 · qty=0.005 · side=BUY · cost=$11.35

[PAUSE] ETH liquidity — spread=62bps · threshold=50bps · side=ETHUSDT

[RESUME] ETH liquidity normal — spread=12bps

[HALT] daily loss floor breached — realized_24h=-$21.40 · floor=-$20.00 · open_orders=8

[CRASH] uncaught exception in grid_engine — RuntimeError: order book empty for ETHUSDT (recent traceback in logs)

[DAILY] 2026-04-28 UTC — fills=42 · realized=$3.18 · recenters=2 · halts=0 · uptime=24h00m

[STOP] clean shutdown — final_open=8 · session_realized=$3.18 · uptime=8h22m
```

### Length budget
- Hard cap: 4096 chars (Telegram limit). We aim for ≤ 200.
- Tracebacks in `[CRASH]` are truncated to the last 5 frames; full trace stays in the log file.

---

## 5. Two-Way Commands

For v1 we support **three** commands. Anything else is silently ignored (responding to unknown commands invites probing).

### `/status`
Replies with current bot state. Same data as STATUS.json plus uptime.

```
[STATUS] running — env=mainnet · open_orders=7 · placed_total=12 · filled_total=4 · realized_24h=+$0.46 · halted=false · uptime=2h14m
```

### `/halt`
Creates the `runtime/HALT` sentinel file (same effect as `touch runtime/HALT` on the host). Replies with confirmation. The bot's existing kill-switch monitor picks it up within ~2 seconds and cancels all orders.

```
[HALT] manual halt requested via Telegram — sentinel created at runtime/HALT
```

After this, the next status update will show `halted=true` and `[HALT]` outbound alert from the kill-switch monitor will fire as well. The operator gets two confirmations.

### `/help`
Lists available commands. Static reply.

```
[HELP] commands — /status · /halt · /help
```

### Commands NOT in v1
- `/unhalt` — halt is sticky by design (one-way trapdoor). Re-running the bot is the unhalt.
- `/pause` / `/resume` — adds a new state and complexity. v2 if useful.
- `/cancel <orderid>` — order management via Telegram is too easy to fat-finger.
- `/restart` — reboots are deliberate operator actions, not chat commands.

---

## 6. Security Model

### Threat model
- **Token leak** — anyone with the bot token can send messages AS the bot to any chat the bot is in. Worst case: an attacker spams or DoSes our channel. Mitigated by rotation (regenerate via @BotFather).
- **Chat ID guessing** — chat IDs are integers but large. Brute-forcing the allowed list via Telegram is impractical.
- **Replay** — Telegram messages aren't signed; we trust Telegram's TLS to the API endpoint.
- **Compromised allowed chat** — if your phone is unlocked by an attacker, they can `/halt` (annoying but bounded) or read alerts (confidential trade info leaks). Out of scope for v1; a phone lock is the operator's responsibility.

### Required env vars (in `.env`)

```
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_ALLOWED_CHAT_IDS=<your chat id>   # comma-separated if multiple
```

### Inbound message handling
1. Receive message from Telegram via `getUpdates`.
2. **Reject if** `message.chat.id not in TELEGRAM_ALLOWED_CHAT_IDS`. **Silently** — no reply. Log the attempt at WARN level.
3. **Reject if** message text doesn't start with a known command. Silently.
4. Otherwise, dispatch.

### Outbound message destinations
Only the allowed chat IDs receive alerts. The bot never broadcasts.

### Token storage
- `.env` is already in `.gitignore`. Token never lands in git.
- On AWS migration (Phase 11C), `.env` lives in the EC2 home directory with `chmod 600`. Future: move to AWS Secrets Manager (Phase 12+).

### Graceful disable
If `TELEGRAM_BOT_TOKEN` is missing or empty in `.env`, `Alerter` factory returns `NullAlerter`. Bot logs one warning at startup and continues. No crash, no degraded trading.

---

## 7. Failure Modes

| Scenario | Behavior |
|---|---|
| Telegram API returns 5xx | Sender task retries with exponential backoff (1s, 2s, 4s, 8s, max 30s). Trading uninterrupted. |
| Telegram API returns 4xx (e.g. invalid token) | Sender task logs ERROR, sets a "broken" flag, drops further alerts (no point retrying). Trading uninterrupted. |
| Send queue overflows (>100 buffered alerts) | Drop oldest. Increment `dropped_count`. Next successful alert prefixes `(N alerts dropped due to queue overflow)`. |
| Network partition (laptop offline) | Same as 5xx. Alerts queued, sent when network returns. If queue overflows during outage, oldest dropped. |
| Bot process crashes | All Telegram tasks die with the process. **Operator gets no alert.** This is a real gap — solved in Phase 11C+ by an external watchdog (e.g. AWS CloudWatch alarm on STATUS.json freshness). Out of scope for 11A. |
| Listener task crashes (unhandled exception) | Caught at task boundary, logged, task restarts. After 5 restarts in 5 min, listener disables itself. Outbound alerts unaffected. |
| Bot token rotated in @BotFather without restarting bot | Bot uses old (now-invalid) token. Sender gets 401, sets broken flag, alerts stop. Operator notices via missing alerts. Restart fixes. |

### What we explicitly do NOT do in v1
- External dead-man's-switch ("if no heartbeat in 30 min, alert me from elsewhere"). Belongs to AWS phase.
- Persistent queue across bot restarts. Pending alerts on shutdown are lost. Acceptable — they're transient by nature.

---

## 8. Configuration

### `config/settings.yaml` additions

```yaml
# --- Notifications (Phase 11A) ---
telegram:
  enabled: true                     # master switch; false = NullAlerter
  send_timeout_seconds: 5
  max_queue_size: 100
  alert_on:
    boot: true
    shutdown: true
    crash: true
    fill: true
    halt: true
    pause: true
    resume: true
    heartbeat: false                # silenced — STATUS.json covers this
    daily_summary: true
    recenter: false
```

### `.env` additions

```
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_CHAT_IDS=<id1,id2>
```

### Default if section missing
If `telegram` block is absent from settings.yaml entirely, behave as if `enabled: false`. Backward-compatible.

---

## 9. File Layout

New files:
```
src/notifications/
├── __init__.py
├── alerter.py        # Alerter ABC, NullAlerter, factory function
├── telegram.py       # TelegramAlerter (sender + listener tasks, httpx client)
└── format.py         # Pure functions: format_fill(), format_halt(), etc. (testable in isolation)

tests/notifications/
├── test_format.py
├── test_alerter_null.py
└── test_telegram_offline.py    # mocks httpx; no real network
```

Modified files:
```
src/grid/grid_runner.py      # construct alerter, pass to components, start tasks
src/grid/grid_engine.py      # call alerter.fill() on fill events
src/risk/manager.py          # call alerter.halt() in trip()
src/observability/heartbeat.py  # call alerter.daily_summary() on UTC rollover
src/signals/liquidity.py     # call alerter.pause() / alerter.resume()
src/signals/sentiment.py     # call alerter.pause() / alerter.resume()
config/settings.yaml         # add telegram block
.env.example                 # add TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS placeholders
```

---

## 10. Testing Plan

### Unit tests
- `format.py` — every alert formatter, with edge cases (very long error messages, unicode in symbol, zero quantities).
- `NullAlerter` — every method returns None and does nothing.
- `TelegramAlerter` with mocked httpx — verifies request payloads, retry logic, queue overflow, chat-id filtering on inbound.

### Integration test against testnet (manual)
1. Operator creates Telegram bot via @BotFather, gets token + own chat ID.
2. Operator adds `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS` to `.env`.
3. Run `python -m src.grid.grid_runner --env testnet`.
4. Verify within 10 seconds:
   - `[BOOT]` message received on Telegram
   - `/status` from phone replies with running state
5. Wait for first testnet fill → verify `[FILL]` message arrives.
6. Send `/halt` from phone → verify bot cancels all orders within ~5 seconds AND a `[HALT]` follow-up arrives.
7. Send a message from a *different* Telegram account (not in `TELEGRAM_ALLOWED_CHAT_IDS`) → verify the bot does NOT respond and a WARN log line appears server-side.

This integration test runs against **testnet** — never the live mainnet canary — and on the `phase-11a-telegram` branch.

---

## 11. Rollback / Disable

Three escape hatches, in order of preference:

1. **Set `telegram.enabled: false` in settings.yaml**, restart bot. NullAlerter takes over. Zero Telegram code path executes.
2. **Remove or blank `TELEGRAM_BOT_TOKEN` in `.env`**, restart bot. Alerter factory returns NullAlerter. Logs one warning at startup.
3. **Revert the merge commit on master.** Returns to pre-Phase-11A behavior.

All three are safe to perform with the bot running (after stopping it). None require code changes during an outage.

---

## 12. Open Decisions Before Implementation

These need a yes/no from the operator before I start coding. Defaults shown in **bold** are what I'd pick if no input.

1. **Library: direct httpx vs. python-telegram-bot?** Default: **httpx**. ~50 lines vs. heavy dep + breaking changes risk.
2. **Markdown formatting in messages?** Default: **plain text**. Robust against escape errors. Upgrade in v2 if useful.
3. **Heartbeat alerts on by default?** Default: **off**. 288/day is too many for a phone.
4. **Recenter alerts on by default?** Default: **off**. Recenters are routine; visible in logs and STATUS.json.
5. **Two-way commands beyond `/status`, `/halt`, `/help`?** Default: **just these three**. More commands = more attack surface.
6. **Persistent queue across restarts?** Default: **no**. Alerts are transient.
7. **External dead-man's-switch in this phase?** Default: **no**. Belongs to Phase 11C (AWS) where a separate process can monitor.
8. **One chat ID or allow multiple operators?** Default: **support multiple via comma-separated `.env` value**, but only the operator's ID in practice for v1.

---

## 13. Implementation Plan (Once Decisions Are Locked)

Estimated ~1-2 days on the `phase-11a-telegram` branch.

| Step | What | Validates |
|---|---|---|
| 1 | Create branch off master @ 1014d06 | clean baseline |
| 2 | `format.py` + unit tests | all message formatters correct |
| 3 | `Alerter` ABC + `NullAlerter` + factory | bot still runs unchanged with NullAlerter |
| 4 | Wire calls into engine/risk/heartbeat/signals | NullAlerter no-ops; trading unchanged on testnet |
| 5 | `TelegramAlerter` sender task | outbound alerts reach Telegram in testnet |
| 6 | `TelegramAlerter` listener + commands | `/status` and `/halt` work end-to-end |
| 7 | settings.yaml block + `.env.example` | enable/disable flips work |
| 8 | Manual integration test on testnet | full workflow verified |
| 9 | PR review, merge to master AFTER canary stable | live mainnet bot picks it up on next restart |

---

## 14. Out of Scope for 11A (Captured for Future Phases)

- **Web push notifications** as alternative to Telegram. (Phase 11B, FastAPI page can do this.)
- **AWS Secrets Manager** for token storage. (Phase 12+ when we move secrets out of `.env`.)
- **External watchdog** alerting if STATUS.json goes stale. (Phase 11C with AWS, runs as a separate Lambda or EC2 cron.)
- **Inline buttons** ("HALT" button instead of text command). Telegram supports them; nice UX upgrade for v2.
- **Per-chat alert filtering** (one chat for fills, another for halts). Useful at multi-operator scale, not at one-person scale.
- **Trade analytics queries** (`/pnl 7d`, `/fills BTC`). Belongs to Phase 11B's web page where rich rendering is easier.

---

## Appendix A — Telegram Bot setup steps for the operator

These run BEFORE any code is written. Operator handles them.

1. Open Telegram, search for `@BotFather`, start a chat.
2. Send `/newbot`, follow prompts. Pick a name (display) and username (must end in `_bot`, e.g. `gridbot_thejas_bot`).
3. BotFather replies with the token. Looks like `123456789:ABC-DEF...` Copy this.
4. Open Telegram, search for the new bot, click Start. This creates a chat between you and the bot.
5. To get your chat ID:
   - Send any message to the bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (replacing `<TOKEN>`).
   - Find `"chat":{"id":<NUMBER>` in the JSON. That's your chat ID.
6. Paste both into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:ABC-DEF...
   TELEGRAM_ALLOWED_CHAT_IDS=987654321
   ```
7. (Optional) Send `/setcommands` to BotFather to register `/status`, `/halt`, `/help` so they appear in the bot's command menu on phone.

After this, the operator has the prerequisites in place and we can build.

---

*End of design.*

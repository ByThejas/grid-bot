"""
observability — heartbeat logging + status snapshot for long-running ops.

Phase 10B. The bot is fine to run unattended once Phase 10 lands, but only
if an operator can see it's alive and healthy without attaching to the
running process. This package gives us:

  * Periodic heartbeat log lines (so `tail -f logs/grid_bot.log | grep heartbeat`
    confirms liveness at a glance).
  * `runtime/STATUS.json` — atomically-written structured snapshot. An
    external watcher (cron, systemd-timer, CloudWatch agent, etc.) reads
    this without needing to talk to the Python process.
  * Daily UTC rollup lines — one structured summary per day with the
    day's fill count, recenter count, realized PnL, and pause counts.

Nothing in here talks to the exchange or changes engine behavior. It's
read-only on the engine + risk state, and fails soft (an I/O error writing
STATUS.json is logged, not raised).
"""

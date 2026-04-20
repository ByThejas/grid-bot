"""
HeartbeatMonitor — periodic liveness signal + STATUS.json snapshot.

WHY THIS EXISTS
The bot is designed to run unattended once the Phase 10 mainnet toggle is
flipped. An operator (possibly you, 2am, phone) needs two things to feel
confident the bot is alive:

  1. A regular log line that proves the event loop is still ticking and
     engines are still counting.
  2. A single file on disk — `runtime/STATUS.json` — that summarises the
     entire bot's current state at the last heartbeat, so external
     watchers (cron, systemd health checks, a dashboard shell script)
     can poll it WITHOUT attaching to the running process.

DAILY SUMMARY
Once per UTC day, on the first heartbeat after midnight, we emit a
structured `DAILY SUMMARY` log line that diffs the current counters
against the snapshot captured at the start of the day. This gives a
"yesterday in one line" view that's easy to grep:

    2026-04-21T00:00:12Z [info] DAILY SUMMARY component=heartbeat utc_day=2026-04-20
                                per_symbol={"BTCUSDT": {"filled": 42, ...}, ...}
                                realized_pnl_24h=+12.34

DESIGN CONSTRAINTS
  * Read-only on engines / risk. Never mutates state — if the snapshot
    logic has a bug, the bot keeps trading correctly.
  * Atomic writes. STATUS.json is rewritten via tmp + os.replace so a
    concurrent reader never sees a partially-written file.
  * Fails soft. If writing STATUS.json raises (disk full, permissions),
    we log a warning and keep running — the bot should not die because
    its observability layer can't reach the disk.
  * Single heartbeat task. One instance per process, spawned by the
    grid runner next to the risk monitor.
"""

import asyncio
import datetime
import json
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.risk.manager import RiskManager
from src.utils.logger import get_logger

logger = get_logger("heartbeat")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservabilityConfig:
    # How often to emit a heartbeat (log line + STATUS.json rewrite).
    # 300s (5min) is the default: frequent enough to catch a stall within
    # minutes, infrequent enough that heartbeat lines don't drown real
    # events in the log. Override in settings.yaml for canary runs.
    heartbeat_interval_s: float = 300.0

    # Where to write the atomic status snapshot. Same runtime/ directory
    # as HALT and SENTIMENT — one place the operator watches.
    status_path: str = "runtime/STATUS.json"

    # If False, skip the UTC-day-rollover DAILY SUMMARY line. The
    # heartbeat itself still fires. Disable only for debugging.
    enable_daily_summary: bool = True

    @classmethod
    def from_yaml(cls, raw: dict) -> "ObservabilityConfig":
        o = raw.get("observability", {}) or {}
        return cls(
            heartbeat_interval_s=float(o.get("heartbeat_interval_s", 300.0)),
            status_path=str(o.get("status_path", "runtime/STATUS.json")),
            enable_daily_summary=bool(o.get("enable_daily_summary", True)),
        )


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _utc_day(ts: float) -> str:
    """UTC date as YYYY-MM-DD. Stable across DST / local-tz changes."""
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def _engine_counters(engine: Any) -> dict:
    """
    Read the public-ish counter fields on a GridEngine.

    We access _placed etc. directly because they're the source of truth
    and the engine doesn't expose a public accessor for them (the values
    are emitted via structured log lines on shutdown). Read-only access
    from a different task is safe — these are simple ints touched from
    the engine task only, and a torn read of an int in CPython is still
    a valid int (GIL guarantees).
    """
    return {
        "placed_total": int(getattr(engine, "_placed", 0)),
        "filled_total": int(getattr(engine, "_filled", 0)),
        "recentered_total": int(getattr(engine, "_recentered", 0)),
        "blocked_by_risk": int(getattr(engine, "_blocked_by_risk", 0)),
        "paused_by_liquidity": int(getattr(engine, "_paused_by_liquidity", 0)),
        "paused_by_sentiment": int(getattr(engine, "_paused_by_sentiment", 0)),
    }


def _diff_counters(before: dict, after: dict) -> dict:
    """Per-symbol counter delta: after - before for every field."""
    out = {}
    for symbol, after_sym in after.items():
        before_sym = before.get(symbol, {})
        out[symbol] = {
            k: int(after_sym.get(k, 0)) - int(before_sym.get(k, 0))
            for k in after_sym.keys()
        }
    return out


def build_snapshot(
    *,
    env_mode: str,
    started_at: float,
    now: float,
    engines: dict,
    risk: Optional[RiskManager],
) -> dict:
    """
    Build a single structured snapshot of the bot's state.

    Separated from the monitor class so tests can call it directly with
    fake engines and a fake risk manager.
    """
    per_symbol = {sym: _engine_counters(eng) for sym, eng in engines.items()}
    risk_block: dict = {}
    if risk is not None:
        risk_block = {
            "halted": bool(risk.halted),
            "halt_reason": risk.halt_reason or None,
            "open_order_count": int(risk.open_order_count),
            "realized_pnl_24h": round(float(risk.realized_pnl_24h), 6),
            "daily_loss_limit_usd": float(risk._cfg.daily_loss_limit_usd),
        }
    return {
        "env_mode": env_mode,
        "started_at_ms": int(started_at * 1000),
        "heartbeat_at_ms": int(now * 1000),
        "uptime_seconds": round(now - started_at, 1),
        "utc_day": _utc_day(now),
        "per_symbol": per_symbol,
        "risk": risk_block,
    }


def write_status_atomic(path: str, snapshot: dict) -> None:
    """
    Write `snapshot` to `path` atomically.

    Writes to `path + .tmp` in the same directory, fsyncs, then os.replaces.
    External watchers tailing `path` either see the old file or the new
    file — never a half-written one. The same-directory tmp is important
    on Windows: os.replace across drives/dirs is not atomic.
    """
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile gives us a unique name in the right directory.
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the tmp file; swallow errors because we'll
        # re-raise the original.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class HeartbeatMonitor:
    """
    Async task. Emit a heartbeat every `config.heartbeat_interval_s`, and
    once per UTC-day rollover a DAILY SUMMARY line.

    USAGE (in grid_runner):
        monitor = HeartbeatMonitor(
            engines=engines, risk=risk, env_mode=env_mode,
            config=obs_cfg,
        )
        tasks.append(asyncio.create_task(monitor.run(stop_event),
                                         name="heartbeat"))
    """

    def __init__(
        self,
        *,
        engines: dict,
        risk: Optional[RiskManager],
        env_mode: str,
        config: ObservabilityConfig,
        now_fn=time.time,
    ):
        self._engines = engines
        self._risk = risk
        self._env_mode = env_mode
        self._cfg = config
        self._now = now_fn

        self._started_at: float = self._now()
        # Snapshot of counters taken at the start of the current UTC day.
        # On the first heartbeat we capture this; on a day rollover we diff
        # against it, then replace it with the fresh snapshot.
        self._day_start_snapshot: Optional[dict] = None
        self._day_start_utc: Optional[str] = None

    # ------------------------------------------------------------------
    # Public: the task body
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """
        Loop until stop_event. Emits the first heartbeat immediately so
        STATUS.json exists before anyone looks for it.
        """
        logger.info("heartbeat started",
                    interval_s=self._cfg.heartbeat_interval_s,
                    status_path=self._cfg.status_path,
                    env_mode=self._env_mode)

        # Initial heartbeat primes the day-start snapshot and writes STATUS.json.
        self.beat_once()

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._cfg.heartbeat_interval_s,
                )
                # stop_event fired during the wait → break cleanly.
                break
            except asyncio.TimeoutError:
                # Interval elapsed → emit.
                self.beat_once()

        logger.info("heartbeat stopping")

    # ------------------------------------------------------------------
    # One heartbeat cycle — public for direct tests.
    # ------------------------------------------------------------------

    def beat_once(self) -> dict:
        """
        Build a snapshot, write STATUS.json, log the heartbeat line, and
        emit a DAILY SUMMARY if we've rolled into a new UTC day.

        Returns the snapshot so tests can assert on it without reparsing
        the JSON file.
        """
        snapshot = build_snapshot(
            env_mode=self._env_mode,
            started_at=self._started_at,
            now=self._now(),
            engines=self._engines,
            risk=self._risk,
        )

        self._write_status_safe(snapshot)
        self._log_heartbeat(snapshot)

        if self._cfg.enable_daily_summary:
            self._maybe_emit_daily_summary(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_status_safe(self, snapshot: dict) -> None:
        try:
            write_status_atomic(self._cfg.status_path, snapshot)
        except OSError as e:
            # Disk full, permissions, path issue. Log and keep running —
            # the bot's health is more important than its status file.
            logger.warning("failed to write STATUS.json",
                           path=self._cfg.status_path, error=str(e))

    def _log_heartbeat(self, snapshot: dict) -> None:
        # Compact counters line so the default log stays readable.
        totals = {
            "placed": sum(s.get("placed_total", 0) for s in snapshot["per_symbol"].values()),
            "filled": sum(s.get("filled_total", 0) for s in snapshot["per_symbol"].values()),
            "recentered": sum(s.get("recentered_total", 0) for s in snapshot["per_symbol"].values()),
        }
        risk = snapshot.get("risk") or {}
        logger.info(
            "heartbeat",
            uptime_seconds=snapshot["uptime_seconds"],
            env_mode=snapshot["env_mode"],
            utc_day=snapshot["utc_day"],
            placed_total=totals["placed"],
            filled_total=totals["filled"],
            recentered_total=totals["recentered"],
            open_orders=risk.get("open_order_count"),
            realized_pnl_24h=risk.get("realized_pnl_24h"),
            halted=risk.get("halted"),
        )

    def _maybe_emit_daily_summary(self, snapshot: dict) -> None:
        current_day = snapshot["utc_day"]

        if self._day_start_snapshot is None:
            # First heartbeat ever — just seed the day start.
            self._day_start_snapshot = snapshot["per_symbol"]
            self._day_start_utc = current_day
            return

        if current_day == self._day_start_utc:
            # Same day, nothing to emit yet.
            return

        # UTC day rolled over. Emit the diff covering the PREVIOUS day,
        # then reset the day-start snapshot for the new day.
        delta = _diff_counters(self._day_start_snapshot, snapshot["per_symbol"])
        risk = snapshot.get("risk") or {}
        logger.info(
            "DAILY SUMMARY",
            utc_day_ended=self._day_start_utc,
            per_symbol=delta,
            realized_pnl_24h=risk.get("realized_pnl_24h"),
            open_orders=risk.get("open_order_count"),
        )
        self._day_start_snapshot = snapshot["per_symbol"]
        self._day_start_utc = current_day

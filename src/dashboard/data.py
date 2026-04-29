"""
data.py — pure read-only views over the bot's externally-visible state.

The dashboard renders three things:
  1. The latest STATUS.json snapshot the heartbeat task wrote.
  2. The last N lines of the log file.
  3. A "view" dict that bundles those plus a tiny amount of derived
     metadata (file mtime, "stale" flag, etc.) for the HTML template.

Every function here returns a useful default on failure (file missing,
file corrupt, file mid-write). The dashboard is a viewer — it should
never crash because the bot wrote something unexpected, and it must
keep serving even when the bot is offline (so the operator can SEE
that the bot is offline).

NO ASYNC HERE. File reads are fast. Wrapping in async would add
ceremony for zero benefit, and aiohttp.web is happy to call sync
functions from handlers.
"""

import datetime
import json
import os
import pathlib
import time
from typing import Optional

# Read STATUS.json in chunks reading from the end. The log file rotates
# at 50MB; reading line-by-line via the standard idiom would be wasteful
# under auto-refresh. Seek-from-end keeps each refresh to a few KB.
_TAIL_CHUNK_SIZE = 4096

# How long since the last STATUS.json write before we flag the snapshot
# as stale. Heartbeat default is 300s; we add ~2× headroom so a momentary
# slow-disk write doesn't trip the warning.
DEFAULT_STALE_AFTER_S: float = 660.0


def read_status(path: pathlib.Path) -> Optional[dict]:
    """
    Load and parse STATUS.json. Returns None if the file is missing,
    unreadable, or contains invalid JSON.

    The heartbeat task writes STATUS.json atomically (tmp + os.replace),
    so we never see a half-written file in normal operation. The defensive
    handling below covers genuinely-corrupt files (disk full mid-write,
    third-party tampering) and the file-absent case before the bot has
    booted once.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # OSError covers FileNotFoundError + PermissionError + concurrent
        # rename windows. ValueError is JSONDecodeError's parent.
        return None


def status_age_seconds(path: pathlib.Path, *, now: Optional[float] = None) -> Optional[float]:
    """
    How many seconds ago STATUS.json was last written, by mtime. Returns
    None if the file doesn't exist (the dashboard renders this as
    "no snapshot yet" rather than "0 seconds old").
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    current = now if now is not None else time.time()
    return max(0.0, current - mtime)


def tail_lines(path: pathlib.Path, n: int) -> list[str]:
    """
    Return the last N lines of the file as decoded strings, in order.

    Implemented with a backwards seek-and-read so the cost is bounded by
    output size, not file size — a 50 MB log file with N=50 reads only a
    few KB. Returns [] for missing files, empty files, or n <= 0.

    Decoding uses errors="replace" to keep mangled bytes from killing the
    page; in practice all our log lines are ASCII, but a binary-paste
    upstream shouldn't take down the dashboard.
    """
    if n <= 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return []
            offset = file_size
            chunks: list[bytes] = []
            newlines_seen = 0
            # Read backwards until we have at least n+1 newlines (the +1
            # guards against a trailing newline that splits to empty).
            while offset > 0 and newlines_seen <= n:
                read_size = min(_TAIL_CHUNK_SIZE, offset)
                offset -= read_size
                f.seek(offset)
                chunk = f.read(read_size)
                chunks.append(chunk)
                newlines_seen += chunk.count(b"\n")
            buf = b"".join(reversed(chunks))
    except OSError:
        return []
    text = buf.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:]


def build_view(
    *,
    status: Optional[dict],
    status_age_s: Optional[float],
    log_lines: list[str],
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    halt_path_exists: bool = False,
    halt_path: str = "runtime/HALT",
    now: Optional[float] = None,
) -> dict:
    """
    Compose a dict the HTML / JSON endpoints render from.

    Splitting view-construction out of the request handlers means the
    template can be a thin formatter and the JSON endpoint can return
    exactly the same shape — no two-source-of-truth bugs. Tests assert
    on view contents directly without spinning up a server.
    """
    rendered_at = now if now is not None else time.time()
    snapshot_present = status is not None
    is_stale = (
        status_age_s is not None and status_age_s > stale_after_s
    )
    rendered_dt = datetime.datetime.fromtimestamp(
        rendered_at, tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    risk = (status or {}).get("risk") or {}
    halted = bool(risk.get("halted"))
    return {
        "rendered_at_utc": rendered_dt,
        "snapshot_present": snapshot_present,
        "snapshot_age_seconds": (
            round(status_age_s, 1) if status_age_s is not None else None
        ),
        "snapshot_is_stale": is_stale,
        "stale_after_s": stale_after_s,
        "status": status or {},
        "halted": halted,
        "halt_sentinel_present": halt_path_exists,
        "halt_sentinel_path": halt_path,
        "log_lines": list(log_lines),
        "log_line_count": len(log_lines),
    }

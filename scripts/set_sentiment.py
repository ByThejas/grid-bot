"""
Operator CLI to update the sentiment sentinel file.

USAGE (venv activated, from repo root):
    # Set sentiment to -0.85 (will pause grid; threshold is -0.7 by default)
    python -m scripts.set_sentiment -- -0.85 "Coinbase exchange halt"

    # Reset to neutral (clears the file → grid auto-resumes)
    python -m scripts.set_sentiment --clear

    # Just show what's currently set
    python -m scripts.set_sentiment --show

WHY this script exists:
The bot reads `runtime/SENTIMENT` JSON and pauses placement if the score
drops below the configured threshold (default -0.7). For v1 there is no
NLP pipeline writing to this file — it's an operator override. This
script is the supported way to write it so the schema stays correct
(see FileSentimentSource for the schema).

A future Phase 8.5 / 9 will add an NLP adapter that writes the same
file, at which point this script remains as a manual override that
takes precedence by virtue of mtime.

Path comes from config/settings.yaml (signals.sentiment.sentiment_path),
falling back to runtime/SENTIMENT.
"""

import argparse
import json
import pathlib
import sys

import yaml

SETTINGS_PATH = "config/settings.yaml"


def _resolve_path() -> pathlib.Path:
    """Read the configured path from settings.yaml, with a sane default."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f) or {}
    except FileNotFoundError:
        settings = {}
    sub = (settings.get("signals", {}) or {}).get("sentiment", {}) or {}
    return pathlib.Path(sub.get("sentiment_path", "runtime/SENTIMENT"))


def _show(path: pathlib.Path) -> int:
    if not path.exists():
        print(f"[no sentiment set] file does not exist: {path}")
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[CORRUPT] {path}: {e}", file=sys.stderr)
        return 1
    print(f"path:   {path}")
    print(f"score:  {data.get('score')}")
    print(f"reason: {data.get('reason', '')}")
    return 0


def _clear(path: pathlib.Path) -> int:
    if path.exists():
        path.unlink()
        print(f"cleared: {path}")
    else:
        print(f"already clear: {path}")
    return 0


def _set(path: pathlib.Path, score: float, reason: str) -> int:
    if not -1.0 <= score <= 1.0:
        print(f"ERROR: score must be in [-1.0, +1.0]; got {score}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"score": score, "reason": reason}
    # Write atomically — tmp file + rename — so the bot never reads a
    # half-written file. The file source uses mtime as the timestamp so
    # rename also gives us a fresh mtime in one shot.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)
    print(f"wrote sentiment: score={score}, reason={reason!r} → {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Update grid bot sentiment sentinel")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="Print current sentiment and exit")
    group.add_argument("--clear", action="store_true", help="Remove the sentiment file (revert to neutral)")
    parser.add_argument("score", nargs="?", type=float, help="Score in [-1.0, +1.0]; below -0.7 pauses by default")
    parser.add_argument("reason", nargs="?", default="", help="Free-form reason (optional)")
    args = parser.parse_args()

    path = _resolve_path()
    if args.show:
        sys.exit(_show(path))
    if args.clear:
        sys.exit(_clear(path))
    if args.score is None:
        parser.error("provide a score (e.g. -0.85), or use --show / --clear")
    sys.exit(_set(path, args.score, args.reason))


if __name__ == "__main__":
    main()

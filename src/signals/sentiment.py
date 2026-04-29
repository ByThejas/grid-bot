"""
Sentiment Signal — third pillar of the signal stack.

WHY this signal exists:
Markets reprice on news faster than any order-book signal can react. A
hack, regulator announcement, exchange outage, or major liquidation
cascade flips the regime in seconds and turns a healthy mean-reverting
grid into a one-way ticket. By the time the order book "shows" it (via
spread widening or imbalance), the grid is already getting picked off.

So this signal lets the bot react to NEWS+SOCIAL sentiment ahead of the
book — but ONLY in the direction that protects capital. v1 is a pure
PAUSE switch: if sentiment is strongly negative, stop placing new
orders. We do NOT use sentiment to bias direction or sizing in v1 —
acting on positive sentiment is how grid bots blow up; acting on
strongly negative sentiment is just risk management.

================================================================
DESIGN
================================================================
Pluggable source. v1 ships a FILE-BASED source (`runtime/SENTIMENT`
JSON) that the operator updates manually OR a future NLP adapter
(CryptoPanic, Santiment, Reddit/Telegram bot) writes to. Engine code
never touches the source — it only sees a SentimentSignal it can
.compute() against.

Same sentinel pattern as the kill switch: file absent = no signal =
neutral. This means the bot defaults to "trade as normal" if the
sentiment pipeline hasn't been set up yet, which is the correct
fail-open behavior for a v1 (compare to risk halt which is fail-CLOSED).

The signal does NOT extend the Signal ABC — same reasoning as
LiquiditySignal. SignalResult+zones is the wrong output shape; the
engine consumes sentiment as a separate optional dependency.

This signal is INSTANCE PER PROCESS. The runner makes one and passes
the same instance to all engines. Sentiment is market-wide (BTC news
typically moves ETH too), so per-symbol granularity isn't worth the
complexity for v1.
"""

import json
import pathlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from src.notifications.alerter import Alerter, NullAlerter
from src.utils.logger import get_logger

logger = get_logger("sentiment")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SentimentConfig:
    """
    All tunables. `pause_threshold` is the only behavior-shaping knob in
    v1; the others are operational (where to read, how often).
    """
    # Pause placement when score < this. Default -0.7 matches
    # settings.yaml signals.sentiment_pause_threshold and represents
    # "strongly negative" — we don't pause on routine bad news, only on
    # genuine regime-change signals (hack, regulator action, major
    # cascade). Range expected: [-1.0, +1.0].
    pause_threshold: float = -0.7

    # Where the file-based source reads from. Same convention as the
    # kill switch (`runtime/HALT`) so the operator's mental model is
    # consistent: drop a file in `runtime/` to influence the bot.
    sentiment_path: str = "runtime/SENTIMENT"

    # Treat sentiment older than this as stale → revert to neutral.
    # Old news is no news. Default 30 minutes — most market-moving news
    # has played out by then. Set to 0 to disable staleness.
    max_age_seconds: float = 1800.0

    @classmethod
    def from_dict(cls, raw: Optional[dict], *, pause_threshold: Optional[float] = None) -> "SentimentConfig":
        """
        Build from settings.yaml. The pause_threshold lives at the top
        level of `signals:` (alongside the weights) for historical reasons,
        so the runner passes it in explicitly via `pause_threshold=`.
        Other knobs come from the `signals.sentiment:` sub-block.
        """
        raw = raw or {}
        return cls(
            pause_threshold=float(pause_threshold) if pause_threshold is not None
                            else float(raw.get("pause_threshold", -0.7)),
            sentiment_path=str(raw.get("sentiment_path", "runtime/SENTIMENT")),
            max_age_seconds=float(raw.get("max_age_seconds", 1800.0)),
        )


# ---------------------------------------------------------------------
# State (output)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SentimentState:
    """
    What the engine consumes. `score` is in [-1.0, +1.0], 0 = neutral.
    `should_pause` is the only thing the engine acts on in v1.
    """
    score: float
    reason: str          # human-readable, surfaced in logs for postmortem
    source: str          # which source produced it (e.g. "file", "neutral", "stale")
    should_pause: bool


# A dedicated neutral state — fail-open default when no source has data.
NEUTRAL = SentimentState(score=0.0, reason="no signal", source="neutral", should_pause=False)


# ---------------------------------------------------------------------
# Source interface
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SourceReading:
    """One reading from a source. `timestamp` is unix seconds; if the
    source can't supply one, it should pass `time.time()` itself."""
    score: float
    reason: str
    timestamp: float


class SentimentSource(ABC):
    """
    Pluggable source. Implementations: FileSentimentSource (v1), and
    future NLP adapters (CryptoPanic, Santiment, social bot, RSS).

    Returns None if no signal is currently available — the signal then
    falls back to NEUTRAL. Source must NOT raise for missing data; only
    raise for genuinely broken state (corrupt file, unreachable API
    after retries) so the engine can decide how to treat it.
    """

    name: str = "abstract"

    @abstractmethod
    def read(self) -> Optional[SourceReading]:
        ...


class FileSentimentSource(SentimentSource):
    """
    Reads `runtime/SENTIMENT` JSON. Schema:
        {"score": -0.85, "reason": "Coinbase exchange halt"}
    `score` is required and must be in [-1.0, +1.0]; `reason` defaults
    to empty string. The file's mtime is the timestamp — operator
    doesn't have to bother with one.

    Returns None if file is absent (the common case). Returns None and
    logs a warning if the file is malformed — we don't want a typo in
    the sentiment file to halt the bot or, worse, to read as a stale
    score from a previous edit.
    """

    name = "file"

    def __init__(self, path: str):
        self._path = pathlib.Path(path)

    def read(self) -> Optional[SourceReading]:
        if not self._path.exists():
            return None
        try:
            stat = self._path.stat()
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            score = float(data["score"])
            if not -1.0 <= score <= 1.0:
                logger.warning("sentiment file score out of range — ignoring",
                               path=str(self._path), score=score)
                return None
            reason = str(data.get("reason", ""))
            return SourceReading(score=score, reason=reason, timestamp=stat.st_mtime)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Don't crash the engine over a bad file — just ignore it.
            logger.warning("sentiment file unreadable — ignoring",
                           path=str(self._path), error=str(e))
            return None


# ---------------------------------------------------------------------
# Pure-function math (testable without I/O)
# ---------------------------------------------------------------------


def evaluate_sentiment(
    reading: Optional[SourceReading],
    config: SentimentConfig,
    source_name: str,
    *,
    now: Optional[float] = None,
) -> SentimentState:
    """
    Convert a raw source reading + config into the SentimentState the
    engine consumes. Encapsulates the staleness check and the pause
    decision so tests can hit it directly.

    Why split this out: SentimentSignal.compute() does I/O (reads the
    file). This function does math. Testing the math against synthetic
    readings is far cheaper than spinning up real files for every case.
    """
    if reading is None:
        return NEUTRAL

    if config.max_age_seconds > 0:
        current = now if now is not None else time.time()
        age = current - reading.timestamp
        if age > config.max_age_seconds:
            return SentimentState(
                score=0.0,
                reason=f"stale ({age:.0f}s old, max {config.max_age_seconds:.0f}s)",
                source="stale",
                should_pause=False,
            )

    should_pause = reading.score < config.pause_threshold
    return SentimentState(
        score=reading.score,
        reason=reading.reason,
        source=source_name,
        should_pause=should_pause,
    )


# ---------------------------------------------------------------------
# Class wrapper
# ---------------------------------------------------------------------


class SentimentSignal:
    """
    USAGE:
        source = FileSentimentSource("runtime/SENTIMENT")
        signal = SentimentSignal(source, SentimentConfig.from_dict(...))
        state = signal.compute()
        if state.should_pause:
            ...

    Synchronous compute() — file reads are fast and the engine already
    runs this on each placement attempt, not in a hot path. Going async
    would add complexity for no real benefit at v1.
    """

    def __init__(
        self,
        source: SentimentSource,
        config: SentimentConfig,
        alerter: Optional[Alerter] = None,
    ):
        self._source = source
        self._config = config
        self._alerter: Alerter = alerter or NullAlerter()
        # Track last reported state to suppress repetitive "neutral" logs.
        # Operationally important — without this, every grid placement
        # would emit a sentiment log line, drowning real signal.
        self._last_logged_pause: Optional[bool] = None

    def compute(self) -> SentimentState:
        try:
            reading = self._source.read()
        except Exception as e:
            # Source contract is "return None for missing"; an exception
            # means something is genuinely broken. Fail open (neutral)
            # but log loudly — operator needs to see this.
            logger.error("sentiment source raised — defaulting to neutral",
                         source=self._source.name, error=str(e))
            reading = None

        state = evaluate_sentiment(reading, self._config, self._source.name)

        # Log on every state transition (pause-state change or score change
        # for non-neutral readings). Quiet on steady neutral.
        prev = self._last_logged_pause
        if state.should_pause != prev or state.source != "neutral":
            logger.info("sentiment",
                        score=round(state.score, 3),
                        source=state.source,
                        should_pause=state.should_pause,
                        reason=state.reason)
            self._last_logged_pause = state.should_pause

        # Edge-trigger pause/resume alerts on transition. Sentiment is
        # market-wide (BTC news moves ETH); we pass symbol="*" so the
        # operator can tell the alert apart from a per-symbol liquidity one.
        # First reading is seeded silently (prev is None) so a routine boot
        # doesn't fire a [RESUME].
        if prev is not None and state.should_pause != prev:
            score_str = f"{state.score:+.2f}"
            threshold_str = f"{self._config.pause_threshold:+.2f}"
            if state.should_pause:
                self._alerter.pause(
                    source="sentiment", symbol="*",
                    score=score_str, threshold=threshold_str,
                    reason=state.reason or "negative",
                )
            else:
                self._alerter.resume(
                    source="sentiment", symbol="*", score=score_str,
                )

        return state

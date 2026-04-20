"""
Tests for SentimentSignal.

WHY these tests matter:
The sentiment signal is the only one that can pause the bot from
EXTERNAL input (not from market state). A bug here either (a) fails
to pause when news warrants it — capital risk, or (b) pauses on
every neutral tick — operationally useless. We lock down both ends
plus the staleness window and bad-input handling.

All tests use the pure `evaluate_sentiment` function or in-memory
sources — no real files needed except the FileSentimentSource tests
which use tmp_path. Run as `python -m tests.test_sentiment` AND
under pytest.
"""

import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

from src.signals.sentiment import (
    NEUTRAL,
    FileSentimentSource,
    SentimentConfig,
    SentimentSignal,
    SentimentSource,
    SourceReading,
    evaluate_sentiment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(
    *,
    pause_threshold: float = -0.7,
    max_age_seconds: float = 1800.0,
) -> SentimentConfig:
    return SentimentConfig(
        pause_threshold=pause_threshold,
        sentiment_path="runtime/SENTIMENT",
        max_age_seconds=max_age_seconds,
    )


class _StubSource(SentimentSource):
    """In-memory source for testing the SentimentSignal wrapper without
    touching the filesystem."""
    name = "stub"

    def __init__(self, reading: Optional[SourceReading] = None, raises: bool = False):
        self._reading = reading
        self._raises = raises

    def read(self) -> Optional[SourceReading]:
        if self._raises:
            raise RuntimeError("simulated source failure")
        return self._reading


# ---------------------------------------------------------------------------
# evaluate_sentiment — pause logic
# ---------------------------------------------------------------------------


def test_evaluate_no_reading_returns_neutral():
    state = evaluate_sentiment(None, _cfg(), source_name="file")
    assert state is NEUTRAL
    assert state.should_pause is False
    assert state.score == 0.0


def test_evaluate_pauses_when_score_below_threshold():
    reading = SourceReading(score=-0.85, reason="exchange halt", timestamp=time.time())
    state = evaluate_sentiment(reading, _cfg(pause_threshold=-0.7), source_name="file")
    assert state.should_pause is True
    assert state.score == -0.85
    assert state.reason == "exchange halt"
    assert state.source == "file"


def test_evaluate_does_not_pause_at_threshold_boundary():
    # Strictly less-than → exactly at threshold should NOT pause.
    reading = SourceReading(score=-0.7, reason="", timestamp=time.time())
    state = evaluate_sentiment(reading, _cfg(pause_threshold=-0.7), source_name="file")
    assert state.should_pause is False


def test_evaluate_does_not_pause_just_above_threshold():
    reading = SourceReading(score=-0.69, reason="", timestamp=time.time())
    state = evaluate_sentiment(reading, _cfg(pause_threshold=-0.7), source_name="file")
    assert state.should_pause is False


def test_evaluate_does_not_pause_on_positive_sentiment():
    # Critical: positive sentiment must NEVER pause. v1 only acts
    # defensively — going long on euphoria is how grid bots blow up.
    reading = SourceReading(score=0.95, reason="ETF approval", timestamp=time.time())
    state = evaluate_sentiment(reading, _cfg(), source_name="file")
    assert state.should_pause is False


# ---------------------------------------------------------------------------
# evaluate_sentiment — staleness
# ---------------------------------------------------------------------------


def test_evaluate_treats_old_reading_as_stale():
    # Reading 2 hours old, max_age 30 min — should drop to neutral.
    now = 10_000.0
    reading = SourceReading(score=-0.9, reason="old news", timestamp=now - 7200)
    state = evaluate_sentiment(reading, _cfg(max_age_seconds=1800), source_name="file", now=now)
    assert state.should_pause is False
    assert state.source == "stale"
    assert state.score == 0.0


def test_evaluate_keeps_fresh_reading():
    # Reading 5 minutes old, max_age 30 min — should still pause.
    now = 10_000.0
    reading = SourceReading(score=-0.9, reason="recent", timestamp=now - 300)
    state = evaluate_sentiment(reading, _cfg(max_age_seconds=1800), source_name="file", now=now)
    assert state.should_pause is True


def test_evaluate_disabled_staleness_keeps_old_reading():
    # max_age_seconds=0 disables the check — old readings still count.
    now = 10_000.0
    reading = SourceReading(score=-0.9, reason="ancient", timestamp=now - 999_999)
    state = evaluate_sentiment(reading, _cfg(max_age_seconds=0), source_name="file", now=now)
    assert state.should_pause is True


# ---------------------------------------------------------------------------
# FileSentimentSource — file I/O
# ---------------------------------------------------------------------------


def test_file_source_returns_none_when_file_absent():
    with tempfile.TemporaryDirectory() as tmp:
        src = FileSentimentSource(str(Path(tmp) / "SENTIMENT"))
        assert src.read() is None


def test_file_source_reads_valid_json():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "SENTIMENT"
        path.write_text(json.dumps({"score": -0.85, "reason": "Coinbase halt"}), encoding="utf-8")
        src = FileSentimentSource(str(path))
        reading = src.read()
        assert reading is not None
        assert reading.score == -0.85
        assert reading.reason == "Coinbase halt"
        # mtime should be recent.
        assert abs(reading.timestamp - time.time()) < 5


def test_file_source_defaults_reason_to_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "SENTIMENT"
        path.write_text(json.dumps({"score": 0.5}), encoding="utf-8")
        reading = FileSentimentSource(str(path)).read()
        assert reading is not None
        assert reading.reason == ""


def test_file_source_rejects_out_of_range_score():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "SENTIMENT"
        path.write_text(json.dumps({"score": -2.5, "reason": "wrong"}), encoding="utf-8")
        # Out-of-range → None, not a partial reading. Otherwise a typo
        # could halt the bot indefinitely.
        assert FileSentimentSource(str(path)).read() is None


def test_file_source_handles_malformed_json():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "SENTIMENT"
        path.write_text("{not valid json", encoding="utf-8")
        assert FileSentimentSource(str(path)).read() is None


def test_file_source_handles_missing_score_key():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "SENTIMENT"
        path.write_text(json.dumps({"reason": "no score"}), encoding="utf-8")
        assert FileSentimentSource(str(path)).read() is None


# ---------------------------------------------------------------------------
# SentimentSignal — wrapper class
# ---------------------------------------------------------------------------


def test_signal_returns_neutral_when_source_returns_none():
    sig = SentimentSignal(_StubSource(reading=None), _cfg())
    state = sig.compute()
    assert state.should_pause is False
    assert state.source == "neutral"


def test_signal_pauses_when_source_returns_negative_reading():
    reading = SourceReading(score=-0.9, reason="hack", timestamp=time.time())
    sig = SentimentSignal(_StubSource(reading=reading), _cfg(pause_threshold=-0.7))
    state = sig.compute()
    assert state.should_pause is True
    assert state.score == -0.9
    assert state.source == "stub"


def test_signal_fails_open_when_source_raises():
    # Source bug shouldn't halt the bot. Fail open to neutral and log.
    sig = SentimentSignal(_StubSource(raises=True), _cfg())
    state = sig.compute()
    assert state.should_pause is False
    assert state.source == "neutral"


# ---------------------------------------------------------------------------
# SentimentConfig.from_dict
# ---------------------------------------------------------------------------


def test_config_from_dict_uses_explicit_pause_threshold():
    # The runner passes pause_threshold from signals.sentiment_pause_threshold
    # explicitly; the sub-block doesn't have to repeat it.
    cfg = SentimentConfig.from_dict({"max_age_seconds": 600.0}, pause_threshold=-0.5)
    assert cfg.pause_threshold == -0.5
    assert cfg.max_age_seconds == 600.0


def test_config_from_dict_falls_back_to_defaults_for_missing_keys():
    cfg = SentimentConfig.from_dict(None)
    assert cfg.pause_threshold == -0.7
    assert cfg.sentiment_path == "runtime/SENTIMENT"
    assert cfg.max_age_seconds == 1800.0


# ---------------------------------------------------------------------------
# Standalone runner + pytest discovery
# ---------------------------------------------------------------------------


_TESTS = [
    test_evaluate_no_reading_returns_neutral,
    test_evaluate_pauses_when_score_below_threshold,
    test_evaluate_does_not_pause_at_threshold_boundary,
    test_evaluate_does_not_pause_just_above_threshold,
    test_evaluate_does_not_pause_on_positive_sentiment,
    test_evaluate_treats_old_reading_as_stale,
    test_evaluate_keeps_fresh_reading,
    test_evaluate_disabled_staleness_keeps_old_reading,
    test_file_source_returns_none_when_file_absent,
    test_file_source_reads_valid_json,
    test_file_source_defaults_reason_to_empty,
    test_file_source_rejects_out_of_range_score,
    test_file_source_handles_malformed_json,
    test_file_source_handles_missing_score_key,
    test_signal_returns_neutral_when_source_returns_none,
    test_signal_pauses_when_source_returns_negative_reading,
    test_signal_fails_open_when_source_raises,
    test_config_from_dict_uses_explicit_pause_threshold,
    test_config_from_dict_falls_back_to_defaults_for_missing_keys,
]


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

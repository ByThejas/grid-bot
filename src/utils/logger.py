"""
Structured logging setup for the grid bot.

WHY structured logging?
- Plain text logs ("ERROR: something broke") are human-readable but hard to search.
- Structured logs (JSON) let you filter by fields in production:
    {"level": "error", "component": "order_manager", "symbol": "BTCUSDT", "msg": "order rejected"}
  You can query: "show me all errors from order_manager for BTCUSDT" — impossible with plain text.

USAGE in any module:
    from src.utils.logger import get_logger
    logger = get_logger("my_component")
    logger.info("grid placed", symbol="BTCUSDT", levels=10)

STARTUP CONTRACT:
- The runner (or any other top-level entrypoint) must call `configure_logging(settings)`
  once, near the top, BEFORE any log line is emitted. Module-level `logger = get_logger(...)`
  is fine to call at import time — the binding is lazy and picks up whichever config is
  in place when the first `.info()` runs.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_LOG_FILE_BYTES = 50 * 1024 * 1024  # 50 MB before rotation
_LOG_FILE_BACKUPS = 5                # keep .1 through .5

_configured = False


def configure_logging(settings: dict) -> None:
    """
    Wire stdlib logging + structlog from `settings.logging`. Idempotent — safe
    to call repeatedly; it clears handlers each time.

    settings.logging keys (all optional with defaults):
      level:   "DEBUG" | "INFO" | "WARNING" | "ERROR"   (default INFO)
      file:    "logs/grid_bot.log"                       (default None → file disabled)
      format:  "json" | "console"                        (only affects file format;
                                                          stderr is always console-rendered
                                                          for human eyes)
    """
    global _configured

    log_cfg = (settings or {}).get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = log_cfg.get("file")
    file_format = str(log_cfg.get("format", "json")).lower()

    # Processors that run on EVERY log event before per-handler formatting.
    # ProcessorFormatter does the per-handler render, so anything universal
    # (timestamp, level name, contextvars merge) goes here.
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stderr always gets the pretty console render so the live cmd window stays
    # readable. Disable colour because Windows cmd doesn't render ANSI cleanly
    # without extra setup.
    # remove_processors_meta strips _record and _from_structlog (added by
    # wrap_for_formatter as a stdlib bridge) so the renderer doesn't dump
    # the whole LogRecord into the output.
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        foreign_pre_chain=shared_processors,
    )
    file_renderer = (
        structlog.processors.JSONRenderer()
        if file_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            file_renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    # Clear any existing handlers — we own this. Avoids duplicate lines if
    # configure_logging is called twice or if a library set up logging earlier.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(console_formatter)
    root.addHandler(stderr_handler)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=_LOG_FILE_BYTES,
            backupCount=_LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a structlog logger bound to a component name. Safe to call at
    module-import time — the underlying chain is bound lazily on first log call,
    so whatever `configure_logging()` set up by then is what gets used.
    """
    return structlog.get_logger(component=name)

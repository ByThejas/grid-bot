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
"""

import structlog


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Create a logger bound to a component name.

    Args:
        name: The component name (e.g., "grid_engine", "risk_manager")

    Returns:
        A structured logger instance you can call .info(), .error(), etc. on
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()  # Pretty output in dev
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(component=name)

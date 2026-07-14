"""Structured JSON logging via structlog.

Per convention (repo CLAUDE.md §5) all logs are structured JSON with per-stage context.
Callers never log raw query text at info level and never log secrets — that discipline
lives at the call sites; this module only wires up the renderer.
"""

from __future__ import annotations

import logging
from typing import cast

import structlog
from structlog.typing import FilteringBoundLogger

_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit JSON to stderr. Idempotent."""
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=log_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a bound structlog logger."""
    return cast("FilteringBoundLogger", structlog.get_logger(name))

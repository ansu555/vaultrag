"""Structured JSON logging via structlog.

Per convention (repo CLAUDE.md §5) all logs are structured JSON with per-stage context.
Callers never log raw query text at info level and never log secrets — that discipline
lives at the call sites; this module only wires up the renderer.

Every log line goes to **stderr**, never stdout: when ``vaultrag mcp`` serves over stdio
(Step 10), stdout *is* the JSON-RPC protocol channel, and a single log line written there
corrupts the session.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import structlog
from structlog.typing import FilteringBoundLogger

_configured = False


class _StderrProxy:
    """File-like object that resolves ``sys.stderr`` at *write* time.

    Passing ``sys.stderr`` itself to ``PrintLoggerFactory`` would freeze whatever
    stream is current when ``configure_logging`` first runs — under pytest/click
    output capture that is a per-test stream that gets closed, and ``configure_logging``
    is deliberately idempotent, so every later log write would hit a closed file.
    Deferring the lookup mirrors ``PrintLogger``'s own call-time ``sys.stdout``
    resolution, just for stderr.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit JSON to stderr. Idempotent."""
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=log_level)  # stdlib default: stderr

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
        logger_factory=structlog.PrintLoggerFactory(file=cast("TextIO", _StderrProxy())),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a bound structlog logger."""
    return cast("FilteringBoundLogger", structlog.get_logger(name))

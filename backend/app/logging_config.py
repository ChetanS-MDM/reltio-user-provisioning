"""Structured logging with per-request correlation IDs.

Every log line includes a correlation ID so that all messages belonging to a
single API Gateway / Lambda invocation can be traced end-to-end.  The ID is
read from the ``x-amzn-requestid`` header when available; otherwise a random
UUID4 is generated in the middleware (see main.py).
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


class CorrelationFilter(logging.Filter):
    """Inject ``record.correlation_id`` from the current context."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()  # type: ignore[attr-defined]
        return True


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure the root logger with a JSON-ish format that includes the
    correlation ID and apply the :class:`CorrelationFilter`."""
    fmt = "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s %(message)s"
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(CorrelationFilter())

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on repeated calls (e.g. tests).
    root.handlers.clear()
    root.addHandler(handler)

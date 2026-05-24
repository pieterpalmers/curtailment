"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import structlog

logger = logging.getLogger("curtailment")


def setup_logging(level: str = "INFO", fmt: str = "json", log_file: str | None = None) -> None:
    """Configure structlog for structured JSON or console output."""
    try:
        import structlog

        _setup_structlog(level, fmt, log_file)
    except ImportError:
        _setup_standard_logging(level, log_file)


def _setup_structlog(level: str, fmt: str, log_file: str | None) -> None:
    import structlog

    if fmt == "json":
        renderer: structlog.dev.ConsoleRenderer | structlog.dev.JsonRenderer = structlog.dev.JsonRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.set_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Direct stdlib logging to structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
    )
    handler = logging.FileHandler(log_file) if log_file else logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def _setup_standard_logging(level: str, log_file: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.FileHandler(log_file) if log_file else logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

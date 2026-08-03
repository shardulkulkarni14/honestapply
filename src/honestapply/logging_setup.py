"""structlog configuration: pretty to console, JSON to a rotating log file."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog

from honestapply.config import PATHS

_configured = False


def configure_logging(level: int = logging.INFO, log_file: Path | None = None) -> None:
    global _configured
    if _configured:
        return

    log_file = log_file or (PATHS.data_dir / "honestapply.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # Console: pretty. File: JSON. Use a stdlib handler for the JSON file.
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=True),
            foreign_pre_chain=shared,
        )
    )

    root = logging.getLogger()
    root.handlers = [file_handler, console_handler]
    root.setLevel(level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "honestapply") -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)

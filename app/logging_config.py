"""
Structured logging configuration using structlog.
Produces JSON lines for machine parsing and pretty console output for dev.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(log_dir: Path, json_logs: bool = True) -> None:
    """
    Configure structlog + stdlib logging.

    Parameters
    ----------
    log_dir : Path
        Directory for log files.
    json_logs : bool
        If True, also write JSON-lines to ``log_dir/app.jsonl``.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── stdlib root logger ──────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console handler (human-readable)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    root.addHandler(console)

    # File handler (JSON lines)
    if json_logs:
        fh = logging.FileHandler(log_dir / "app.jsonl", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)

    # ── structlog pipeline ──────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

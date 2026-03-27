"""
Application logging setup helpers.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LoggingConfig


def configure_capture_pipeline_logging(config: LoggingConfig) -> Path:
    """
    Configure root logging with console and rotating file handlers.

    Returns the absolute path to the log file.
    """
    directory = Path(config.log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = (directory / config.file_name).resolve()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(config.resolved_level())

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    return log_path

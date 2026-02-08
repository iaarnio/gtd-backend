"""
Structured logging configuration for Loki integration.

This module provides JSON logging output suitable for ingestion by Promtail/Loki,
with structured context fields for filtering and alerting.
"""

import json
import logging
import sys
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """
    Custom formatter that outputs logs as JSON with structured fields.

    Standard fields: timestamp, level, logger, message
    Context fields (if present): capture_id, error_type, operation, external_service, http_status, retry_count
    Exception handling: stack trace included when exc_info=True
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as JSON."""
        log_data: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt or "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra context fields if present
        if hasattr(record, "component"):
            log_data["component"] = record.component
        if hasattr(record, "operation"):
            log_data["operation"] = record.operation
        if hasattr(record, "capture_id"):
            log_data["capture_id"] = record.capture_id
        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type
        if hasattr(record, "external_service"):
            log_data["external_service"] = record.external_service
        if hasattr(record, "http_status"):
            log_data["http_status"] = record.http_status
        if hasattr(record, "retry_count"):
            log_data["retry_count"] = record.retry_count
        if hasattr(record, "attempt"):
            log_data["attempt"] = record.attempt

        # Add exception info if present
        if record.exc_info and record.exc_text:
            log_data["exception"] = record.exc_text

        return json.dumps(log_data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """
    Traditional text formatter for development/local logging.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as human-readable text."""
        msg = super().format(record)

        # Append extra context if present
        extra_parts = []
        for attr in ["component", "operation", "capture_id", "error_type", "external_service",
                     "http_status", "retry_count", "attempt"]:
            if hasattr(record, attr):
                extra_parts.append(f"{attr}={getattr(record, attr)}")

        if extra_parts:
            msg += " | " + " ".join(extra_parts)

        return msg


def configure_logging(json_logs: bool = True, log_level: str = "INFO") -> None:
    """
    Configure the root logger with appropriate formatting and level.

    Args:
        json_logs: If True, use JSON formatter (for Loki). If False, use text formatter (for local dev).
        log_level: Log level as string ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicate logs
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Set formatter based on preference
    if json_logs:
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.

    This is a convenience function that ensures consistent logger naming.
    """
    return logging.getLogger(name)

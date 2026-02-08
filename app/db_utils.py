"""
Database utilities for resilient transaction management.

This module provides:
- transactional_session: Context manager for SQLite BUSY-safe transactions
- Automatic rollback on failure, retry on BUSY errors
"""

import logging
import time
from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@contextmanager
def transactional_session(
    session: Session,
    max_retries: int = 5,
) -> Generator[None, None, None]:
    """
    Context manager for database transactions with automatic rollback and SQLite BUSY retry.

    Handles:
    - Automatic commit on successful completion
    - Automatic rollback on exception
    - SQLite BUSY retry with exponential backoff
    - Structured logging of all database operations

    Usage:
        with transactional_session(db):
            db.add(capture)
            # Automatic commit on exit, rollback on exception

    Args:
        session: SQLAlchemy session
        max_retries: Maximum retry attempts for SQLite BUSY errors

    Yields:
        None

    Raises:
        Any exception from within the context block
    """
    attempt = 0
    last_exception = None

    while attempt <= max_retries:
        try:
            yield
            # Commit on successful completion
            session.commit()
            logger.debug(
                "Database transaction committed",
                extra={"component": "database", "operation": "commit"},
            )
            return

        except Exception as e:
            # Rollback on any exception
            session.rollback()
            error_msg = str(e).lower()
            attempt += 1
            last_exception = e

            # Check if this is a SQLite BUSY error (database is locked)
            is_busy_error = (
                "database is locked" in error_msg
                or "busy" in error_msg
                or "unable to open database file" in error_msg
            )

            if is_busy_error and attempt <= max_retries:
                # Calculate exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
                delay = 0.1 * (2 ** (attempt - 1))
                logger.warning(
                    f"Database locked, retrying in {delay:.2f}s (attempt {attempt}/{max_retries})",
                    extra={
                        "component": "database",
                        "error_type": "db_locked",
                        "attempt": attempt,
                        "retry_count": max_retries,
                    },
                )
                time.sleep(delay)
                # Continue to next attempt
                continue
            else:
                # Non-retryable error or max retries exceeded
                if is_busy_error:
                    logger.error(
                        f"Database locked after {max_retries} retries",
                        extra={
                            "component": "database",
                            "error_type": "db_locked",
                            "attempt": attempt,
                            "retry_count": max_retries,
                        },
                        exc_info=True,
                    )
                else:
                    logger.error(
                        "Database transaction failed",
                        extra={
                            "component": "database",
                            "error_type": type(e).__name__,
                        },
                        exc_info=True,
                    )
                # Re-raise the exception
                raise

    # Should not reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Unknown error in transaction context")

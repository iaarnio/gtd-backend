"""
Daily highlights scheduler.

Runs the daily highlights job at configured time (default: 23:00 UTC).
Calculates next run time and sleeps until then.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from . import daily_highlights
from .config import Config
from .db import get_db
from .logging_config import get_logger

logger = get_logger(__name__)


def _get_next_run_time(hour: int, minute: int) -> datetime:
    """
    Calculate when the next daily run should occur.

    Args:
        hour: Hour (0-23) in UTC
        minute: Minute (0-59) in UTC

    Returns:
        datetime when next run should occur
    """
    now = datetime.utcnow()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If the time has already passed today, schedule for tomorrow
    if next_run <= now:
        next_run += timedelta(days=1)

    return next_run


def _sleep_until_next_run(hour: int, minute: int) -> None:
    """
    Sleep until the next scheduled run time.
    Checks every minute to avoid missing the scheduled time.
    """
    while True:
        next_run = _get_next_run_time(hour, minute)
        sleep_seconds = (next_run - datetime.utcnow()).total_seconds()

        if sleep_seconds <= 0:
            # Time to run
            break

        # Sleep for up to 1 minute or until next run (whichever is sooner)
        sleep_time = min(sleep_seconds, 60)
        time.sleep(sleep_time)


def run_highlights_scheduler() -> None:
    """
    Main scheduler loop.

    Runs at configured time (hour:minute UTC).
    Sleeps between runs.
    """
    from .db import engine

    hour = Config.HIGHLIGHTS_RUN_HOUR
    minute = Config.HIGHLIGHTS_RUN_MINUTE

    logger.info(
        f"Daily highlights scheduler started (runs at {hour:02d}:{minute:02d} UTC)",
        extra={"component": "highlights_scheduler", "operation": "start"},
    )

    while True:
        try:
            # Sleep until next run time
            _sleep_until_next_run(hour, minute)

            # Create new session for this run
            SessionLocal = Session(bind=engine)
            try:
                logger.info(
                    f"Running daily highlights at {datetime.utcnow().isoformat()}",
                    extra={"component": "highlights_scheduler", "operation": "run_job"},
                )

                result = daily_highlights.run_daily_highlights(SessionLocal)

                logger.info(
                    f"Daily highlights job completed: {result}",
                    extra={
                        "component": "highlights_scheduler",
                        "operation": "job_complete",
                        "result": result,
                    },
                )

            finally:
                SessionLocal.close()

        except Exception as e:
            logger.error(
                f"Error in highlights scheduler: {e}",
                extra={
                    "component": "highlights_scheduler",
                    "operation": "scheduler_error",
                    "error_type": "exception",
                },
                exc_info=True,
            )
            # Sleep for a minute before retrying
            time.sleep(60)


def start_scheduler() -> threading.Thread:
    """
    Start the daily highlights scheduler in a background thread.

    Returns:
        Thread object (daemon thread)
    """
    thread = threading.Thread(target=run_highlights_scheduler, daemon=True)
    thread.start()
    return thread

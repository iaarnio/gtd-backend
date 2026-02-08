"""
Daily background job scheduler.

Runs scheduled jobs at configured times:
- Daily highlights (02:00 UTC)
- Nightly backlog drain (03:00 UTC)

Calculates next run times and sleeps until them.
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


def run_background_scheduler() -> None:
    """
    Main scheduler loop.

    Checks every minute for scheduled jobs:
    - Highlights at HIGHLIGHTS_RUN_HOUR:HIGHLIGHTS_RUN_MINUTE
    - Backlog drain at HIGHLIGHTS_RUN_HOUR+1:00 (one hour after highlights)
    """
    from .db import engine
    from . import backlog_processor

    highlights_hour = Config.HIGHLIGHTS_RUN_HOUR
    highlights_minute = Config.HIGHLIGHTS_RUN_MINUTE
    backlog_hour = (highlights_hour + 1) % 24  # Next hour (wrap around at 23→00)
    backlog_minute = 0

    logger.info(
        f"Background scheduler started "
        f"(highlights at {highlights_hour:02d}:{highlights_minute:02d} UTC, "
        f"backlog drain at {backlog_hour:02d}:{backlog_minute:02d} UTC)",
        extra={"component": "scheduler", "operation": "start"},
    )

    last_highlights_run = None
    last_backlog_run = None

    while True:
        try:
            now = datetime.utcnow()
            today = now.date()

            # Check if it's time to run highlights
            if (
                now.hour == highlights_hour
                and now.minute == highlights_minute
                and last_highlights_run != today
            ):
                SessionLocal = Session(bind=engine)
                try:
                    logger.info(
                        f"Running daily highlights at {now.isoformat()}",
                        extra={"component": "scheduler", "job": "highlights"},
                    )
                    result = daily_highlights.run_daily_highlights(SessionLocal)
                    logger.info(
                        f"Highlights job completed: {result}",
                        extra={"component": "scheduler", "job": "highlights", "result": result},
                    )
                    last_highlights_run = today
                finally:
                    SessionLocal.close()

            # Check if it's time to run backlog drain
            if (
                now.hour == backlog_hour
                and now.minute == backlog_minute
                and last_backlog_run != today
            ):
                SessionLocal = Session(bind=engine)
                try:
                    logger.info(
                        f"Running backlog drain at {now.isoformat()}",
                        extra={"component": "scheduler", "job": "backlog"},
                    )
                    result = backlog_processor.nightly_backlog_drain(SessionLocal)
                    logger.info(
                        f"Backlog drain completed: {result}",
                        extra={"component": "scheduler", "job": "backlog", "result": result},
                    )
                    last_backlog_run = today
                finally:
                    SessionLocal.close()

            # Sleep for a minute before checking again
            time.sleep(60)

        except Exception as e:
            logger.error(
                f"Error in background scheduler: {e}",
                extra={
                    "component": "scheduler",
                    "operation": "scheduler_error",
                    "error_type": "exception",
                },
                exc_info=True,
            )
            # Sleep for a minute before retrying
            time.sleep(60)


def start_scheduler() -> threading.Thread:
    """
    Start the background scheduler in a background thread.

    Runs: highlights job and backlog drain job

    Returns:
        Thread object (daemon thread)
    """
    thread = threading.Thread(target=run_background_scheduler, daemon=True)
    thread.start()
    return thread

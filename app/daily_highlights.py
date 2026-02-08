"""
Daily highlights system for RTM.

Manages two labels:
- #highlight: User-owned, manual
- #highlight-today: System-owned, transient (cleared daily)

Selects 5 lonely actions (no project, no #na tag, not completed) to highlight each day.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from . import models
from .config import Config
from .db_utils import transactional_session
from .logging_config import get_logger
from .rtm import call as rtm_call

logger = get_logger(__name__)

# RTM labels (from config)
HIGHLIGHT_LABEL = Config.HIGHLIGHT_LABEL
SYSTEM_LABEL = Config.SYSTEM_LABEL

# Selection
BAND_A_LIMIT = 5  # Older tasks (14+ days)
BAND_B_LIMIT = 5  # Recent tasks (7- days)
MAX_CANDIDATES = BAND_A_LIMIT + BAND_B_LIMIT
FINAL_SELECT = 5

# Anti-nag rule: exclude tasks suggested 3+ times in last 14 days
MAX_SUGGESTIONS_14D = 3
SUGGESTION_WINDOW_DAYS = 14


def run_daily_highlights(db: Session) -> dict:
    """
    Run the daily highlights job:
    1. Clear yesterday's system highlights
    2. Build candidate pool from DB
    3. Verify against RTM (completion status)
    4. Score and select final 5
    5. Apply system highlight label
    6. Persist suggestion metadata

    Returns:
        dict with status, selected_count, error (if any)
    """
    try:
        logger.info("Starting daily highlights job", extra={"component": "highlights"})

        # Step 1: Clear system highlights from yesterday
        clear_system_highlights(db)

        # Step 2: Build candidate pool
        candidates = build_candidate_pool(db)
        logger.info(
            f"Built candidate pool with {len(candidates)} tasks",
            extra={"component": "highlights", "operation": "build_candidates"},
        )

        if not candidates:
            logger.info(
                "No candidates available for highlights",
                extra={"component": "highlights"},
            )
            return {"status": "ok", "selected_count": 0, "reason": "no_candidates"}

        # Step 3: Verify against RTM (skip failed lookups)
        valid = filter_existing_uncompleted_tasks(db, candidates)
        logger.info(
            f"Verified {len(valid)} uncompleted tasks",
            extra={"component": "highlights", "operation": "verify"},
        )

        if not valid:
            logger.info(
                "No valid tasks after RTM verification",
                extra={"component": "highlights"},
            )
            return {"status": "ok", "selected_count": 0, "reason": "none_valid"}

        # Step 4: Score and select final 5
        selected = select_final(valid)
        logger.info(
            f"Selected {len(selected)} tasks for highlighting",
            extra={"component": "highlights", "operation": "select", "count": len(selected)},
        )

        # Step 5: Apply system highlight to RTM
        apply_highlights_to_rtm(db, selected)

        # Step 6: Update DB with suggestion metadata
        persist_suggestion_metadata(db, selected)

        logger.info(
            f"Daily highlights job completed. Highlighted {len(selected)} tasks.",
            extra={"component": "highlights", "selected_count": len(selected)},
        )

        return {
            "status": "ok",
            "selected_count": len(selected),
            "selected_ids": [t.rtm_task_id for t in selected],
        }

    except Exception as e:
        logger.error(
            f"Daily highlights job failed: {e}",
            extra={
                "component": "highlights",
                "operation": "run_daily_highlights",
                "error_type": "exception",
            },
            exc_info=True,
        )
        return {"status": "error", "error": str(e)}


def clear_system_highlights(db: Session) -> None:
    """
    Step 1: Remove #highlight-today tag from all tasks.

    Intent: Remove only system-owned label, never touch #highlight or other tags.
    """
    logger.debug(
        "Clearing system highlights",
        extra={"component": "highlights", "operation": "clear"},
    )

    try:
        # Get all tasks with #highlight-today
        tasks = rtm_call("rtm.tasks.getList", search=f"tag:{SYSTEM_LABEL}")

        if not tasks or "taskseries" not in tasks:
            logger.debug(
                "No tasks with system label to clear",
                extra={"component": "highlights"},
            )
            return

        count = 0
        for taskseries in tasks.get("taskseries", []):
            for task in taskseries.get("task", []):
                rtm_task_id = task.get("id")
                taskseries_id = taskseries.get("id")
                list_id = tasks.get("id")

                if rtm_task_id and taskseries_id and list_id:
                    try:
                        rtm_call(
                            "rtm.tasks.removeTag",
                            list_id=list_id,
                            taskseries_id=taskseries_id,
                            task_id=rtm_task_id,
                            tags=SYSTEM_LABEL,
                        )
                        count += 1
                    except Exception as e:
                        logger.warning(
                            f"Failed to clear highlight from task {rtm_task_id}: {e}",
                            extra={
                                "component": "highlights",
                                "operation": "clear_tag",
                                "rtm_task_id": rtm_task_id,
                                "error_type": "rtm_call_failed",
                            },
                        )

        logger.info(
            f"Cleared system highlights from {count} tasks",
            extra={"component": "highlights", "count": count},
        )

    except Exception as e:
        logger.error(
            f"Failed to clear system highlights: {e}",
            extra={
                "component": "highlights",
                "operation": "clear_system_highlights",
                "error_type": "exception",
            },
            exc_info=True,
        )
        raise


def build_candidate_pool(db: Session) -> List[models.RtmTask]:
    """
    Step 2: Build candidate pool from local DB (max 10).

    Hard filters:
    - NOT part of a project (rtm_project_id IS NULL)
    - NOT completed (rtm_completed = false)
    - NOT tagged with #na (tags NOT LIKE '%#na%')

    Anti-nag rule:
    - Exclude tasks suggested 3+ times in last 14 days

    Returns two bands:
    - Band A: Older tasks (14+ days), prefer least-recently-suggested
    - Band B: Recent tasks (7- days), prefer newest first
    """
    cutoff_14d = datetime.utcnow() - timedelta(days=14)
    cutoff_7d = datetime.utcnow() - timedelta(days=7)
    nag_cutoff = datetime.utcnow() - timedelta(days=SUGGESTION_WINDOW_DAYS)

    # Band A: Older tasks (14+ days old, anti-stall)
    band_a = (
        db.query(models.RtmTask)
        .filter(
            models.RtmTask.rtm_project_id.is_(None),
            models.RtmTask.rtm_completed.is_(False),
            ~models.RtmTask.tags.ilike(f"%{HIGHLIGHT_LABEL}%"),  # Never auto-suggest already highlighted
        )
        .filter(models.RtmTask.created_at < cutoff_14d)
        # Anti-nag: exclude if suggested 3+ times in last 14 days
        .filter(
            (models.RtmTask.times_suggested < MAX_SUGGESTIONS_14D)
            | (models.RtmTask.last_suggested_at < nag_cutoff)
        )
        .order_by(models.RtmTask.last_suggested_at.asc().nullsfirst())
        .limit(BAND_A_LIMIT)
        .all()
    )

    # Band B: Recent tasks (7- days old, easy wins)
    band_b = (
        db.query(models.RtmTask)
        .filter(
            models.RtmTask.rtm_project_id.is_(None),
            models.RtmTask.rtm_completed.is_(False),
            ~models.RtmTask.tags.ilike(f"%{HIGHLIGHT_LABEL}%"),
        )
        .filter(models.RtmTask.created_at >= cutoff_7d)
        # Anti-nag
        .filter(
            (models.RtmTask.times_suggested < MAX_SUGGESTIONS_14D)
            | (models.RtmTask.last_suggested_at < nag_cutoff)
        )
        .order_by(models.RtmTask.created_at.desc())
        .limit(BAND_B_LIMIT)
        .all()
    )

    candidates = band_a + band_b
    return candidates


def filter_existing_uncompleted_tasks(
    db: Session, candidates: List[models.RtmTask]
) -> List[models.RtmTask]:
    """
    Step 3: Verify candidates against RTM.

    For each candidate, fetch current status from RTM.
    Skip tasks that are completed or not found.
    """
    valid = []

    for task in candidates:
        try:
            status = rtm_call("rtm.tasks.getList", list_id=task.rtm_list_id)
            found = False
            completed = False

            for ts in status.get("taskseries", []):
                if ts.get("id") == task.rtm_taskseries_id:
                    found = True
                    for t in ts.get("task", []):
                        if t.get("id") == task.rtm_task_id:
                            # Check if completed
                            if t.get("completed"):
                                completed = True
                            break
                    break

            if not found:
                logger.debug(
                    f"Task {task.rtm_task_id} not found in RTM",
                    extra={"component": "highlights", "rtm_task_id": task.rtm_task_id},
                )
                # Mark as completed in DB to skip future checks
                task.rtm_completed = True
                db.add(task)
                with transactional_session(db):
                    pass
                continue

            if completed:
                logger.debug(
                    f"Task {task.rtm_task_id} is completed in RTM",
                    extra={"component": "highlights", "rtm_task_id": task.rtm_task_id},
                )
                task.rtm_completed = True
                db.add(task)
                with transactional_session(db):
                    pass
                continue

            # Task exists and is not completed
            valid.append(task)

        except Exception as e:
            logger.warning(
                f"Failed to verify task {task.rtm_task_id}: {e}",
                extra={
                    "component": "highlights",
                    "operation": "verify_task",
                    "rtm_task_id": task.rtm_task_id,
                    "error_type": "rtm_call_failed",
                },
            )
            # Skip on error (conservative approach)
            continue

    return valid


def select_final(candidates: List[models.RtmTask]) -> List[models.RtmTask]:
    """
    Step 4: Score and select final 5.

    Score = (5 if never suggested else 0)
          + (3 if 30+ days old else 0)
          + random(0, 2)

    Returns top 5 by score.
    """
    import random

    now = datetime.utcnow()
    scored = []

    for task in candidates:
        score = 0

        # Bonus for never-suggested
        if task.times_suggested == 0:
            score += 5

        # Bonus for old tasks
        age_days = (now - task.created_at).days
        if age_days >= 30:
            score += 3

        # Random tiebreaker
        score += random.uniform(0, 2)

        scored.append((task, score))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    # Return top 5
    return [task for task, score in scored[:FINAL_SELECT]]


def apply_highlights_to_rtm(db: Session, selected: List[models.RtmTask]) -> None:
    """
    Step 5: Apply #highlight-today tag to RTM.

    Important: Do not add #highlight, only #highlight-today.
    """
    for task in selected:
        try:
            rtm_call(
                "rtm.tasks.addTag",
                list_id=task.rtm_list_id,
                taskseries_id=task.rtm_taskseries_id,
                task_id=task.rtm_task_id,
                tags=SYSTEM_LABEL,
            )
            logger.debug(
                f"Applied highlight to task {task.rtm_task_id}",
                extra={"component": "highlights", "rtm_task_id": task.rtm_task_id},
            )
        except Exception as e:
            logger.warning(
                f"Failed to apply highlight to task {task.rtm_task_id}: {e}",
                extra={
                    "component": "highlights",
                    "operation": "apply_tag",
                    "rtm_task_id": task.rtm_task_id,
                    "error_type": "rtm_call_failed",
                },
            )
            # Continue with other tasks


def persist_suggestion_metadata(db: Session, selected: List[models.RtmTask]) -> None:
    """
    Step 6: Update DB with suggestion tracking.

    Increments times_suggested and updates last_suggested_at.
    """
    now = datetime.utcnow()

    for task in selected:
        task.times_suggested += 1
        task.last_suggested_at = now
        db.add(task)

    with transactional_session(db):
        pass

    logger.debug(
        f"Updated suggestion metadata for {len(selected)} tasks",
        extra={"component": "highlights", "count": len(selected)},
    )

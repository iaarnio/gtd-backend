"""
Batch backlog clarification pipeline.

Slow digestion of bulk-imported tasks at controlled rate (e.g., 5 per day).
After clarification, items feed into standard approve→commit pipeline.
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict

from sqlalchemy.orm import Session

from . import clarification, models
from .db_utils import transactional_session
from .logging_config import get_logger

logger = get_logger(__name__)

# Hard cap: 5 items per day (not configurable)
BACKLOG_DAILY_LIMIT = 5

# Max clarification attempts per item
MAX_CLARIFY_ATTEMPTS = 3


def bulk_import_backlog(db: Session, text: str) -> Dict[str, int]:
    """
    Import multiple tasks from plain text (one per line).

    Args:
        db: Database session
        text: Multi-line text, one task per line (whitespace lines ignored)

    Returns:
        dict with imported count and skipped count
    """
    lines = text.strip().split("\n")
    imported = 0
    skipped = 0

    for line in lines:
        line = line.strip()
        if not line:
            skipped += 1
            continue

        item = models.BacklogItem(
            raw_text=line,
            source="rtm-export",
            status="pending",
        )
        db.add(item)
        imported += 1

    with transactional_session(db):
        pass  # Context manager handles commit

    logger.info(
        f"Bulk imported {imported} backlog items (skipped {skipped} empty lines)",
        extra={
            "component": "backlog",
            "operation": "bulk_import",
            "imported": imported,
            "skipped": skipped,
        },
    )

    return {"imported": imported, "skipped_empty": skipped}


def nightly_backlog_drain(db: Session) -> Dict[str, any]:
    """
    Nightly job: Process up to BACKLOG_DAILY_LIMIT pending items.

    Selects FIFO (oldest first), clarifies, creates pending approvals.

    Returns:
        dict with processed count, failed count, errors
    """
    logger.info(
        "Starting nightly backlog drain",
        extra={"component": "backlog", "operation": "drain_start"},
    )

    # Select pending items FIFO
    items = (
        db.query(models.BacklogItem)
        .filter(models.BacklogItem.status == "pending")
        .order_by(models.BacklogItem.imported_at.asc())
        .limit(BACKLOG_DAILY_LIMIT)
        .all()
    )

    processed = 0
    failed = 0
    errors = []

    for item in items:
        try:
            _process_backlog_item(db, item)
            processed += 1
        except Exception as e:
            logger.error(
                f"Failed to process backlog item {item.id}: {e}",
                extra={
                    "component": "backlog",
                    "operation": "process_item",
                    "backlog_id": item.id,
                    "error_type": "exception",
                },
                exc_info=True,
            )
            failed += 1
            errors.append({"item_id": item.id, "error": str(e)})

    logger.info(
        f"Backlog drain complete: {processed} processed, {failed} failed",
        extra={
            "component": "backlog",
            "operation": "drain_complete",
            "processed": processed,
            "failed": failed,
        },
    )

    return {
        "status": "ok",
        "processed": processed,
        "failed": failed,
        "errors": errors if errors else None,
    }


def _process_backlog_item(db: Session, item: models.BacklogItem) -> None:
    """
    Process a single backlog item:
    1. Mark as processing
    2. Clarify with AI
    3. Create pending approval
    4. Mark as processed

    If clarification fails and max attempts reached, mark as failed.
    """
    item.status = "processing"
    db.add(item)
    with transactional_session(db):
        pass

    try:
        # Clarify the item text
        clarify_result = _clarify_backlog_item(db, item)

        if not clarify_result:
            # Clarification failed
            item.clarify_attempts += 1
            if item.clarify_attempts >= MAX_CLARIFY_ATTEMPTS:
                item.status = "failed"
                item.last_error = "Max clarification attempts reached"
                logger.warning(
                    f"Backlog item {item.id} failed after {MAX_CLARIFY_ATTEMPTS} attempts",
                    extra={
                        "component": "backlog",
                        "backlog_id": item.id,
                        "clarify_attempts": item.clarify_attempts,
                    },
                )
            else:
                item.status = "pending"
            db.add(item)
            with transactional_session(db):
                pass
            return

        # Create capture from backlog item
        # This feeds into the standard clarify→approve→commit pipeline
        capture = models.Capture(
            raw_text=item.raw_text,
            source="backlog",
            source_id=f"backlog-{item.id}",
            clarify_json=clarify_result,
            decision_status="proposed",  # Ready for approval
        )
        db.add(capture)

        with transactional_session(db):
            pass

        # Mark backlog item as processed
        item.status = "processed"
        item.processed_at = datetime.utcnow()
        db.add(item)

        with transactional_session(db):
            pass

        logger.info(
            f"Processed backlog item {item.id} → capture {capture.id}",
            extra={
                "component": "backlog",
                "operation": "item_processed",
                "backlog_id": item.id,
                "capture_id": capture.id,
            },
        )

    except Exception as e:
        # Mark as failed with error message
        item.clarify_attempts += 1
        item.status = "failed" if item.clarify_attempts >= MAX_CLARIFY_ATTEMPTS else "pending"
        item.last_error = str(e)
        db.add(item)
        with transactional_session(db):
            pass
        raise


def _clarify_backlog_item(db: Session, item: models.BacklogItem) -> Optional[str]:
    """
    Clarify a backlog item using the same LLM logic as email captures.

    Returns:
        JSON string of clarification result, or None if clarification failed
    """
    try:
        result = clarification._call_llm_api(item.raw_text)

        if result:
            import json

            clarify_json = json.dumps(result, ensure_ascii=False)
            logger.debug(
                f"Clarified backlog item {item.id}",
                extra={
                    "component": "backlog",
                    "operation": "clarify_item",
                    "backlog_id": item.id,
                },
            )
            return clarify_json

        return None

    except Exception as e:
        logger.warning(
            f"Backlog item {item.id} clarification failed: {e}",
            extra={
                "component": "backlog",
                "operation": "clarify_item",
                "backlog_id": item.id,
                "error_type": "clarification_failed",
            },
        )
        raise


def get_backlog_status(db: Session) -> Dict[str, int]:
    """
    Get current backlog status.

    Returns:
        dict with counts for each status
    """
    pending = db.query(models.BacklogItem).filter(
        models.BacklogItem.status == "pending"
    ).count()
    processed = db.query(models.BacklogItem).filter(
        models.BacklogItem.status == "processed"
    ).count()
    failed = db.query(models.BacklogItem).filter(
        models.BacklogItem.status == "failed"
    ).count()

    return {
        "pending": pending,
        "processed": processed,
        "failed": failed,
        "daily_limit": BACKLOG_DAILY_LIMIT,
    }

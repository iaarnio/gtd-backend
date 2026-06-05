import asyncio
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .config import config
from .db import SessionLocal
from .db_utils import transactional_session
from .models import Anchor, Capture
from .rtm import add_note, add_task, call as rtm_call, create_timeline, is_configured
from .time_utils import utcnow_iso_z, utcnow_naive

logger = logging.getLogger(__name__)


"""
RTM commit loop (Step 7).

Rules implemented:
- Only approved captures are committed.
- Next actions create one RTM task; projects create project + first next action.
- Duplicate RTM task creation is prevented via persisted commit state.
- RTM is treated as a write-only side effect (no readback).

Important note about duplicates:
RTM does not provide an idempotency key for task creation. To make
duplicate creation impossible, this loop *never retries* a commit that
is in an unknown state (e.g., network timeout where we can't know if
the task was created). Unknown state is persisted and requires an
explicit human decision to clear later (hardening step).
"""


# Import retry limits from config
MAX_COMMIT_ATTEMPTS = config.MAX_COMMIT_RETRIES
RETRY_DELAY_SECONDS = config.COMMIT_RETRY_DELAY
COMMIT_DEBOUNCE_SECONDS = config.COMMIT_DEBOUNCE_SECONDS

_debounced_sync_task: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return utcnow_iso_z()


def _classify_commit_error(exc: Exception) -> Tuple[str, str]:
    """
    Classify an exception into RTM commit status and error message.

    Returns (status, error_message) tuple where status is one of:
    - 'failed': Retryable error (network timeout, server error, RTM temporary failure)
    - 'auth_failed': Authentication error (invalid token, requires user re-auth)
    - 'unknown': Timeout/unknown state (cannot determine if task was created, requires manual review)

    Args:
        exc: The exception that occurred during commit

    Returns:
        (status: str, error_msg: str) tuple
    """
    error_type = type(exc).__name__
    error_msg = str(exc)

    # Timeout = unknown state (we don't know if task was created)
    # This should NOT be retried automatically to prevent duplicates
    if "Timeout" in error_type or "timeout" in error_msg.lower():
        return "unknown", f"Timeout during RTM commit: {error_msg}. Manual review required."

    # Authentication errors = requires user re-auth
    if "auth" in error_msg.lower() or "401" in error_msg or "403" in error_msg:
        return "auth_failed", f"RTM authentication failed: {error_msg}. User must re-authenticate."

    # Circuit breaker = temporary failure (service hammering prevention)
    if "circuit" in error_msg.lower():
        return "failed", f"RTM service temporarily unavailable (circuit breaker open): {error_msg}"

    # Network/server errors = retryable
    if any(term in error_msg.lower() for term in ["connection", "network", "server", "500", "503"]):
        return "failed", f"RTM temporary failure (retryable): {error_msg}"

    # Default: treat as retryable failure
    return "failed", f"RTM commit failed: {error_msg}"


def _parse_json_maybe(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return None
    return None


def _build_smart_add(
    task_name: str,
    *,
    include_na: bool,
    due_date: str,
    text_for_tags: str,
) -> str:
    tags: List[str] = []
    if include_na:
        tags.append("#na")
    if "terveys" in text_for_tags:
        tags.append("#terveys")
    if "vero" in text_for_tags:
        tags.append("#vero")
    if "joulu" in text_for_tags:
        tags.append("#joulu")

    parts = [task_name]
    if tags:
        parts.append(" ".join(tags))
    if due_date:
        parts.append(f"^{due_date}")

    return " ".join(parts)


def _compute_commit_entries(clar: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Build RTM Smart Add entries for a capture.

    Returns a list of (smart_add, task_name):
    - next_action/non_actionable: one entry (no #na)
    - project: two entries (project task + first next action with #na)

    Tags are based on spec:
    - next actions: #na
    - health: #terveys
    - tax: #vero
    - christmas: #joulu
    """
    ctype = (clar.get("type") or "").strip()
    project_name = (clar.get("project_name") or "").strip()
    project_shortname = (clar.get("project_shortname") or "").strip().upper()
    next_action = (clar.get("next_action") or "").strip()
    clarified_text = (clar.get("clarified_text") or "").strip()
    due_date = (clar.get("due_date") or "").strip()

    text_for_tags_base = " ".join([clarified_text, project_name, next_action]).lower()

    if ctype == "project":
        base = project_name or clarified_text or next_action or "Projekti"
        # project_shortname is required and must come from clarification (checked upstream).
        # It should already be uppercase from the extraction above.
        shortname = project_shortname
        if not shortname:
            # This should not happen if _commit_one_capture checks properly, but defensive:
            raise ValueError("project_shortname is required for projects")
        project_task_name = f"{shortname} - §§§ - {base}"
        project_smart_add = _build_smart_add(
            project_task_name,
            include_na=False,
            due_date=due_date,
            text_for_tags=f"{project_task_name} {text_for_tags_base}",
        )

        first_next_action = next_action or f"{shortname} --- Määritä ensimmäinen next action"
        action_smart_add = _build_smart_add(
            first_next_action,
            include_na=True,
            due_date=due_date,
            text_for_tags=f"{first_next_action} {text_for_tags_base}",
        )
        return [
            (project_smart_add, project_task_name),
            (action_smart_add, first_next_action),
        ]

    # next_action/non_actionable: single standalone task without #na.
    task_name = next_action or clarified_text or "Tehtävä"
    smart_add = _build_smart_add(
        task_name,
        include_na=False,
        due_date=due_date,
        text_for_tags=f"{task_name} {text_for_tags_base}",
    )
    return [(smart_add, task_name)]




def _commit_one_capture(db, capture: Capture) -> None:
    """
    Attempt to commit a single capture to RTM.

    Handles error classification, retry logic, and detailed error logging.
    Commit attempt count is incremented before attempting.
    """
    clar = _parse_json_maybe(capture.clarify_json) or {}
    ctype = (clar.get("type") or "").strip()

    # For projects, project_shortname is required from clarification.
    if ctype == "project":
        project_shortname = (clar.get("project_shortname") or "").strip().upper()
        if not project_shortname:
            # This is a permanent error - missing required field
            logger.error(
                f"Capture {capture.id}: missing project_shortname, cannot commit",
                extra={
                    "component": "rtm_commit",
                    "operation": "commit",
                    "capture_id": capture.id,
                    "error_type": "missing_field",
                },
            )
            capture.commit_status = "failed"
            capture.last_commit_attempt_at = utcnow_naive()
            capture.commit_error_message = "Missing project_shortname in clarification"
            db.add(capture)
            with transactional_session(db):
                pass  # Context manager handles commit
            return

    commit_entries = _compute_commit_entries(clar)
    logger.debug(
        f"Capture {capture.id}: prepared {len(commit_entries)} RTM task(s)",
        extra={
            "component": "rtm_commit",
            "operation": "commit",
            "capture_id": capture.id,
            "task_names": [task_name for _, task_name in commit_entries],
        },
    )

    # Increment attempt count before trying
    capture.commit_attempt_count += 1
    now = utcnow_naive()
    capture.last_commit_attempt_at = now

    # External side effect (RTM)
    try:
        # Get auth token from DB
        from .rtm_auth import get_rtm_auth
        auth_record = get_rtm_auth()
        if not auth_record or not auth_record.auth_token:
            raise RuntimeError("No RTM auth token available (user must authenticate)")

        logger.debug(
            f"Creating timeline and adding task to RTM for capture {capture.id} (attempt {capture.commit_attempt_count})",
            extra={
                "component": "rtm_commit",
                "operation": "commit",
                "capture_id": capture.id,
                "attempt": capture.commit_attempt_count,
            },
        )
        timeline = create_timeline(auth_token=auth_record.auth_token)
        created_task_ids = []
        for smart_add, task_name in commit_entries:
            ids = add_task(timeline=timeline, name=smart_add, auth_token=auth_record.auth_token)
            created_task_ids.append(ids)
            logger.info(
                f"Committed task for capture {capture.id}: {task_name}",
                extra={
                    "component": "rtm_commit",
                    "operation": "commit",
                    "capture_id": capture.id,
                    "attempt": capture.commit_attempt_count,
                    "task_name": task_name,
                    "task_id": ids.get("task_id"),
                },
            )

        # Add notes to the first (main) task if present in clarification
        notes_text = (clar.get("notes") or "").strip()
        if notes_text and created_task_ids:
            first_ids = created_task_ids[0]
            try:
                add_note(
                    timeline=timeline,
                    list_id=first_ids["list_id"],
                    taskseries_id=first_ids["taskseries_id"],
                    task_id=first_ids["task_id"],
                    note_title="",
                    note_text=notes_text,
                    auth_token=auth_record.auth_token,
                )
                logger.info(
                    f"Added note to RTM task for capture {capture.id}",
                    extra={
                        "component": "rtm_commit",
                        "operation": "add_note",
                        "capture_id": capture.id,
                    },
                )
            except Exception as note_exc:
                # Note creation failure is non-fatal: the task itself was created.
                # Log the error but do not change commit_status.
                logger.warning(
                    f"Failed to add note to RTM task for capture {capture.id}: {note_exc}",
                    extra={
                        "component": "rtm_commit",
                        "operation": "add_note",
                        "capture_id": capture.id,
                        "error_type": type(note_exc).__name__,
                    },
                )

        # Success: update commit_status
        capture.commit_status = "committed"
        capture.commit_error_message = None
        capture.rtm_task_id = created_task_ids[0].get("task_id")
        capture.rtm_taskseries_id = created_task_ids[0].get("taskseries_id")
        capture.rtm_list_id = created_task_ids[0].get("list_id")
        logger.info(
            f"Successfully committed capture {capture.id} to RTM with {len(created_task_ids)} task(s) (attempt {capture.commit_attempt_count})",
            extra={
                "component": "rtm_commit",
                "operation": "commit",
                "capture_id": capture.id,
                "attempt": capture.commit_attempt_count,
                "task_count": len(created_task_ids),
            },
        )
    except Exception as exc:
        # Classify the error
        error_status, error_msg = _classify_commit_error(exc)
        if ctype == "project":
            # Project commit can create two tasks. If failure occurs mid-sequence, retrying can duplicate.
            # Mark unknown to force manual review and avoid automatic duplicate creation.
            error_status = "unknown"
            error_msg = f"Project commit failed in multi-task flow: {error_msg}"

        # Check if we've exceeded max attempts
        if capture.commit_attempt_count >= MAX_COMMIT_ATTEMPTS:
            capture.commit_status = "permanently_failed" if error_status != "unknown" else "unknown"
            logger.error(
                f"Commit permanently failed for capture {capture.id} after {MAX_COMMIT_ATTEMPTS} attempts",
                extra={
                    "component": "rtm_commit",
                    "operation": "commit",
                    "capture_id": capture.id,
                    "error_type": error_status,
                    "attempt": capture.commit_attempt_count,
                    "retry_count": MAX_COMMIT_ATTEMPTS,
                },
                exc_info=True,
            )
        else:
            # Will retry later
            capture.commit_status = error_status if error_status in ["auth_failed", "unknown"] else "failed"
            logger.warning(
                f"Commit failed for capture {capture.id}, will retry (attempt {capture.commit_attempt_count}/{MAX_COMMIT_ATTEMPTS})",
                extra={
                    "component": "rtm_commit",
                    "operation": "commit",
                    "capture_id": capture.id,
                    "error_type": error_status,
                    "attempt": capture.commit_attempt_count,
                    "retry_count": MAX_COMMIT_ATTEMPTS,
                },
                exc_info=True,
            )

        capture.commit_error_message = error_msg
        # Record when this capture first entered a failed state (never overwritten).
        if capture.first_failed_at is None:
            capture.first_failed_at = now
        db.add(capture)
        with transactional_session(db):
            pass  # Context manager handles commit
        return

    db.add(capture)
    with transactional_session(db):
        pass  # Context manager handles commit


def _get_active_anchor(db, today: date) -> Optional[Anchor]:
    return (
        db.query(Anchor)
        .filter(
            Anchor.kind == "approval_anchor",
            Anchor.status == "active",
            Anchor.valid_until >= today,
        )
        .order_by(Anchor.created_at.desc())
        .first()
    )


def _anchor_task_exists_in_rtm(auth_token: str, anchor_name: str) -> bool:
    """
    Check if an incomplete RTM task with the exact anchor name already exists.
    """
    data = rtm_call(
        "rtm.tasks.getList",
        {"filter": "status:incomplete"},
        auth_token=auth_token,
    )
    raw = data.get("raw")
    if not raw:
        raise RuntimeError("RTM getList response missing raw XML")

    root = ET.fromstring(raw)
    if root.get("stat") != "ok":
        err = root.find("err")
        err_msg = err.get("msg") if err is not None else "Unknown RTM error"
        raise RuntimeError(f"RTM getList failed: {err_msg}")

    tasks_elem = root.find("tasks")
    if tasks_elem is None:
        return False

    for list_elem in tasks_elem.findall("list"):
        for taskseries in list_elem.findall("taskseries"):
            name = (taskseries.get("name") or "").strip()
            if name == anchor_name:
                return True

    return False


def _ensure_anchor_for_pending_approvals(db) -> None:
    """
    If there are proposed captures and no active anchor for today,
    create a single RTM anchor task and record it.
    """
    # Check if there are any proposed captures.
    has_proposed = (
        db.query(Capture.id)
        .filter(Capture.decision_status == "proposed")
        .first()
        is not None
    )
    if not has_proposed:
        return

    today = date.today()
    anchor = _get_active_anchor(db, today)
    if anchor:
        return

    anchor_name = "Tarkista GTD-hyväksynnät"

    from .rtm_auth import get_rtm_auth
    auth_record = get_rtm_auth()
    if not auth_record or not auth_record.auth_token:
        return

    # Guard against duplicates in RTM.
    try:
        anchor_exists = _anchor_task_exists_in_rtm(auth_record.auth_token, anchor_name)
    except Exception as exc:
        logger.warning(
            f"Could not verify existing RTM anchor task: {exc}",
            extra={
                "component": "rtm_commit",
                "operation": "anchor_exists_check",
                "error_type": "rtm_call_failed",
            },
        )
        return

    # Expire any old active anchors.
    db.query(Anchor).filter(
        Anchor.kind == "approval_anchor",
        Anchor.status == "active",
        Anchor.valid_until < today,
    ).update({"status": "expired"})
    with transactional_session(db):
        pass  # Context manager handles commit

    # Create a new anchor record.
    anchor = Anchor(
        kind="approval_anchor",
        status="active",
        valid_until=today,
    )
    db.add(anchor)
    with transactional_session(db):
        pass  # Context manager handles commit
    db.refresh(anchor)

    if anchor_exists:
        state: Dict[str, Any] = {
            "provider": "rtm",
            "status": "already_exists",
            "anchor_name": anchor_name,
            "updated_at": _now_iso(),
        }
        anchor.external_state = json.dumps(state, ensure_ascii=False)
        db.add(anchor)
        with transactional_session(db):
            pass  # Context manager handles commit
        return

    # Attempt to create the RTM anchor task.
    # Anchor is not a project, so it uses a simple task name without project format.
    # Include today's date so it appears as priority in daily list
    today_iso = today.isoformat()
    smart_add = f"{anchor_name} ^{today_iso}"

    state: Dict[str, Any] = {
        "provider": "rtm",
        "status": "in_progress",
        "smart_add": smart_add,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    try:
        timeline = create_timeline(auth_token=auth_record.auth_token)
        ids = add_task(timeline=timeline, name=smart_add, auth_token=auth_record.auth_token)
    except Exception as exc:
        # Unknown state: we do not retry automatically to avoid
        # potential duplicates. This remains visible in the DB.
        state.update(
            {
                "status": "unknown",
                "last_error": str(exc),
                "updated_at": _now_iso(),
            }
        )
        anchor.external_state = json.dumps(state, ensure_ascii=False)
        db.add(anchor)
        with transactional_session(db):
            pass  # Context manager handles commit
        return

    state.update(
        {
            "status": "committed",
            "timeline": timeline,
            "rtm": ids,
            "updated_at": _now_iso(),
        }
    )
    anchor.external_state = json.dumps(state, ensure_ascii=False)
    db.add(anchor)
    with transactional_session(db):
        pass  # Context manager handles commit


def _poll_once() -> None:
    import os
    # Check if RTM API credentials are configured
    api_key = os.environ.get("RTM_API_KEY")
    shared_secret = os.environ.get("RTM_SHARED_SECRET")

    if not api_key or not shared_secret:
        # RTM is optional; without config, commit loop is disabled.
        logger.debug("RTM API credentials not configured, skipping commit loop")
        return

    # Check if RTM auth token is valid (stored in database after bootstrap)
    from .rtm_auth import is_rtm_auth_valid
    if not is_rtm_auth_valid():
        logger.info("RTM auth token not valid or not configured, skipping commit loop")
        return

    db = SessionLocal()
    try:
        # Only fetch captures ready for commit: approved + not yet committed
        pending_commits = (
            db.query(Capture)
            .filter(
                Capture.decision_status == "approved",
                Capture.commit_status.in_(["pending", "failed"])
            )
            .order_by(Capture.created_at.asc())
            .all()
        )
        logger.info(f"RTM commit poll: found {len(pending_commits)} captures ready to commit")
        for capture in pending_commits:
            logger.info(f"Committing capture {capture.id} to RTM")
            _commit_one_capture(db, capture)
        # After processing approved captures, ensure a single anchor
        # task exists when there are pending approvals.
        _ensure_anchor_for_pending_approvals(db)
    finally:
        db.close()


async def retry_failed_captures(capture_ids: list) -> None:
    """
    Background retry task for captures that failed immediate sync.

    Retries up to (MAX_COMMIT_ATTEMPTS - 1) times with RETRY_DELAY_SECONDS
    between attempts. Runs as an asyncio task — does NOT block HTTP responses.

    Args:
        capture_ids: List of capture IDs to retry
    """
    for attempt in range(MAX_COMMIT_ATTEMPTS - 1):
        await asyncio.sleep(RETRY_DELAY_SECONDS)

        db = SessionLocal()
        try:
            remaining = (
                db.query(Capture)
                .filter(
                    Capture.id.in_(capture_ids),
                    Capture.commit_status.in_(["pending", "failed"]),
                )
                .all()
            )
            if not remaining:
                logger.info(
                    f"Background retry: all captures committed, stopping",
                    extra={"component": "rtm_commit", "operation": "background_retry"},
                )
                return

            logger.info(
                f"Background retry attempt {attempt + 1}/{MAX_COMMIT_ATTEMPTS - 1}: "
                f"{len(remaining)} captures to retry",
                extra={
                    "component": "rtm_commit",
                    "operation": "background_retry",
                    "attempt": attempt + 1,
                    "capture_count": len(remaining),
                },
            )
            for capture in remaining:
                _commit_one_capture(db, capture)
        except Exception as e:
            logger.error(
                f"Error in background retry: {e}",
                extra={"component": "rtm_commit", "operation": "background_retry"},
                exc_info=True,
            )
        finally:
            db.close()


def schedule_debounced_sync() -> None:
    """
    Schedule debounced background sync for approved captures.

    Every new approval resets the timer. Sync starts only after there
    have been no new approvals for COMMIT_DEBOUNCE_SECONDS.
    """
    global _debounced_sync_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "No running event loop; skipping debounced sync scheduling",
            extra={"component": "rtm_commit", "operation": "debounced_schedule"},
        )
        return

    if _debounced_sync_task and not _debounced_sync_task.done():
        _debounced_sync_task.cancel()

    _debounced_sync_task = loop.create_task(_run_debounced_sync())


async def _run_debounced_sync() -> None:
    """Wait for debounce window, then sync approved captures off-thread."""
    global _debounced_sync_task

    try:
        if COMMIT_DEBOUNCE_SECONDS > 0:
            await asyncio.sleep(COMMIT_DEBOUNCE_SECONDS)

        failed_ids = await asyncio.to_thread(sync_approved_captures)
        if failed_ids:
            asyncio.create_task(retry_failed_captures(failed_ids))
    except asyncio.CancelledError:
        logger.debug(
            "Debounced RTM sync rescheduled",
            extra={"component": "rtm_commit", "operation": "debounced_schedule"},
        )
        raise
    except Exception as e:
        logger.error(
            f"Debounced RTM sync failed: {e}",
            extra={"component": "rtm_commit", "operation": "debounced_sync"},
            exc_info=True,
        )
    finally:
        current_task = asyncio.current_task()
        if _debounced_sync_task is current_task:
            _debounced_sync_task = None


def sync_approved_captures(capture_ids: list = None) -> list:
    """
    Immediately sync approved captures to RTM.

    Called after approve_capture() to sync the just-approved capture
    and any previously failed captures.

    Args:
        capture_ids: Optional list of specific capture IDs to sync.
                     If None, syncs all pending/failed approved captures.

    Returns:
        List of capture IDs that failed and need background retry.
    """
    import os

    api_key = os.environ.get("RTM_API_KEY")
    shared_secret = os.environ.get("RTM_SHARED_SECRET")
    if not api_key or not shared_secret:
        logger.debug("RTM API credentials not configured, skipping sync")
        return []

    from .rtm_auth import is_rtm_auth_valid
    if not is_rtm_auth_valid():
        logger.info("RTM auth token not valid, skipping sync")
        return []

    db = SessionLocal()
    failed_ids = []
    try:
        # Always process all pending/failed approved captures
        # (includes the just-approved one plus any previously failed)
        captures = (
            db.query(Capture)
            .filter(
                Capture.decision_status == "approved",
                Capture.commit_status.in_(["pending", "failed"]),
            )
            .order_by(Capture.created_at.asc())
            .all()
        )

        if not captures:
            return []

        logger.info(
            f"Immediate RTM sync: processing {len(captures)} captures",
            extra={
                "component": "rtm_commit",
                "operation": "immediate_sync",
                "capture_count": len(captures),
            },
        )

        for capture in captures:
            _commit_one_capture(db, capture)
            # Re-read status after commit attempt
            db.refresh(capture)
            if capture.commit_status in ("pending", "failed"):
                failed_ids.append(capture.id)
    except Exception as e:
        logger.error(
            f"Error in immediate RTM sync: {e}",
            extra={"component": "rtm_commit", "operation": "immediate_sync"},
            exc_info=True,
        )
    finally:
        db.close()

    return failed_ids


# ---------------------------------------------------------------------------
# Extended retry backoff schedule (seconds from last_commit_attempt_at).
# After the initial fast retries (3 × 5 min), this loop keeps trying with
# increasing delays up to 24 h from first_failed_at.
# ---------------------------------------------------------------------------
_EXTENDED_BACKOFF_SECONDS = [
    30 * 60,       # +30 min
    60 * 60,       # +1 h
    2 * 60 * 60,   # +2 h
    4 * 60 * 60,   # +4 h
    8 * 60 * 60,   # +8 h
    24 * 60 * 60,  # +24 h (last attempt)
]

# How long to keep retrying from first_failed_at before giving up entirely.
_RETRY_WINDOW_SECONDS = 25 * 60 * 60  # 25 h

# Send the "still failing" notification after this many seconds from first_failed_at.
_NOTIFY_AFTER_SECONDS = 2 * 60 * 60   # 2 h

# Send the "gave up" notification after this many seconds from first_failed_at.
_NOTIFY_FINAL_SECONDS = 24 * 60 * 60  # 24 h

# How often the extended-retry loop wakes up.
_EXTENDED_LOOP_INTERVAL = int(
    __import__("os").environ.get("RTM_EXTENDED_RETRY_LOOP_INTERVAL", "600")
)  # 10 min default

_extended_retry_task: Optional[asyncio.Task] = None


def _should_extended_retry(capture) -> bool:
    """
    Return True if the capture is due for an extended retry attempt.

    Backoff logic: the minimum gap between retries grows as the total failure
    duration grows.  The schedule (indexed by elapsed_since_first) is:

        < 1 h   → retry after 30 min
        < 2 h   → retry after 1 h
        < 4 h   → retry after 2 h
        < 8 h   → retry after 4 h
        < 24 h  → retry after 8 h
        < 25 h  → retry after 24 h  (one final attempt, then give up)
        ≥ 25 h  → no more retries

    The loop wakes every 10 min so actual retry times are rounded up to the
    next loop tick.
    """
    if capture.commit_status not in ("failed", "permanently_failed"):
        return False
    if capture.first_failed_at is None or capture.last_commit_attempt_at is None:
        return False

    now = utcnow_naive()

    elapsed_since_first = (now - capture.first_failed_at).total_seconds()
    if elapsed_since_first > _RETRY_WINDOW_SECONDS:
        return False

    elapsed_since_last = (now - capture.last_commit_attempt_at).total_seconds()

    # Determine the minimum gap required between retries based on total failure age.
    # _EXTENDED_BACKOFF_SECONDS is [30m, 1h, 2h, 4h, 8h, 24h].
    # We pair consecutive thresholds: if failure age is within the *next* threshold,
    # wait at least the *current* threshold before retrying.
    thresholds = _EXTENDED_BACKOFF_SECONDS  # [1800, 3600, 7200, 14400, 28800, 86400]
    for i, min_gap in enumerate(thresholds):
        next_threshold = thresholds[i + 1] if i + 1 < len(thresholds) else _RETRY_WINDOW_SECONDS
        if elapsed_since_first < next_threshold:
            return elapsed_since_last >= min_gap

    # Beyond all thresholds (shouldn't reach here given the window check above).
    return False


def _extended_retry_one(db, capture) -> None:
    """
    Reset a permanently_failed/failed capture for one more attempt.

    Resets attempt_count to 0 so _commit_one_capture's MAX_COMMIT_ATTEMPTS
    gate doesn't immediately re-block it.
    """
    logger.info(
        f"Extended retry: resetting capture {capture.id} for reattempt "
        f"(first_failed_at={capture.first_failed_at}, "
        f"attempts_so_far={capture.commit_attempt_count})",
        extra={
            "component": "rtm_commit",
            "operation": "extended_retry",
            "capture_id": capture.id,
        },
    )
    capture.commit_attempt_count = 0
    capture.commit_status = "failed"
    db.add(capture)
    with transactional_session(db):
        pass
    _commit_one_capture(db, capture)


def _check_and_notify(db) -> None:
    """
    Send notification emails for captures that have been failing long enough.

    - "Still failing" email: fired once when first_failed_at > 2 h ago and
      no prior notification has been sent.
    - "Gave up" email: fired once when first_failed_at > 24 h ago and the
      prior notification was the 2 h one (failure_notified_at < first_failed_at + 23 h).
    """
    from .email_notify import is_configured as smtp_configured, send_rtm_failure_notification

    if not smtp_configured():
        return

    now = utcnow_naive()

    failed_statuses = ("failed", "permanently_failed", "auth_failed", "unknown")

    all_failing = (
        db.query(Capture)
        .filter(
            Capture.decision_status == "approved",
            Capture.commit_status.in_(failed_statuses),
            Capture.first_failed_at.isnot(None),
        )
        .all()
    )

    two_hour_candidates = []
    final_candidates = []

    for c in all_failing:
        elapsed = (now - c.first_failed_at).total_seconds()

        if elapsed >= _NOTIFY_FINAL_SECONDS:
            # Final notification: send if no notification sent yet, OR if the last
            # notification was the 2 h one (i.e., was sent before the 24 h mark).
            final_threshold_ts = c.first_failed_at + __import__("datetime").timedelta(
                seconds=_NOTIFY_FINAL_SECONDS
            )
            if c.failure_notified_at is None or c.failure_notified_at < final_threshold_ts:
                final_candidates.append(c)

        elif elapsed >= _NOTIFY_AFTER_SECONDS:
            # 2 h notification: only if never notified for this failure episode.
            if c.failure_notified_at is None:
                two_hour_candidates.append(c)

    if two_hour_candidates:
        sent = send_rtm_failure_notification(two_hour_candidates, is_final=False)
        if sent:
            ts = utcnow_naive()
            for c in two_hour_candidates:
                c.failure_notified_at = ts
                db.add(c)
            with transactional_session(db):
                pass
            logger.info(
                f"Sent 2 h RTM failure notification for {len(two_hour_candidates)} capture(s)",
                extra={"component": "rtm_commit", "operation": "notify"},
            )

    if final_candidates:
        sent = send_rtm_failure_notification(final_candidates, is_final=True)
        if sent:
            ts = utcnow_naive()
            for c in final_candidates:
                c.failure_notified_at = ts
                db.add(c)
            with transactional_session(db):
                pass
            logger.info(
                f"Sent final RTM failure notification for {len(final_candidates)} capture(s)",
                extra={"component": "rtm_commit", "operation": "notify"},
            )


def _extended_retry_poll_once() -> None:
    """
    Single pass of the extended retry loop.

    1. Retry captures that are due for a backoff-scheduled reattempt.
    2. Send notification emails for captures that have been failing long enough.
    """
    import os as _os
    if not _os.environ.get("RTM_API_KEY") or not _os.environ.get("RTM_SHARED_SECRET"):
        return

    from .rtm_auth import is_rtm_auth_valid
    if not is_rtm_auth_valid():
        return

    db = SessionLocal()
    try:
        # Find all captures eligible for extended retry.
        candidates = (
            db.query(Capture)
            .filter(
                Capture.decision_status == "approved",
                Capture.commit_status.in_(("failed", "permanently_failed")),
                Capture.first_failed_at.isnot(None),
            )
            .all()
        )

        retried = 0
        for capture in candidates:
            if _should_extended_retry(capture):
                _extended_retry_one(db, capture)
                retried += 1

        if retried:
            logger.info(
                f"Extended retry loop: retried {retried} capture(s)",
                extra={"component": "rtm_commit", "operation": "extended_retry"},
            )

        # Notification check (independent of retry).
        _check_and_notify(db)

    except Exception as exc:
        logger.error(
            f"Error in extended retry loop: {exc}",
            extra={"component": "rtm_commit", "operation": "extended_retry"},
            exc_info=True,
        )
    finally:
        db.close()


async def _run_extended_retry_loop() -> None:
    """Async loop: wake every _EXTENDED_LOOP_INTERVAL seconds."""
    global _extended_retry_task
    logger.info(
        f"Extended RTM retry loop started (interval={_EXTENDED_LOOP_INTERVAL}s)",
        extra={"component": "rtm_commit", "operation": "extended_retry_loop"},
    )
    while True:
        try:
            await asyncio.sleep(_EXTENDED_LOOP_INTERVAL)
            await asyncio.to_thread(_extended_retry_poll_once)
        except asyncio.CancelledError:
            logger.info(
                "Extended RTM retry loop cancelled",
                extra={"component": "rtm_commit", "operation": "extended_retry_loop"},
            )
            raise
        except Exception as exc:
            logger.error(
                f"Unexpected error in extended retry loop: {exc}",
                extra={"component": "rtm_commit", "operation": "extended_retry_loop"},
                exc_info=True,
            )


def start_extended_retry_loop() -> None:
    """
    Start the extended retry / notification background loop.

    Safe to call from lifespan context (running event loop required).
    Idempotent: does nothing if the loop is already running.
    """
    global _extended_retry_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "No running event loop; extended retry loop not started",
            extra={"component": "rtm_commit", "operation": "extended_retry_loop"},
        )
        return

    if _extended_retry_task and not _extended_retry_task.done():
        return  # already running

    _extended_retry_task = loop.create_task(_run_extended_retry_loop())
    logger.info(
        "Extended RTM retry loop scheduled",
        extra={"component": "rtm_commit", "operation": "extended_retry_loop"},
    )


def startup_sweep() -> None:
    """
    One-time sweep on application startup.

    Processes any captures stuck in pending/failed state from before restart,
    and ensures anchor tasks are created for pending approvals.
    Replaces the old background polling loop for restart recovery.
    """
    logger.info(
        "Running startup RTM sweep",
        extra={"component": "rtm_commit", "operation": "startup_sweep"},
    )
    try:
        _poll_once()
    except Exception as e:
        logger.error(
            f"Error in startup RTM sweep: {e}",
            extra={"component": "rtm_commit", "operation": "startup_sweep"},
            exc_info=True,
        )

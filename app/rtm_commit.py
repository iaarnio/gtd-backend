import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import config
from .db import SessionLocal
from .db_utils import transactional_session
from .models import Anchor, Capture
from .rtm import add_task, call as rtm_call, create_timeline, is_configured

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


# Import polling interval and retry limits from config
POLL_INTERVAL_SECONDS = config.COMMIT_POLL_INTERVAL
MAX_COMMIT_ATTEMPTS = config.MAX_COMMIT_RETRIES


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


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
    - next_action/non_actionable: one entry (#na)
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

    # next_action or non_actionable: both create RTM tasks with #na tag.
    # non_actionable is just metadata that the AI wasn't confident, but user approved it anyway.
    task_name = next_action or clarified_text or "Tehtävä"
    smart_add = _build_smart_add(
        task_name,
        include_na=True,
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
            capture.last_commit_attempt_at = datetime.utcnow()
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
    now = datetime.utcnow()
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


def run_commit_loop() -> None:
    logger.info(f"RTM commit loop started, polling every {POLL_INTERVAL_SECONDS} seconds")
    while True:
        try:
            _poll_once()
        except Exception as e:
            # Crash-safe: never bring the service down because RTM is failing.
            logger.error(f"Error in RTM commit loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_committer() -> None:
    logger.info("Starting background RTM committer loop")
    thread = threading.Thread(target=run_commit_loop, name="rtm-committer", daemon=True)
    thread.start()
    logger.info("RTM committer thread started")

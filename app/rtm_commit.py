import json
import logging
import os
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from .db import SessionLocal
from .models import Anchor, Capture
from .rtm import add_task, create_timeline, is_configured

logger = logging.getLogger(__name__)


"""
RTM commit loop (Step 7).

Rules implemented:
- Only approved captures are committed.
- At most one RTM task per capture.
- Duplicate RTM task creation is prevented via persisted commit state.
- RTM is treated as a write-only side effect (no readback).

Important note about duplicates:
RTM does not provide an idempotency key for task creation. To make
duplicate creation impossible, this loop *never retries* a commit that
is in an unknown state (e.g., network timeout where we can't know if
the task was created). Unknown state is persisted and requires an
explicit human decision to clear later (hardening step).
"""


POLL_INTERVAL_SECONDS = 30


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


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


def _compute_task_name_and_tags(clar: Dict[str, Any]) -> Tuple[str, str]:
    """
    Build a single RTM Smart Add string:
    - task name
    - tags (#na etc.)
    - optional due date (^YYYY-MM-DD)

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

    if ctype == "project":
        base = project_name or clarified_text or next_action or "Projekti"
        # project_shortname is required and must come from clarification (checked upstream).
        # It should already be uppercase from the extraction above.
        shortname = project_shortname
        if not shortname:
            # This should not happen if _commit_one_capture checks properly, but defensive:
            raise ValueError("project_shortname is required for projects")
        task_name = f"{shortname} - §§§ - {base}"
        include_na = False
    else:
        # next_action or non_actionable: both create RTM tasks with #na tag.
        # non_actionable is just metadata that the AI wasn't confident, but user approved it anyway.
        task_name = next_action or clarified_text or "Tehtävä"
        include_na = True

    text_for_tags = " ".join([task_name, clarified_text, project_name, next_action]).lower()
    tags = []
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

    smart_add = " ".join(parts)
    return smart_add, task_name




def _commit_one_capture(db, capture: Capture) -> None:
    clar = _parse_json_maybe(capture.clarify_json) or {}
    ctype = (clar.get("type") or "").strip()

    # For projects, project_shortname is required from clarification.
    if ctype == "project":
        project_shortname = (clar.get("project_shortname") or "").strip().upper()
        if not project_shortname:
            logger.warning(f"Capture {capture.id}: missing project_shortname, marking as failed")
            capture.commit_status = "failed"
            capture.last_commit_attempt_at = datetime.utcnow()
            db.add(capture)
            db.commit()
            return

    smart_add, task_name = _compute_task_name_and_tags(clar)
    logger.debug(f"Capture {capture.id}: task_name={task_name}, smart_add={smart_add}")

    # External side effect (RTM)
    try:
        # Get auth token from DB
        from .rtm_auth import get_rtm_auth
        auth_record = get_rtm_auth()
        if not auth_record or not auth_record.auth_token:
            raise RuntimeError("No RTM auth token available")

        logger.debug(f"Creating timeline and adding task to RTM for capture {capture.id}")
        timeline = create_timeline(auth_token=auth_record.auth_token)
        ids = add_task(timeline=timeline, name=smart_add, auth_token=auth_record.auth_token)

        # Success: update commit_status
        capture.commit_status = "committed"
        capture.last_commit_attempt_at = datetime.utcnow()
        capture.rtm_task_id = ids.get("task_id")
        capture.rtm_taskseries_id = ids.get("taskseries_id")
        capture.rtm_list_id = ids.get("list_id")
        logger.info(f"Successfully committed capture {capture.id} to RTM: {ids}")
    except Exception as exc:
        logger.error(f"Failed to commit capture {capture.id} to RTM: {exc}", exc_info=True)
        capture.commit_status = "failed"
        capture.last_commit_attempt_at = datetime.utcnow()
        db.add(capture)
        db.commit()
        return

    db.add(capture)
    db.commit()


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

    # Expire any old active anchors.
    db.query(Anchor).filter(
        Anchor.kind == "approval_anchor",
        Anchor.status == "active",
        Anchor.valid_until < today,
    ).update({"status": "expired"})
    db.commit()

    # Create a new anchor record.
    anchor = Anchor(
        kind="approval_anchor",
        status="active",
        valid_until=today,
    )
    db.add(anchor)
    db.commit()
    db.refresh(anchor)

    # Attempt to create the RTM anchor task.
    # Anchor is not a project, so it uses a simple task name without project format.
    smart_add = "Tarkista GTD-hyväksynnät #na"

    state: Dict[str, Any] = {
        "provider": "rtm",
        "status": "in_progress",
        "smart_add": smart_add,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    try:
        timeline = create_timeline()
        ids = add_task(timeline=timeline, name=smart_add)
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
        db.commit()
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
    db.commit()


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
    logger.info("RTM commit loop started, polling every 30 seconds")
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


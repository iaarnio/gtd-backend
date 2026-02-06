import json
import os
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from .db import SessionLocal
from .models import Anchor, Capture
from .rtm import add_task, create_timeline, is_configured


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


def _commit_state(capture: Capture) -> Optional[Dict[str, Any]]:
    return _parse_json_maybe(capture.external_commit_state)


def _set_commit_state(db, capture: Capture, state: Dict[str, Any]) -> None:
    capture.external_commit_state = json.dumps(state, ensure_ascii=False)
    db.add(capture)
    db.commit()


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
        # next_action or non_actionable: we only create tasks for actionable items.
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


def _should_attempt_commit(capture: Capture) -> bool:
    if capture.decision_status != "approved":
        return False

    state = _commit_state(capture)
    if state is None:
        return True  # never attempted

    status = state.get("status")
    # We retry only explicit failures. Unknown / in_progress are not retried automatically.
    return status == "failed"


def _commit_one_capture(db, capture: Capture) -> None:
    clar = _parse_json_maybe(capture.clarify_json) or {}

    # Only commit actionable items. Non-actionable captures remain approved but uncommitted.
    ctype = (clar.get("type") or "").strip()
    if ctype == "non_actionable":
        _set_commit_state(
            db,
            capture,
            {
                "provider": "rtm",
                "status": "skipped",
                "reason": "non_actionable",
                "updated_at": _now_iso(),
            },
        )
        return

    # For projects, project_shortname is required from clarification.
    if ctype == "project":
        project_shortname = (clar.get("project_shortname") or "").strip().upper()
        if not project_shortname:
            _set_commit_state(
                db,
                capture,
                {
                    "provider": "rtm",
                    "status": "skipped",
                    "reason": "missing_project_shortname",
                    "updated_at": _now_iso(),
                },
            )
            return

    smart_add, task_name = _compute_task_name_and_tags(clar)

    # Mark in_progress before any external side effect.
    prev = _commit_state(capture) or {}
    attempts = int(prev.get("attempts") or 0) + 1
    in_progress = {
        "provider": "rtm",
        "status": "in_progress",
        "attempts": attempts,
        "task_name": task_name,
        "smart_add": smart_add,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _set_commit_state(db, capture, in_progress)

    # External side effect (RTM)
    try:
        timeline = create_timeline()
        ids = add_task(timeline=timeline, name=smart_add)
    except Exception as exc:
        # If we cannot be sure whether RTM created the task (timeouts etc.),
        # we mark this as unknown and do not retry automatically.
        unknown = {
            **in_progress,
            "status": "unknown",
            "updated_at": _now_iso(),
            "last_error": str(exc),
        }
        _set_commit_state(db, capture, unknown)
        return

    committed = {
        **in_progress,
        "status": "committed",
        "updated_at": _now_iso(),
        "committed_at": _now_iso(),
        "timeline": timeline,
        "rtm": ids,
    }
    _set_commit_state(db, capture, committed)


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
    if not is_configured():
        # RTM is optional; without config, commit loop is disabled.
        return

    db = SessionLocal()
    try:
        approved = (
            db.query(Capture)
            .filter(Capture.decision_status == "approved")
            .order_by(Capture.created_at.asc())
            .all()
        )
        for capture in approved:
            if not _should_attempt_commit(capture):
                continue
            _commit_one_capture(db, capture)
        # After processing approved captures, ensure a single anchor
        # task exists when there are pending approvals.
        _ensure_anchor_for_pending_approvals(db)
    finally:
        db.close()


def run_commit_loop() -> None:
    while True:
        try:
            _poll_once()
        except Exception:
            # Crash-safe: never bring the service down because RTM is failing.
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_committer() -> None:
    thread = threading.Thread(target=run_commit_loop, name="rtm-committer", daemon=True)
    thread.start()


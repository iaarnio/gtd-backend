import json
import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import clarification, email_ingestion, models, rtm_commit  # noqa: F401  - ensure models are imported
from .db import Base, engine, get_db
from .rtm import auth_get_frob, auth_get_token
from .rtm_auth import is_rtm_auth_valid, store_rtm_auth, bootstrap_rtm_auth_from_env
from .schemas import CaptureCreate, CaptureOut, ClarificationUpdate

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Personal GTD Backend", version="0.1.0")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def initialize_database() -> None:
    """
    Ensure the SQLite database file exists and all tables are created,
    then start the IMAP poller and clarification loop in the
    background.
    """
    # Importing models above ensures all metadata is registered on Base.
    Base.metadata.create_all(bind=engine)
    # Bootstrap RTM auth from .env on first startup
    bootstrap_rtm_auth_from_env()
    # Start email ingestion loop. If email credentials are not
    # configured this becomes a no-op.
    email_ingestion.start_background_poller()
    # Start clarification loop. If no OpenAI API key is configured this
    # becomes a no-op.
    clarification.start_background_clarifier()
    # Start RTM commit loop. If RTM is not configured this becomes a no-op.
    rtm_commit.start_background_committer()


@app.get("/health")
def health_check() -> dict:
    """
    Basic health endpoint used to verify that the service is running.
    """
    return {"status": "ok"}


@app.get("/audit-log", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """
    View complete audit trail of all captures with their full state:
    - Ingestion details (source, email_id, timestamps)
    - Clarification results
    - User decisions
    - RTM sync status
    """
    # Get all captures ordered by creation time (newest first)
    captures = (
        db.query(models.Capture)
        .order_by(models.Capture.created_at.desc())
        .all()
    )

    # Enrich captures with parsed clarification data
    enriched = []
    for c in captures:
        clar = _parse_clarify_json(c.clarify_json) or {}
        enriched.append({
            "id": c.id,
            "created_at": c.created_at,
            "source": c.source,
            "email_id": c.email_id,
            "email_link": c.email_link,
            "raw_text": c.raw_text[:100],  # First 100 chars
            "clarified_text": clar.get("clarified_text", ""),
            "decision_status": c.decision_status,
            "decision_at": c.decision_at,
            "commit_status": c.commit_status,
            "last_commit_attempt_at": c.last_commit_attempt_at,
            "rtm_task_id": c.rtm_task_id,
        })

    return templates.TemplateResponse(
        "audit_log.html",
        {"request": request, "captures": enriched},
    )


def _parse_clarify_json(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Helper to safely parse clarify_json text into a dictionary for the UI.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return None
    return None


@app.post("/captures", response_model=CaptureOut, status_code=status.HTTP_201_CREATED)
def create_capture(payload: CaptureCreate, db: Session = Depends(get_db)) -> CaptureOut:
    """
    Manually create a capture for testing and scaffolding.

    decision_status is always initialized to "proposed" by the model
    default; callers cannot skip this state.
    """
    capture = models.Capture(
        raw_text=payload.raw_text,
        source=payload.source,
        source_id=payload.source_id,
        source_link=payload.source_link,
    )
    db.add(capture)
    db.commit()
    db.refresh(capture)
    return capture


@app.get("/approvals", response_class=HTMLResponse)
def approvals_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """
    List all captures awaiting action:
    - Proposed (awaiting user decision)
    - Approved but not yet committed to RTM (awaiting sync)
    """
    # Get proposed captures (awaiting user decision)
    proposed = (
        db.query(models.Capture)
        .filter(models.Capture.decision_status == "proposed")
        .order_by(models.Capture.created_at.asc())
        .all()
    )

    # Get approved captures that haven't been successfully committed yet
    # (still awaiting RTM sync)
    approved_pending = (
        db.query(models.Capture)
        .filter(
            models.Capture.decision_status == "approved",
            models.Capture.commit_status != "committed"
        )
        .order_by(models.Capture.created_at.asc())
        .all()
    )

    # Combine all captures awaiting action
    all_captures = proposed + approved_pending

    # For each capture, attach a parsed clarification dict and status for display.
    enriched = []
    for c in all_captures:
        clar = _parse_clarify_json(c.clarify_json)
        clar = clar or {}

        # Determine status indicator
        if c.decision_status == "proposed":
            status_label = "Pending decision"
        else:
            # approved - show RTM sync status based on commit_status
            if c.commit_status == "pending":
                status_label = "Approved, waiting for RTM sync"
            elif c.commit_status == "failed":
                status_label = "RTM sync failed (will retry)"
            elif c.commit_status == "committed":
                status_label = "Synced to RTM ✓"
            else:
                status_label = f"RTM: {c.commit_status}"

        # Wrap in a simple object-like dict for template attribute access
        enriched.append(
            {
                "id": c.id,
                "created_at": c.created_at,
                "source": c.source,
                "clar_dict": clar,
                "decision_status": c.decision_status,
                "status_label": status_label,
            }
        )

    # Check RTM auth status
    rtm_auth_valid = is_rtm_auth_valid()

    return templates.TemplateResponse(
        "approvals_list.html",
        {"request": request, "captures": enriched, "rtm_auth_valid": rtm_auth_valid},
    )


@app.get("/approvals/{capture_id}", response_class=HTMLResponse)
def approval_detail(
    capture_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Detailed view of a single capture, including raw text and
    clarification JSON, with controls for editing and decision making.
    """
    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    clar_dict = _parse_clarify_json(capture.clarify_json) or {}
    # Pretty-print JSON for editing; fall back to an empty object.
    if capture.clarify_json:
        try:
            parsed = json.loads(capture.clarify_json)
            clarification_json = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            # If the stored value is not valid JSON, show it as-is so it can be fixed.
            clarification_json = capture.clarify_json
    else:
        clarification_json = "{\n  \n}"

    return templates.TemplateResponse(
        "approval_detail.html",
        {
            "request": request,
            "capture": capture,
            "clar_dict": clar_dict,
            "clarification_json": clarification_json,
        },
    )


@app.post("/approvals/{capture_id}/clarification")
async def approval_update_clarification(
    capture_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """
    Update clarification from the approval HTML form fields.
    """
    form = await request.form()

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    # Get current clarification to preserve fields not in form
    clar = _parse_clarify_json(capture.clarify_json) or {}

    # Update with form fields
    project_name = form.get("project_name") or ""
    next_action = form.get("next_action") or ""
    is_next_action = form.get("is_next_action") == "on"

    # Determine type: checkbox is the primary indicator
    if project_name:
        clar_type = "project"
    elif is_next_action:
        clar_type = "next_action"
    else:
        # Keep original type if neither project nor NA checkbox
        clar_type = clar.get("type", "project")

    clar.update({
        "type": clar_type,
        "clarified_text": project_name or next_action or clar.get("clarified_text"),
        "project_name": project_name or None,
        "project_shortname": (form.get("project_shortname") or "").upper() or None,
        "next_action": next_action or None,
        "suggested_context": form.get("suggested_context") or None,
        "due_date": form.get("due_date") or None,
        "notes": form.get("notes") or None,
    })

    capture.clarify_json = json.dumps(clar, ensure_ascii=False)
    # Reset commit status when clarification is updated so it will be reprocessed
    if capture.decision_status == "approved":
        capture.commit_status = "pending"
    db.add(capture)
    db.commit()

    return RedirectResponse(url=f"/approvals/{capture_id}", status_code=status.HTTP_303_SEE_OTHER)


def _ensure_proposed(capture: models.Capture) -> None:
    if capture.decision_status != "proposed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Capture is already {capture.decision_status}",
        )


@app.post("/approvals/{capture_id}/approve")
async def approve_capture(
    capture_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Mark a capture as approved. This transition is irreversible.
    Accepts form fields to save clarification before approving.
    """
    from datetime import datetime

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    _ensure_proposed(capture)

    # Try to save clarification from form fields
    try:
        form = await request.form()
        # Get current clarification to preserve fields not in form
        clar = _parse_clarify_json(capture.clarify_json) or {}

        # Update with form fields
        project_name = form.get("project_name") or ""
        next_action = form.get("next_action") or ""
        is_next_action = form.get("is_next_action") == "on"

        # Determine type: user can override via project_name or NA checkbox
        if project_name:
            clar_type = "project"
        elif is_next_action:
            clar_type = "next_action"
        else:
            # Keep original type if no explicit override
            clar_type = clar.get("type", "next_action")

        clar.update({
            "type": clar_type,
            "clarified_text": project_name or next_action or clar.get("clarified_text"),
            "project_name": project_name or None,
            "project_shortname": (form.get("project_shortname") or "").upper() or None,
            "next_action": next_action or None,
            "suggested_context": form.get("suggested_context") or None,
            "due_date": form.get("due_date") or None,
            "notes": form.get("notes") or None,
        })

        capture.clarify_json = json.dumps(clar, ensure_ascii=False)
    except Exception as e:
        # If form parsing fails, continue without saving
        logger.debug(f"Failed to save clarification in approve: {e}")

    capture.decision_status = "approved"
    capture.decision_at = datetime.utcnow()
    db.add(capture)
    db.commit()

    return RedirectResponse(url="/approvals", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/approvals/{capture_id}/reject")
async def reject_capture(
    capture_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Mark a capture as rejected. This transition is irreversible.
    Accepts form fields to save clarification before rejecting.
    """
    from datetime import datetime

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    _ensure_proposed(capture)

    # Try to save clarification from form fields
    try:
        form = await request.form()
        # Get current clarification to preserve fields not in form
        clar = _parse_clarify_json(capture.clarify_json) or {}

        # Update with form fields
        project_name = form.get("project_name") or ""
        next_action = form.get("next_action") or ""
        is_next_action = form.get("is_next_action") == "on"

        # Determine type: user can override via project_name or NA checkbox
        if project_name:
            clar_type = "project"
        elif is_next_action:
            clar_type = "next_action"
        else:
            # Keep original type if no explicit override
            clar_type = clar.get("type", "next_action")

        clar.update({
            "type": clar_type,
            "clarified_text": project_name or next_action or clar.get("clarified_text"),
            "project_name": project_name or None,
            "project_shortname": (form.get("project_shortname") or "").upper() or None,
            "next_action": next_action or None,
            "suggested_context": form.get("suggested_context") or None,
            "due_date": form.get("due_date") or None,
            "notes": form.get("notes") or None,
        })

        capture.clarify_json = json.dumps(clar, ensure_ascii=False)
    except Exception as e:
        # If form parsing fails, continue without saving
        logger.debug(f"Failed to save clarification in reject: {e}")

    capture.decision_status = "rejected"
    capture.decision_at = datetime.utcnow()
    db.add(capture)
    db.commit()

    return RedirectResponse(url="/approvals", status_code=status.HTTP_303_SEE_OTHER)


@app.put(
    "/captures/{capture_id}/clarification",
    response_model=CaptureOut,
)
def update_clarification(
    capture_id: int,
    payload: ClarificationUpdate,
    db: Session = Depends(get_db),
) -> CaptureOut:
    """
    Attach or replace clarification JSON for an existing capture.

    The JSON body is stored verbatim as text in the database.
    """
    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    # Store verbatim JSON as text; this keeps the database inspectable
    # while preserving the full AI output structure.
    capture.clarify_json = json.dumps(payload.data, ensure_ascii=False)
    db.add(capture)
    db.commit()
    db.refresh(capture)
    return capture


# RTM Authentication Endpoints


@app.post("/rtm/auth/start", response_class=HTMLResponse)
def rtm_auth_start(request: Request) -> HTMLResponse:
    """
    Initiate RTM auth flow.

    Generates a frob and builds the authorization URL, then shows the user
    a page with the auth URL and instructions to complete authorization.
    """
    try:
        frob = auth_get_frob()
        # Build RTM auth URL
        import os
        api_key = os.environ.get("RTM_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RTM not configured"
            )

        # Generate signature for auth URL
        from .rtm import _sign_params
        shared_secret = os.environ.get("RTM_SHARED_SECRET")
        auth_params = {
            "api_key": api_key,
            "perms": "delete",
            "frob": frob,
        }
        api_sig = _sign_params(shared_secret, auth_params)

        auth_url = (
            f"https://www.rememberthemilk.com/services/auth/"
            f"?api_key={api_key}"
            f"&perms=delete"
            f"&frob={frob}"
            f"&api_sig={api_sig}"
        )

        return templates.TemplateResponse(
            "rtm_auth.html",
            {"request": request, "auth_url": auth_url, "frob": frob},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start RTM auth: {str(e)}"
        )


@app.post("/rtm/auth/finish", response_class=RedirectResponse)
async def rtm_auth_finish(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """
    Complete RTM auth flow.

    Takes the frob from the form and exchanges it for an auth token.
    Stores the token in the database, then redirects back to approvals.
    """
    try:
        form = await request.form()
        frob = form.get("frob")
        if not frob:
            raise ValueError("Missing frob parameter")

        result = auth_get_token(frob)
        if result.get("stat") != "ok":
            raise ValueError(f"RTM auth failed: {result.get('err', {}).get('msg', 'unknown error')}")

        token = result.get("token")
        perms = result.get("perms")
        user_info = result.get("user", {})

        store_rtm_auth(
            auth_token=token,
            perms=perms,
            username=user_info.get("username"),
            user_id=user_info.get("id"),
        )

        return RedirectResponse(url="/approvals?auth=success", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url=f"/approvals?auth=failed&error={str(e)}", status_code=status.HTTP_303_SEE_OTHER)


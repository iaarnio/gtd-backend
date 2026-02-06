import json
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import clarification, email_ingestion, models, rtm_commit  # noqa: F401  - ensure models are imported
from .db import Base, engine, get_db
from .schemas import CaptureCreate, CaptureOut, ClarificationUpdate


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
    List all captures that are currently in the proposed state.
    """
    captures = (
        db.query(models.Capture)
        .filter(models.Capture.decision_status == "proposed")
        .order_by(models.Capture.created_at.asc())
        .all()
    )

    # For each capture, attach a parsed clarification dict for display.
    enriched = []
    for c in captures:
        clar = _parse_clarify_json(c.clarify_json)
        clar = clar or {}
        # Wrap in a simple object-like dict for template attribute access
        enriched.append(
            {
                "id": c.id,
                "created_at": c.created_at,
                "source": c.source,
                "clar_dict": clar,
            }
        )

    return templates.TemplateResponse(
        "approvals_list.html",
        {"request": request, "captures": enriched},
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
    Update clarification JSON from the approval HTML form.
    """
    form = await request.form()
    clarification_json = form.get("clarification_json")
    if clarification_json is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing clarification_json")

    try:
        data = json.loads(clarification_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    capture.clarify_json = json.dumps(data, ensure_ascii=False)
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
def approve_capture(
    capture_id: int,
    db: Session = Depends(get_db),
):
    """
    Mark a capture as approved. This transition is irreversible.
    """
    from datetime import datetime

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    _ensure_proposed(capture)

    capture.decision_status = "approved"
    capture.decision_at = datetime.utcnow()
    db.add(capture)
    db.commit()

    return RedirectResponse(url="/approvals", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/approvals/{capture_id}/reject")
def reject_capture(
    capture_id: int,
    db: Session = Depends(get_db),
):
    """
    Mark a capture as rejected. This transition is irreversible.
    """
    from datetime import datetime

    capture = db.get(models.Capture, capture_id)
    if capture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found")

    _ensure_proposed(capture)

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


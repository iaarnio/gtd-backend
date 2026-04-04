"""
Test factories for creating model instances in tests.
"""
import json
from datetime import datetime
from typing import Optional

from app.models import Capture


def make_capture(db, **kwargs) -> Capture:
    """Create and persist a Capture with sensible defaults."""
    defaults = {
        "raw_text": "Test capture",
        "source": "test",
        "decision_status": "proposed",
        "clarify_status": "pending",
        "commit_status": "pending",
        "commit_attempt_count": 0,
    }
    defaults.update(kwargs)
    c = Capture(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_approved_capture(
    db,
    *,
    clar_type: str = "next_action",
    next_action: str = "Buy groceries",
    project_name: Optional[str] = None,
    project_shortname: Optional[str] = None,
    commit_status: str = "pending",
    commit_attempt_count: int = 0,
    **kwargs,
) -> Capture:
    """Create an approved capture with clarification JSON."""
    clar = {
        "type": clar_type,
        "clarified_text": next_action or project_name or "Test",
        "next_action": next_action,
        "project_name": project_name,
        "project_shortname": project_shortname,
        "notes": None,
        "confidence_score": 0.9,
    }
    return make_capture(
        db,
        decision_status="approved",
        decision_at=datetime.utcnow(),
        clarify_status="completed",
        clarify_json=json.dumps(clar),
        commit_status=commit_status,
        commit_attempt_count=commit_attempt_count,
        **kwargs,
    )

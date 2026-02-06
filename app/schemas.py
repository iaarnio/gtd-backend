from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class CaptureCreate(BaseModel):
    """
    Payload used to create a new capture manually for testing.
    """

    raw_text: str
    source: str
    source_id: Optional[str] = None
    source_link: Optional[str] = None


class CaptureOut(BaseModel):
    """
    Public representation of a capture, suitable for debugging and
    manual inspection via the API.
    """

    id: int
    created_at: datetime
    raw_text: str
    source: str
    source_id: Optional[str] = None
    source_link: Optional[str] = None

    clarify_json: Optional[Any] = None
    decision_status: str
    decision_at: Optional[datetime] = None
    decision_notes: Optional[str] = None
    external_commit_state: Optional[str] = None

    class Config:
        from_attributes = True


class ClarificationUpdate(BaseModel):
    """
    Payload used to attach or replace clarification output.

    The JSON payload is stored verbatim as text in the database.
    """

    data: Any


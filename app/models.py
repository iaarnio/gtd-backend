from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Integer, String, Text

from .db import Base


class Capture(Base):
    """
    Core domain record representing a single captured input and its
    decision state.

    Step 2: schema only, no business logic.
    """

    __tablename__ = "captures"

    id = Column(Integer, primary_key=True, index=True)

    # Capture metadata
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )
    raw_text = Column(Text, nullable=False)
    source = Column(String(50), nullable=False)
    source_id = Column(String(255), nullable=True, index=True)
    source_link = Column(Text, nullable=True)

    # Clarification result stored verbatim as JSON text
    clarify_json = Column(Text, nullable=True)

    # Decision state
    decision_status = Column(
        String(20),
        nullable=False,
        default="proposed",  # proposed / approved / rejected
        index=True,
    )
    decision_at = Column(DateTime, nullable=True)
    decision_notes = Column(Text, nullable=True)

    # External commit state, e.g. RTM task id or status marker
    external_commit_state = Column(Text, nullable=True)


class Anchor(Base):
    """
    Represents an anchor/reminder task created in RTM.

    This is used to ensure that when there are pending approvals, there
    is at most one active RTM reminder task at a time. State is fully
    persisted for auditability.
    """

    __tablename__ = "anchors"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    # Logical kind, in case more anchor types are added later.
    kind = Column(String(50), nullable=False, default="approval_anchor", index=True)

    # active / expired
    status = Column(String(20), nullable=False, default="active", index=True)

    # Simple daily TTL: anchor is considered valid until this date (UTC).
    valid_until = Column(Date, nullable=False, default=date.today, index=True)

    # RTM integration state, e.g. committed/unknown with task ids.
    external_state = Column(Text, nullable=True)



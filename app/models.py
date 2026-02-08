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
    email_id = Column(String(255), nullable=True, index=True)
    email_link = Column(Text, nullable=True)

    # Clarification result stored verbatim as JSON text
    clarify_json = Column(Text, nullable=True)

    # Clarification state tracking
    # pending = awaiting clarification attempt
    # in_progress = currently being clarified
    # completed = successfully clarified
    # failed = clarification attempted but failed (will retry)
    # permanently_failed = max retries exceeded (requires manual intervention)
    clarify_status = Column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )

    # Number of clarification attempts made
    clarify_attempt_count = Column(Integer, nullable=False, default=0)

    # When this capture was last attempted to clarify
    last_clarify_attempt_at = Column(DateTime, nullable=True)

    # Decision state
    decision_status = Column(
        String(20),
        nullable=False,
        default="proposed",  # proposed / approved / rejected
        index=True,
    )
    decision_at = Column(DateTime, nullable=True)
    decision_notes = Column(Text, nullable=True)

    # Commit state (separate from decision_status)
    # pending = not yet committed to RTM
    # committed = successfully committed
    # failed = commit was attempted but failed (will retry)
    # auth_failed = authentication failed (requires user re-auth)
    # unknown = timeout/unknown state (requires manual review)
    # permanently_failed = max retries exceeded (requires manual intervention)
    commit_status = Column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )

    # When this capture was last attempted to commit
    last_commit_attempt_at = Column(DateTime, nullable=True)

    # Number of commit attempts made
    commit_attempt_count = Column(Integer, nullable=False, default=0)

    # Detailed error message from last failed commit (for operator visibility)
    commit_error_message = Column(Text, nullable=True)

    # RTM task IDs and metadata from successful commit
    rtm_task_id = Column(String(255), nullable=True)
    rtm_taskseries_id = Column(String(255), nullable=True)
    rtm_list_id = Column(String(255), nullable=True)

    # Legacy field - kept for backwards compatibility
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


class RtmAuth(Base):
    """
    Stores RTM authentication state and credentials.

    This is a singleton-like table (typically only one row) that tracks:
    - The current valid auth token for RTM API calls
    - When it was last validated
    - Whether the token is currently valid
    - User metadata from RTM
    """

    __tablename__ = "rtm_auth"

    id = Column(Integer, primary_key=True, index=True)

    # The RTM auth token used for API calls
    auth_token = Column(Text, nullable=True)

    # Expected permissions from RTM (e.g., "delete")
    perms = Column(String(50), nullable=True)

    # RTM username and user ID
    username = Column(String(255), nullable=True)
    user_id = Column(String(255), nullable=True)

    # Is the current token valid?
    valid = Column(String(20), nullable=False, default="unknown")  # unknown / valid / invalid

    # When this auth record was created
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    # When the token was last verified with RTM
    last_checked_at = Column(DateTime, nullable=True)


class RtmTask(Base):
    """
    Cache of RTM tasks for daily highlight system.

    Tracks:
    - RTM task metadata (ID, name, created_at)
    - Project association (project_id if task is part of project)
    - Completion status (rtm_completed)
    - Tags (cached snapshot)
    - Suggestion tracking (times_suggested, last_suggested_at)
    """

    __tablename__ = "rtm_tasks"

    id = Column(Integer, primary_key=True, index=True)

    # RTM task identifiers
    rtm_task_id = Column(String(50), unique=True, nullable=False, index=True)
    rtm_taskseries_id = Column(String(50), nullable=False)
    rtm_list_id = Column(String(50), nullable=False)

    # Task metadata
    name = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)

    # Project association (NULL = lonely action = eligible for highlight)
    rtm_project_id = Column(String(50), nullable=True, index=True)

    # Completion status
    rtm_completed = Column(Boolean, nullable=False, default=False, index=True)

    # Cached tags as JSON array (e.g., ["#na", "work"])
    tags = Column(Text, nullable=True)

    # Suggestion tracking for anti-nag rule
    times_suggested = Column(Integer, nullable=False, default=0)
    last_suggested_at = Column(DateTime, nullable=True, index=True)

    # When this record was last synced with RTM
    last_synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Timestamps
    created_at_db = Column(DateTime, nullable=False, default=datetime.utcnow)



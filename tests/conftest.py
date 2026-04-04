import os
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Force test-safe DB path before importing app.db (which creates directories on import).
TEST_DB_PATH = Path(__file__).resolve().parent / "test.db"
os.environ.setdefault("DATABASE_PATH", str(TEST_DB_PATH))

from app.db import Base

# Ensure all model tables are registered on Base.metadata.
from app import models  # noqa: F401


def _build_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def _db_session_generator():
    # This function is wrapped as a fixture below to keep mypy/linters simple.
    db = _build_session()
    try:
        yield db
    finally:
        db.close()


# Expose as pytest fixture without importing pytest at module top-level in app code.
import pytest  # noqa: E402


@pytest.fixture
def db_session():
    yield from _db_session_generator()


@pytest.fixture
def mock_rtm_api(monkeypatch):
    """
    Mock RTM API calls (create_timeline, add_task, add_note).

    Returns a tracker SimpleNamespace with:
      - calls: list of (function_name, args, kwargs) tuples
      - timeline_counter: increments per create_timeline call
      - task_counter: increments per add_task call
      - fail_on: set to a function name string to make it raise RuntimeError
      - fail_error: the exception class to raise (default RuntimeError)
    """
    tracker = SimpleNamespace(
        calls=[],
        timeline_counter=0,
        task_counter=0,
        fail_on=None,
        fail_error=RuntimeError,
        fail_message="Mocked RTM failure",
    )

    def mock_create_timeline(auth_token=None):
        tracker.calls.append(("create_timeline", (), {"auth_token": auth_token}))
        if tracker.fail_on == "create_timeline":
            raise tracker.fail_error(tracker.fail_message)
        tracker.timeline_counter += 1
        return f"timeline-{tracker.timeline_counter}"

    def mock_add_task(timeline, name, auth_token=None):
        tracker.calls.append(("add_task", (), {"timeline": timeline, "name": name, "auth_token": auth_token}))
        if tracker.fail_on == "add_task":
            raise tracker.fail_error(tracker.fail_message)
        tracker.task_counter += 1
        return {
            "list_id": f"list-{tracker.task_counter}",
            "taskseries_id": f"ts-{tracker.task_counter}",
            "task_id": f"task-{tracker.task_counter}",
        }

    def mock_add_note(timeline, list_id, taskseries_id, task_id, note_title, note_text, auth_token=None):
        tracker.calls.append(("add_note", (), {
            "timeline": timeline, "list_id": list_id,
            "taskseries_id": taskseries_id, "task_id": task_id,
            "note_title": note_title, "note_text": note_text,
            "auth_token": auth_token,
        }))
        if tracker.fail_on == "add_note":
            raise tracker.fail_error(tracker.fail_message)

    import app.rtm_commit as rtm_commit_mod
    monkeypatch.setattr(rtm_commit_mod, "create_timeline", mock_create_timeline)
    monkeypatch.setattr(rtm_commit_mod, "add_task", mock_add_task)
    monkeypatch.setattr(rtm_commit_mod, "add_note", mock_add_note)

    return tracker


@pytest.fixture
def mock_rtm_auth(monkeypatch):
    """
    Mock RTM auth so it always returns a valid token.
    Patches both rtm_commit and rtm_auth modules.
    """
    fake_auth = SimpleNamespace(auth_token="mock-token-valid", valid="valid")

    import app.rtm_auth as rtm_auth_mod
    import app.rtm_commit as rtm_commit_mod

    # Patch get_rtm_auth in rtm_commit (used by _commit_one_capture)
    monkeypatch.setattr(rtm_auth_mod, "get_rtm_auth", lambda: fake_auth)
    monkeypatch.setattr(rtm_auth_mod, "is_rtm_auth_valid", lambda: True)

    return fake_auth


@pytest.fixture
def mock_rtm_env(monkeypatch):
    """Set RTM env vars so credential checks pass."""
    monkeypatch.setenv("RTM_API_KEY", "test-api-key")
    monkeypatch.setenv("RTM_SHARED_SECRET", "test-shared-secret")


@pytest.fixture
def mock_session_local(db_session, monkeypatch):
    """
    Patch SessionLocal in rtm_commit so sync_approved_captures and
    retry_failed_captures use the test in-memory DB.
    """
    import app.rtm_commit as rtm_commit_mod

    def fake_session_local():
        return db_session

    monkeypatch.setattr(rtm_commit_mod, "SessionLocal", fake_session_local)
    return db_session

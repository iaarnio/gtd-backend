"""
Tests for RTM commit logic in app.rtm_commit.

Covers:
- _commit_one_capture: success/error paths, status transitions
- _classify_commit_error: error classification
"""

import json

import pytest

from app import models, rtm_commit
from app.time_utils import utcnow_naive
from tests.factories import make_approved_capture, make_capture


# ---------------------------------------------------------------------------
# _commit_one_capture: success path
# ---------------------------------------------------------------------------

class TestCommitOneCapture:
    """Tests for _commit_one_capture with mocked RTM API."""

    def test_successful_commit_sets_committed_status(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        capture = make_approved_capture(db_session, next_action="Buy groceries")

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "committed"
        assert capture.commit_error_message is None
        assert capture.commit_attempt_count == 1

    def test_successful_commit_stores_rtm_ids(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        capture = make_approved_capture(db_session, next_action="File taxes")

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.rtm_task_id == "task-1"
        assert capture.rtm_taskseries_id == "ts-1"
        assert capture.rtm_list_id == "list-1"

    def test_project_creates_two_rtm_tasks(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        capture = make_approved_capture(
            db_session,
            clar_type="project",
            project_name="Health tracking",
            project_shortname="HLTH",
            next_action="HLTH --- Research apps",
        )

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "committed"
        # Two add_task calls: project + first next action
        add_task_calls = [c for c in mock_rtm_api.calls if c[0] == "add_task"]
        assert len(add_task_calls) == 2

    def test_notes_added_to_first_task(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        clar = {
            "type": "next_action",
            "clarified_text": "Review doc",
            "next_action": "Review doc",
            "notes": "Check section 3 especially",
        }
        capture = make_capture(
            db_session,
            decision_status="approved",
            decision_at=utcnow_naive(),
            clarify_json=json.dumps(clar),
        )

        rtm_commit._commit_one_capture(db_session, capture)

        add_note_calls = [c for c in mock_rtm_api.calls if c[0] == "add_note"]
        assert len(add_note_calls) == 1
        assert add_note_calls[0][2]["note_text"] == "Check section 3 especially"

    def test_note_failure_is_nonfatal(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        """If add_note fails, the task is still committed successfully."""
        mock_rtm_api.fail_on = "add_note"

        clar = {
            "type": "next_action",
            "clarified_text": "Review doc",
            "next_action": "Review doc",
            "notes": "This note will fail",
        }
        capture = make_capture(
            db_session,
            decision_status="approved",
            decision_at=utcnow_naive(),
            clarify_json=json.dumps(clar),
        )

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "committed"


# ---------------------------------------------------------------------------
# _commit_one_capture: error paths
# ---------------------------------------------------------------------------

class TestCommitErrors:

    def test_network_error_sets_failed_status(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        mock_rtm_api.fail_on = "create_timeline"
        mock_rtm_api.fail_message = "Connection refused"

        capture = make_approved_capture(db_session)

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "failed"
        assert "Connection refused" in capture.commit_error_message
        assert capture.commit_attempt_count == 1

    def test_auth_error_sets_auth_failed_status(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        mock_rtm_api.fail_on = "create_timeline"
        mock_rtm_api.fail_message = "401 Unauthorized"

        capture = make_approved_capture(db_session)

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "auth_failed"

    def test_timeout_error_sets_unknown_status(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        mock_rtm_api.fail_on = "create_timeline"
        mock_rtm_api.fail_error = TimeoutError
        mock_rtm_api.fail_message = "Request timed out"

        capture = make_approved_capture(db_session)

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "unknown"
        assert "Manual review required" in capture.commit_error_message

    def test_project_failure_always_sets_unknown(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        """Project commits set unknown on failure to prevent partial duplicates."""
        mock_rtm_api.fail_on = "add_task"
        mock_rtm_api.fail_message = "Server error 500"

        capture = make_approved_capture(
            db_session,
            clar_type="project",
            project_name="Big Project",
            project_shortname="BIGP",
            next_action="BIGP --- First step",
        )

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "unknown"

    def test_max_attempts_sets_permanently_failed(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        mock_rtm_api.fail_on = "create_timeline"
        mock_rtm_api.fail_message = "Connection error"

        capture = make_approved_capture(
            db_session,
            commit_attempt_count=rtm_commit.MAX_COMMIT_ATTEMPTS - 1,
        )

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "permanently_failed"
        assert capture.commit_attempt_count == rtm_commit.MAX_COMMIT_ATTEMPTS

    def test_missing_project_shortname_sets_failed(
        self, db_session, mock_rtm_api, mock_rtm_auth
    ):
        clar = {
            "type": "project",
            "project_name": "No shortname project",
            "project_shortname": "",
        }
        capture = make_capture(
            db_session,
            decision_status="approved",
            decision_at=utcnow_naive(),
            clarify_json=json.dumps(clar),
        )

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status == "failed"
        assert "Missing project_shortname" in capture.commit_error_message

    def test_missing_auth_token_raises_and_fails(
        self, db_session, mock_rtm_api, monkeypatch
    ):
        """No auth token → classified as retryable failure."""
        import app.rtm_auth as rtm_auth_mod
        monkeypatch.setattr(rtm_auth_mod, "get_rtm_auth", lambda: None)

        capture = make_approved_capture(db_session)

        rtm_commit._commit_one_capture(db_session, capture)

        db_session.refresh(capture)
        assert capture.commit_status in ("failed", "auth_failed")
        assert capture.commit_attempt_count == 1


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TestErrorClassification:

    def test_timeout_classified_as_unknown(self):
        status, msg = rtm_commit._classify_commit_error(TimeoutError("Request timed out"))
        assert status == "unknown"

    def test_auth_error_classified_as_auth_failed(self):
        status, msg = rtm_commit._classify_commit_error(RuntimeError("401 Unauthorized"))
        assert status == "auth_failed"

    def test_connection_error_classified_as_failed(self):
        status, msg = rtm_commit._classify_commit_error(ConnectionError("Connection refused"))
        assert status == "failed"

    def test_server_error_classified_as_failed(self):
        status, msg = rtm_commit._classify_commit_error(RuntimeError("500 Internal Server Error"))
        assert status == "failed"

    def test_unknown_error_defaults_to_failed(self):
        status, msg = rtm_commit._classify_commit_error(ValueError("Something weird"))
        assert status == "failed"

    def test_circuit_breaker_classified_as_failed(self):
        status, msg = rtm_commit._classify_commit_error(RuntimeError("Circuit breaker open"))
        assert status == "failed"
        assert "circuit breaker" in msg.lower()

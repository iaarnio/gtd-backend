"""Tests for app.rtm_commit — sync/commit logic (mock RTM)."""

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from app.models import Capture
from app.rtm_commit import (
    _build_smart_add,
    _classify_commit_error,
    _compute_commit_entries,
    _parse_json_maybe,
    _commit_one_capture,
)


class TestParseJsonMaybe:
    """Test _parse_json_maybe helper."""

    def test_valid_json_dict(self):
        result = _parse_json_maybe('{"type": "action"}')
        assert result == {"type": "action"}

    def test_valid_json_non_dict(self):
        """Non-dict JSON returns None."""
        result = _parse_json_maybe('"just a string"')
        assert result is None

    def test_invalid_json(self):
        result = _parse_json_maybe("not json at all")
        assert result is None

    def test_none_input(self):
        result = _parse_json_maybe(None)
        assert result is None

    def test_empty_string(self):
        result = _parse_json_maybe("")
        assert result is None


class TestBuildSmartAdd:
    """Test _build_smart_add helper."""

    def test_simple_task(self):
        result = _build_smart_add("Buy milk", include_na=False, due_date="", text_for_tags="buy milk")
        assert result == "Buy milk"

    def test_with_na_tag(self):
        result = _build_smart_add("Buy milk", include_na=True, due_date="", text_for_tags="buy milk")
        assert "#na" in result

    def test_with_due_date(self):
        result = _build_smart_add("Buy milk", include_na=False, due_date="2025-01-15", text_for_tags="buy milk")
        assert "^2025-01-15" in result

    def test_with_terveys_tag(self):
        result = _build_smart_add("Check", include_na=False, due_date="", text_for_tags="terveys check")
        assert "#terveys" in result

    def test_with_vero_tag(self):
        result = _build_smart_add("Tax", include_na=False, due_date="", text_for_tags="vero tax")
        assert "#vero" in result

    def test_with_joulu_tag(self):
        result = _build_smart_add("Gift", include_na=False, due_date="", text_for_tags="joulu gift")
        assert "#joulu" in result

    def test_combined_tags(self):
        result = _build_smart_add(
            "Task",
            include_na=True,
            due_date="2025-12-25",
            text_for_tags="terveys vero joulu",
        )
        assert "#na" in result
        assert "#terveys" in result
        assert "#vero" in result
        assert "#joulu" in result
        assert "^2025-12-25" in result


class TestComputeCommitEntries:
    """Test _compute_commit_entries for different capture types."""

    def test_next_action_type(self):
        clar = {
            "type": "next_action",
            "next_action": "Soita lääkärille",
            "clarified_text": "Soita lääkärille",
        }
        entries = _compute_commit_entries(clar)
        assert len(entries) == 1
        smart_add, task_name = entries[0]
        assert "Soita lääkärille" in smart_add

    def test_project_type(self):
        clar = {
            "type": "project",
            "project_name": "Uuden auton hankinta",
            "project_shortname": "AUTO",
            "next_action": "AUTO --- Selvitä budjetti",
            "clarified_text": "Uuden auton hankinta",
        }
        entries = _compute_commit_entries(clar)
        assert len(entries) == 2

        # First entry is the project task
        project_smart_add, project_name = entries[0]
        assert "AUTO - §§§ -" in project_name

        # Second entry is the first next action with #na
        action_smart_add, action_name = entries[1]
        assert "#na" in action_smart_add

    def test_project_missing_shortname_raises(self):
        clar = {
            "type": "project",
            "project_name": "Test",
            "project_shortname": "",
        }
        with pytest.raises(ValueError, match="project_shortname is required"):
            _compute_commit_entries(clar)

    def test_non_actionable_type(self):
        clar = {
            "type": "non_actionable",
            "clarified_text": "Muistiinpano",
        }
        entries = _compute_commit_entries(clar)
        assert len(entries) == 1

    def test_project_default_next_action(self):
        """If no next_action provided for project, default is generated."""
        clar = {
            "type": "project",
            "project_name": "Test project",
            "project_shortname": "TEST",
            "next_action": "",
            "clarified_text": "Test project",
        }
        entries = _compute_commit_entries(clar)
        assert len(entries) == 2
        _, action_name = entries[1]
        assert "TEST" in action_name
        assert "ensimmäinen next action" in action_name.lower()


class TestClassifyCommitError:
    """Test _classify_commit_error."""

    def test_timeout_error(self):
        exc = TimeoutError("Connection timed out")
        status, msg = _classify_commit_error(exc)
        assert status == "unknown"
        assert "Timeout" in msg or "timeout" in msg.lower()

    def test_auth_error(self):
        exc = RuntimeError("401 Unauthorized")
        status, msg = _classify_commit_error(exc)
        assert status == "auth_failed"

    def test_connection_error(self):
        exc = RuntimeError("connection refused by server")
        status, msg = _classify_commit_error(exc)
        assert status == "failed"

    def test_circuit_breaker_error(self):
        exc = RuntimeError("Circuit breaker is OPEN")
        status, msg = _classify_commit_error(exc)
        assert status == "failed"
        assert "circuit" in msg.lower()

    def test_generic_error(self):
        exc = RuntimeError("Something went wrong")
        status, msg = _classify_commit_error(exc)
        assert status == "failed"


class TestCommitOneCapture:
    """Test _commit_one_capture with mocked RTM API."""

    def test_successful_commit(self, db_session, mock_rtm_api, mock_rtm_auth):
        """Approved capture with clarification should commit successfully."""
        clar = json.dumps({
            "type": "next_action",
            "next_action": "Soita lääkärille",
            "clarified_text": "Soita lääkärille",
            "confidence_score": 0.9,
        })
        c = Capture(
            raw_text="Call doctor",
            source="test",
            decision_status="approved",
            clarify_json=clar,
            clarify_status="completed",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        _commit_one_capture(db_session, c)

        db_session.refresh(c)
        assert c.commit_status == "committed"
        assert c.rtm_task_id is not None
        assert c.commit_attempt_count == 1

    def test_commit_with_notes(self, db_session, mock_rtm_api, mock_rtm_auth):
        """Commit should add notes to the RTM task when present."""
        clar = json.dumps({
            "type": "next_action",
            "next_action": "Task with notes",
            "clarified_text": "Task with notes",
            "notes": "Important context here",
            "confidence_score": 0.9,
        })
        c = Capture(
            raw_text="Task with notes",
            source="test",
            decision_status="approved",
            clarify_json=clar,
            clarify_status="completed",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        _commit_one_capture(db_session, c)

        db_session.refresh(c)
        assert c.commit_status == "committed"

        # Verify add_note was called
        note_calls = [call for call in mock_rtm_api.calls if call[0] == "add_note"]
        assert len(note_calls) == 1

    def test_commit_project_creates_two_tasks(self, db_session, mock_rtm_api, mock_rtm_auth):
        """Project commit should create project + next action tasks."""
        clar = json.dumps({
            "type": "project",
            "project_name": "Auto purchase",
            "project_shortname": "AUTO",
            "next_action": "AUTO --- Research budget",
            "clarified_text": "Auto purchase",
            "confidence_score": 0.9,
        })
        c = Capture(
            raw_text="Buy car",
            source="test",
            decision_status="approved",
            clarify_json=clar,
            clarify_status="completed",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        _commit_one_capture(db_session, c)

        db_session.refresh(c)
        assert c.commit_status == "committed"

        # Verify two add_task calls
        add_calls = [call for call in mock_rtm_api.calls if call[0] == "add_task"]
        assert len(add_calls) == 2

    def test_commit_failure_marks_failed(self, db_session, mock_rtm_api, mock_rtm_auth):
        """RTM failure should mark capture as failed."""
        mock_rtm_api.fail_on = "create_timeline"

        clar = json.dumps({
            "type": "next_action",
            "next_action": "Test",
            "clarified_text": "Test",
            "confidence_score": 0.9,
        })
        c = Capture(
            raw_text="Will fail",
            source="test",
            decision_status="approved",
            clarify_json=clar,
            clarify_status="completed",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        _commit_one_capture(db_session, c)

        db_session.refresh(c)
        assert c.commit_status in ("failed", "unknown")
        assert c.commit_error_message is not None
        assert c.commit_attempt_count == 1

    def test_project_missing_shortname_fails(self, db_session, mock_rtm_api, mock_rtm_auth):
        """Project without shortname should fail gracefully."""
        clar = json.dumps({
            "type": "project",
            "project_name": "Test",
            "project_shortname": "",
            "confidence_score": 0.9,
        })
        c = Capture(
            raw_text="Project no shortname",
            source="test",
            decision_status="approved",
            clarify_json=clar,
            clarify_status="completed",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        _commit_one_capture(db_session, c)

        db_session.refresh(c)
        assert c.commit_status == "failed"
        assert "project_shortname" in c.commit_error_message.lower()

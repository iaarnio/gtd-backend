"""Tests for app.schemas — Pydantic validation."""

import pytest
from pydantic import ValidationError

from app.schemas import CaptureCreate, CaptureOut, ClarificationUpdate
from app.time_utils import utcnow_naive


class TestCaptureCreate:
    """CaptureCreate validation tests."""

    def test_valid_minimal(self):
        payload = CaptureCreate(raw_text="Buy milk", source="test")
        assert payload.raw_text == "Buy milk"
        assert payload.source == "test"
        assert payload.source_id is None
        assert payload.source_link is None

    def test_valid_full(self):
        payload = CaptureCreate(
            raw_text="Buy milk",
            source="email",
            source_id="msg-123",
            source_link="https://example.com",
        )
        assert payload.source_id == "msg-123"
        assert payload.source_link == "https://example.com"

    def test_missing_raw_text(self):
        with pytest.raises(ValidationError):
            CaptureCreate(source="test")

    def test_missing_source(self):
        with pytest.raises(ValidationError):
            CaptureCreate(raw_text="Buy milk")

    def test_empty_raw_text_allowed(self):
        """Pydantic str allows empty strings by default."""
        payload = CaptureCreate(raw_text="", source="test")
        assert payload.raw_text == ""


class TestCaptureOut:
    """CaptureOut validation tests."""

    def test_valid_minimal(self):
        out = CaptureOut(
            id=1,
            created_at=utcnow_naive(),
            raw_text="Test",
            source="test",
            decision_status="proposed",
        )
        assert out.id == 1
        assert out.decision_status == "proposed"

    def test_optional_fields_default_none(self):
        out = CaptureOut(
            id=1,
            created_at=utcnow_naive(),
            raw_text="Test",
            source="test",
            decision_status="proposed",
        )
        assert out.source_id is None
        assert out.source_link is None
        assert out.clarify_json is None
        assert out.decision_at is None
        assert out.decision_notes is None
        assert out.external_commit_state is None

    def test_from_attributes(self):
        """CaptureOut should work with from_attributes=True (ORM mode)."""
        class FakeCapture:
            id = 1
            created_at = utcnow_naive()
            raw_text = "Test"
            source = "test"
            source_id = None
            source_link = None
            clarify_json = None
            decision_status = "proposed"
            decision_at = None
            decision_notes = None
            external_commit_state = None

        out = CaptureOut.model_validate(FakeCapture())
        assert out.id == 1


class TestClarificationUpdate:
    """ClarificationUpdate validation tests."""

    def test_valid_dict_data(self):
        payload = ClarificationUpdate(data={"type": "action", "clarified_text": "Test"})
        assert payload.data["type"] == "action"

    def test_valid_any_data(self):
        """data field is Any, so it accepts various types."""
        payload = ClarificationUpdate(data="just a string")
        assert payload.data == "just a string"

        payload2 = ClarificationUpdate(data=[1, 2, 3])
        assert payload2.data == [1, 2, 3]

    def test_missing_data(self):
        with pytest.raises(ValidationError):
            ClarificationUpdate()

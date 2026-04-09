"""Tests for app.clarification — AI clarification engine (mock LLM)."""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.clarification import (
    _build_user_prompt,
    _clarify_capture,
    _poll_once,
    _should_retry_clarification,
)
from app.models import Capture


class TestBuildUserPrompt:
    """Test user prompt construction."""

    def test_prompt_contains_raw_text(self):
        prompt = _build_user_prompt("Buy milk from store")
        assert "Buy milk from store" in prompt

    def test_prompt_has_instruction(self):
        prompt = _build_user_prompt("Test")
        assert "JSON" in prompt


class TestShouldRetryClarification:
    """Test retry logic with backoff."""

    def test_pending_should_retry(self):
        c = Capture(raw_text="Test", source="test", clarify_status="pending")
        assert _should_retry_clarification(c, datetime.utcnow()) is True

    def test_completed_should_not_retry(self):
        c = Capture(raw_text="Test", source="test", clarify_status="completed")
        assert _should_retry_clarification(c, datetime.utcnow()) is False

    def test_failed_first_retry_immediate(self):
        """First retry (attempt_count=0, so next attempt=1) should happen immediately."""
        now = datetime.utcnow()
        c = Capture(
            raw_text="Test",
            source="test",
            clarify_status="failed",
            clarify_attempt_count=0,
            last_clarify_attempt_at=now - timedelta(seconds=1),
        )
        assert _should_retry_clarification(c, now) is True

    def test_failed_second_retry_needs_delay(self):
        """Second retry (attempt_count=1, next=2) needs 5 minutes delay."""
        now = datetime.utcnow()
        c = Capture(
            raw_text="Test",
            source="test",
            clarify_status="failed",
            clarify_attempt_count=1,
            last_clarify_attempt_at=now - timedelta(seconds=10),
        )
        # attempt_count=1 → next_attempt_number=2 → delay=300s, only 10s passed
        assert _should_retry_clarification(c, now) is False

    def test_failed_second_retry_after_delay(self):
        """Second retry (attempt_count=1) after 5 min delay should proceed."""
        now = datetime.utcnow()
        c = Capture(
            raw_text="Test",
            source="test",
            clarify_status="failed",
            clarify_attempt_count=1,
            last_clarify_attempt_at=now - timedelta(seconds=301),
        )
        assert _should_retry_clarification(c, now) is True

    def test_max_attempts_exceeded(self):
        """After max attempts, should not retry."""
        now = datetime.utcnow()
        c = Capture(
            raw_text="Test",
            source="test",
            clarify_status="failed",
            clarify_attempt_count=5,
            last_clarify_attempt_at=now - timedelta(hours=24),
        )
        assert _should_retry_clarification(c, now) is False

    def test_failed_no_last_attempt_retries(self):
        """Failed with no last_clarify_attempt_at should retry."""
        c = Capture(
            raw_text="Test",
            source="test",
            clarify_status="failed",
            clarify_attempt_count=1,
            last_clarify_attempt_at=None,
        )
        assert _should_retry_clarification(c, datetime.utcnow()) is True


class TestClarifyCapture:
    """Test _clarify_capture with mocked LLM."""

    def test_successful_clarification(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Buy milk", source="test")
        c.id = 1

        llm_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "type": "action",
                            "clarified_text": "Osta maitoa",
                            "project_name": None,
                            "project_shortname": None,
                            "next_action": "Osta maitoa kaupasta",
                            "suggested_context": "@kauppa",
                            "due_date": None,
                            "notes": None,
                            "ambiguities": None,
                            "suggested_tool": "RTM",
                            "confidence_score": 0.9,
                        })
                    }
                }
            ]
        }

        with patch("app.clarification._call_llm_api", return_value=llm_response):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is not None
        parsed = json.loads(result)
        assert parsed["type"] == "action"
        assert parsed["confidence_score"] == 0.9

    def test_clarification_returns_none_on_api_error(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Test", source="test")
        c.id = 1

        import requests
        with patch("app.clarification._call_llm_api", side_effect=requests.exceptions.ConnectionError("Connection refused")):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is None

    def test_clarification_returns_none_on_invalid_json(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Test", source="test")
        c.id = 1

        llm_response = {
            "choices": [{"message": {"content": "not valid json"}}]
        }

        with patch("app.clarification._call_llm_api", return_value=llm_response):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is None

    def test_clarification_returns_none_missing_confidence(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Test", source="test")
        c.id = 1

        # Missing confidence_score
        llm_response = {
            "choices": [
                {"message": {"content": json.dumps({"type": "action", "clarified_text": "Test"})}}
            ]
        }

        with patch("app.clarification._call_llm_api", return_value=llm_response):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is None

    def test_clarification_returns_none_on_llm_error(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Test", source="test")
        c.id = 1

        llm_response = {"error": {"message": "Rate limited", "type": "rate_limit"}}

        with patch("app.clarification._call_llm_api", return_value=llm_response):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is None

    def test_clarification_returns_none_on_empty_content(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        c = Capture(raw_text="Test", source="test")
        c.id = 1

        llm_response = {"choices": [{"message": {"content": ""}}]}

        with patch("app.clarification._call_llm_api", return_value=llm_response):
            result = _clarify_capture("test-key", "https://api.example.com", c)

        assert result is None


class TestPollOnce:
    """Test the clarification poll cycle."""

    def test_poll_skips_without_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # Should not raise, just return
        _poll_once()

    def test_poll_skips_without_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        _poll_once()

    def test_poll_processes_pending_captures(self, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")

        # Create a pending capture
        c = Capture(raw_text="Test capture", source="test", clarify_status="pending")
        db_session.add(c)
        db_session.commit()

        llm_result = json.dumps({
            "type": "action",
            "clarified_text": "Test",
            "confidence_score": 0.8,
        })

        # Mock SessionLocal to return our test session, and mock the LLM call
        # Patch close() so the session stays usable for post-poll assertions
        with patch("app.clarification.SessionLocal", return_value=db_session), \
             patch("app.clarification._clarify_capture", return_value=llm_result), \
             patch.object(db_session, "close"):
            _poll_once()

        c = db_session.query(Capture).filter_by(id=c.id).first()
        assert c.clarify_status == "completed"
        assert c.clarify_json is not None

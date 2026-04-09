"""Tests for app.rtm — RTM API client (mock HTTP)."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.rtm import (
    _sign_params,
    add_note,
    add_task,
    call,
    create_timeline,
    is_configured,
)


class TestSignParams:
    """Test RTM parameter signing."""

    def test_sign_params_basic(self):
        """Verify signing produces correct md5 hash."""
        secret = "testsecret"
        params = {"api_key": "abc", "method": "rtm.test.echo"}
        sig = _sign_params(secret, params)

        # Manually compute expected signature
        raw = "testsecret" + "api_key" + "abc" + "method" + "rtm.test.echo"
        expected = hashlib.md5(raw.encode("utf-8")).hexdigest()
        assert sig == expected

    def test_sign_params_sorted_keys(self):
        """Keys should be sorted for consistent signing."""
        secret = "s"
        params_a = {"z": "1", "a": "2"}
        params_b = {"a": "2", "z": "1"}
        assert _sign_params(secret, params_a) == _sign_params(secret, params_b)

    def test_sign_params_empty(self):
        """Empty params should still produce a valid hash."""
        sig = _sign_params("secret", {})
        expected = hashlib.md5("secret".encode("utf-8")).hexdigest()
        assert sig == expected


class TestIsConfigured:
    """Test RTM configuration check."""

    def test_configured_when_both_set(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")
        assert is_configured() is True

    def test_not_configured_missing_key(self, monkeypatch):
        monkeypatch.delenv("RTM_API_KEY", raising=False)
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")
        assert is_configured() is False

    def test_not_configured_missing_secret(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.delenv("RTM_SHARED_SECRET", raising=False)
        assert is_configured() is False

    def test_not_configured_both_missing(self, monkeypatch):
        monkeypatch.delenv("RTM_API_KEY", raising=False)
        monkeypatch.delenv("RTM_SHARED_SECRET", raising=False)
        assert is_configured() is False


class TestCallFunction:
    """Test the main call() function."""

    def test_call_not_configured_raises(self, monkeypatch):
        monkeypatch.delenv("RTM_API_KEY", raising=False)
        monkeypatch.delenv("RTM_SHARED_SECRET", raising=False)
        monkeypatch.delenv("RTM_AUTH_TOKEN", raising=False)

        with pytest.raises(RuntimeError, match="RTM is not configured"):
            call("rtm.test.echo", {})

    def test_call_timeline_create(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="ok"><timeline>12345</timeline></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            result = call("rtm.timelines.create", {}, auth_token="token")
            assert result == {"timeline": "12345"}

    def test_call_non_timeline_returns_raw(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="ok"><list id="1"><taskseries id="2"><task id="3"/></taskseries></list></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            result = call("rtm.tasks.add", {"timeline": "123", "name": "Test"}, auth_token="token")
            assert "raw" in result


class TestCreateTimeline:
    """Test create_timeline."""

    def test_create_timeline_success(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="ok"><timeline>99999</timeline></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            timeline = create_timeline(auth_token="token")
            assert timeline == "99999"


class TestAddTask:
    """Test add_task."""

    def test_add_task_success(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = (
            '<rsp stat="ok">'
            '<list id="list-1">'
            '<taskseries id="ts-1">'
            '<task id="task-1"/>'
            '</taskseries>'
            '</list>'
            '</rsp>'
        )
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            result = add_task(timeline="123", name="Test task", auth_token="token")
            assert result["list_id"] == "list-1"
            assert result["taskseries_id"] == "ts-1"
            assert result["task_id"] == "task-1"

    def test_add_task_rtm_error(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="fail"><err code="100" msg="Invalid API key"/></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            with pytest.raises(RuntimeError, match="RTM task add failed"):
                add_task(timeline="123", name="Test", auth_token="token")


class TestAddNote:
    """Test add_note."""

    def test_add_note_success(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="ok"><note id="note-1"/></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            # Should not raise
            add_note(
                timeline="123",
                list_id="l-1",
                taskseries_id="ts-1",
                task_id="t-1",
                note_title="Note",
                note_text="Some text",
                auth_token="token",
            )

    def test_add_note_rtm_error(self, monkeypatch):
        monkeypatch.setenv("RTM_API_KEY", "key")
        monkeypatch.setenv("RTM_SHARED_SECRET", "secret")

        xml_response = '<rsp stat="fail"><err code="300" msg="Note error"/></rsp>'
        with patch("app.rtm._call_rtm_api", return_value=xml_response):
            with pytest.raises(RuntimeError, match="RTM notes.add failed"):
                add_note(
                    timeline="123",
                    list_id="l-1",
                    taskseries_id="ts-1",
                    task_id="t-1",
                    note_title="Note",
                    note_text="Text",
                    auth_token="token",
                )

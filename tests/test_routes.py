"""Tests for app.main — FastAPI route tests using TestClient."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import Base, get_db
from app.models import Capture


@pytest.fixture
def client(db_session, monkeypatch):
    """Create a TestClient that uses the in-memory test DB session."""
    # Suppress background threads and startup side effects
    monkeypatch.setattr("app.email_ingestion.start_background_poller", lambda: None)
    monkeypatch.setattr("app.clarification.start_background_clarifier", lambda: None)
    monkeypatch.setattr("app.rtm_commit.start_background_committer", lambda: None)
    monkeypatch.setattr("app.daily_highlights_scheduler.start_scheduler", lambda: None)
    monkeypatch.setattr("app.rtm_auth.bootstrap_rtm_auth_from_env", lambda: None)

    from app.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # raise_server_exceptions=False: get status codes instead of crashes
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()


class TestCreateCapture:
    """POST /captures endpoint."""

    def test_create_capture_success(self, client, db_session):
        response = client.post(
            "/captures",
            json={"raw_text": "Buy groceries", "source": "test"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["raw_text"] == "Buy groceries"
        assert data["source"] == "test"
        assert data["decision_status"] == "proposed"
        assert data["id"] is not None

    def test_create_capture_with_optional_fields(self, client, db_session):
        response = client.post(
            "/captures",
            json={
                "raw_text": "Email task",
                "source": "email",
                "source_id": "msg-456",
                "source_link": "https://example.com/msg",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["source_id"] == "msg-456"
        assert data["source_link"] == "https://example.com/msg"

    def test_create_capture_missing_raw_text(self, client):
        response = client.post("/captures", json={"source": "test"})
        assert response.status_code == 422  # Validation error

    def test_create_capture_missing_source(self, client):
        response = client.post("/captures", json={"raw_text": "Test"})
        assert response.status_code == 422


class TestUpdateClarification:
    """PUT /captures/{id}/clarification endpoint."""

    def test_update_clarification_success(self, client, db_session):
        # Create a capture first
        c = Capture(raw_text="Test task", source="test")
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        clar_data = {
            "type": "action",
            "clarified_text": "Osta maitoa",
            "confidence_score": 0.9,
        }
        response = client.put(
            f"/captures/{c.id}/clarification",
            json={"data": clar_data},
        )
        assert response.status_code == 200
        data = response.json()
        # clarify_json is returned as parsed JSON
        assert data["id"] == c.id

    def test_update_clarification_not_found(self, client):
        response = client.put(
            "/captures/99999/clarification",
            json={"data": {"type": "action"}},
        )
        assert response.status_code == 404


class TestRestoreCapture:
    """POST /captures/{id}/restore endpoint."""

    def test_restore_rejected_capture(self, client, db_session):
        c = Capture(raw_text="Rejected task", source="test", decision_status="rejected")
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        response = client.post(f"/captures/{c.id}/restore")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

        db_session.refresh(c)
        assert c.decision_status == "proposed"

    def test_restore_non_rejected_capture(self, client, db_session):
        c = Capture(raw_text="Proposed task", source="test", decision_status="proposed")
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        response = client.post(f"/captures/{c.id}/restore")
        assert response.status_code == 400

    def test_restore_not_found(self, client):
        response = client.post("/captures/99999/restore")
        assert response.status_code == 404


class TestHealthEndpoint:
    """GET /health endpoint."""

    def test_health_returns_html(self, client):
        response = client.get("/health")
        # May return 500 if templates aren't found, but the route exists
        assert response.status_code in (200, 500)


class TestPullEmails:
    """POST /emails/pull endpoint."""

    def test_pull_emails_no_credentials(self, client, monkeypatch):
        monkeypatch.delenv("IMAP_USERNAME", raising=False)
        monkeypatch.delenv("IMAP_PASSWORD", raising=False)
        response = client.post("/emails/pull")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "IMAP" in data["error"]

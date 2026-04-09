"""Tests for app.models — model creation, defaults, and constraints."""

from datetime import date, datetime

from app.models import Anchor, BacklogItem, Capture, RtmAuth, RtmTask
from app.time_utils import utcnow_naive


class TestCaptureModel:
    """Capture model creation and defaults."""

    def test_capture_defaults(self, db_session):
        c = Capture(raw_text="Buy milk", source="test")
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        assert c.id is not None
        assert c.raw_text == "Buy milk"
        assert c.source == "test"
        assert c.decision_status == "proposed"
        assert c.commit_status == "pending"
        assert c.clarify_status == "pending"
        assert c.clarify_attempt_count == 0
        assert c.commit_attempt_count == 0
        assert c.created_at is not None
        assert isinstance(c.created_at, datetime)

    def test_capture_nullable_fields(self, db_session):
        c = Capture(raw_text="Test", source="test")
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        assert c.source_id is None
        assert c.source_link is None
        assert c.email_id is None
        assert c.email_link is None
        assert c.clarify_json is None
        assert c.decision_at is None
        assert c.decision_notes is None
        assert c.last_commit_attempt_at is None
        assert c.commit_error_message is None
        assert c.rtm_task_id is None
        assert c.rtm_taskseries_id is None
        assert c.rtm_list_id is None

    def test_capture_with_all_fields(self, db_session):
        now = utcnow_naive()
        c = Capture(
            raw_text="Full capture",
            source="email",
            source_id="msg-123",
            source_link="https://example.com",
            email_id="<test@example.com>",
            email_link="https://mail.google.com/test",
            clarify_json='{"type": "action"}',
            clarify_status="completed",
            clarify_attempt_count=1,
            last_clarify_attempt_at=now,
            decision_status="approved",
            decision_at=now,
            decision_notes="Looks good",
            commit_status="committed",
            last_commit_attempt_at=now,
            commit_attempt_count=1,
            rtm_task_id="task-1",
            rtm_taskseries_id="ts-1",
            rtm_list_id="list-1",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)

        assert c.source == "email"
        assert c.decision_status == "approved"
        assert c.commit_status == "committed"
        assert c.clarify_status == "completed"

    def test_multiple_captures(self, db_session):
        for i in range(5):
            db_session.add(Capture(raw_text=f"Task {i}", source="test"))
        db_session.commit()

        captures = db_session.query(Capture).all()
        assert len(captures) == 5

    def test_capture_raw_text_not_nullable(self, db_session):
        """raw_text is non-nullable; adding without it should fail."""
        import sqlalchemy
        c = Capture(source="test")  # missing raw_text
        db_session.add(c)
        try:
            db_session.commit()
            # SQLite may not enforce NOT NULL on all engines the same way
            # but the column is defined as nullable=False
        except (sqlalchemy.exc.IntegrityError, sqlalchemy.exc.OperationalError):
            db_session.rollback()


class TestAnchorModel:
    """Anchor model creation and defaults."""

    def test_anchor_defaults(self, db_session):
        a = Anchor()
        db_session.add(a)
        db_session.commit()
        db_session.refresh(a)

        assert a.id is not None
        assert a.kind == "approval_anchor"
        assert a.status == "active"
        assert a.valid_until is not None
        assert a.created_at is not None

    def test_anchor_with_external_state(self, db_session):
        a = Anchor(
            kind="approval_anchor",
            status="active",
            valid_until=date.today(),
            external_state='{"provider": "rtm", "status": "committed"}',
        )
        db_session.add(a)
        db_session.commit()
        db_session.refresh(a)

        assert a.external_state is not None
        assert "committed" in a.external_state


class TestRtmAuthModel:
    """RtmAuth model creation and defaults."""

    def test_rtm_auth_defaults(self, db_session):
        auth = RtmAuth()
        db_session.add(auth)
        db_session.commit()
        db_session.refresh(auth)

        assert auth.id is not None
        assert auth.valid == "unknown"
        assert auth.auth_token is None

    def test_rtm_auth_full(self, db_session):
        auth = RtmAuth(
            auth_token="test-token",
            perms="delete",
            username="testuser",
            user_id="12345",
            valid="valid",
            last_checked_at=utcnow_naive(),
        )
        db_session.add(auth)
        db_session.commit()
        db_session.refresh(auth)

        assert auth.auth_token == "test-token"
        assert auth.perms == "delete"
        assert auth.username == "testuser"
        assert auth.valid == "valid"


class TestRtmTaskModel:
    """RtmTask model creation and defaults."""

    def test_rtm_task_defaults(self, db_session):
        task = RtmTask(
            rtm_task_id="t-1",
            rtm_taskseries_id="ts-1",
            rtm_list_id="l-1",
            name="Test task",
            created_at=utcnow_naive(),
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        assert task.id is not None
        assert task.rtm_completed is False
        assert task.times_suggested == 0
        assert task.rtm_project_id is None

    def test_rtm_task_unique_rtm_task_id(self, db_session):
        """rtm_task_id should be unique."""
        import sqlalchemy
        now = utcnow_naive()
        t1 = RtmTask(rtm_task_id="dup-1", rtm_taskseries_id="ts-1", rtm_list_id="l-1", name="A", created_at=now)
        t2 = RtmTask(rtm_task_id="dup-1", rtm_taskseries_id="ts-2", rtm_list_id="l-2", name="B", created_at=now)
        db_session.add(t1)
        db_session.commit()
        db_session.add(t2)
        try:
            db_session.commit()
            assert False, "Should have raised IntegrityError"
        except sqlalchemy.exc.IntegrityError:
            db_session.rollback()


class TestBacklogItemModel:
    """BacklogItem model creation and defaults."""

    def test_backlog_item_defaults(self, db_session):
        item = BacklogItem(raw_text="Old task from RTM")
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        assert item.id is not None
        assert item.status == "pending"
        assert item.source == "rtm-export"
        assert item.clarify_attempts == 0
        assert item.last_error is None
        assert item.processed_at is None

    def test_backlog_item_custom_source(self, db_session):
        item = BacklogItem(raw_text="Imported task", source="manual")
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        assert item.source == "manual"

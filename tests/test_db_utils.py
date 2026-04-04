"""Tests for app.db_utils — transactional_session context manager."""

import pytest
from sqlalchemy import text

from app.db_utils import transactional_session
from app.models import Capture


class TestTransactionalSession:
    """Tests for the transactional_session context manager."""

    def test_auto_commit_on_success(self, db_session):
        """Session should auto-commit when context exits normally."""
        c = Capture(raw_text="Auto commit test", source="test")
        db_session.add(c)
        with transactional_session(db_session):
            pass  # Context manager handles commit

        # Verify it was committed
        result = db_session.query(Capture).filter_by(raw_text="Auto commit test").first()
        assert result is not None
        assert result.source == "test"

    def test_auto_rollback_on_exception(self, db_session):
        """Session should rollback when an exception occurs."""
        c = Capture(raw_text="Rollback test", source="test")
        db_session.add(c)

        with pytest.raises(ValueError):
            with transactional_session(db_session):
                raise ValueError("Intentional error")

        # The capture should not be committed (rollback happened)
        result = db_session.query(Capture).filter_by(raw_text="Rollback test").first()
        assert result is None

    def test_multiple_adds_in_transaction(self, db_session):
        """Multiple adds should all commit together."""
        db_session.add(Capture(raw_text="T1", source="test"))
        db_session.add(Capture(raw_text="T2", source="test"))
        db_session.add(Capture(raw_text="T3", source="test"))
        with transactional_session(db_session):
            pass

        count = db_session.query(Capture).count()
        assert count == 3

    def test_nested_usage(self, db_session):
        """Using transactional_session for sequential operations."""
        c1 = Capture(raw_text="First", source="test")
        db_session.add(c1)
        with transactional_session(db_session):
            pass

        c2 = Capture(raw_text="Second", source="test")
        db_session.add(c2)
        with transactional_session(db_session):
            pass

        count = db_session.query(Capture).count()
        assert count == 2

    def test_commit_persists_field_updates(self, db_session):
        """Field updates should be committed."""
        c = Capture(raw_text="Update me", source="test")
        db_session.add(c)
        with transactional_session(db_session):
            pass

        c.decision_status = "approved"
        db_session.add(c)
        with transactional_session(db_session):
            pass

        db_session.refresh(c)
        assert c.decision_status == "approved"

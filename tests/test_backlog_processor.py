import json
from datetime import datetime, timedelta

from app import backlog_processor, models


def test_nightly_backlog_drain_processes_oldest_pending_items_first(db_session, monkeypatch):
    old_item = models.BacklogItem(
        raw_text="old task",
        status="pending",
        imported_at=datetime.utcnow() - timedelta(days=2),
    )
    new_item = models.BacklogItem(
        raw_text="new task",
        status="pending",
        imported_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add_all([old_item, new_item])
    db_session.commit()

    processed_ids = []

    def fake_clarify(_db, item):
        processed_ids.append(item.id)
        return json.dumps({"type": "next_action", "clarified_text": item.raw_text})

    monkeypatch.setattr(backlog_processor, "_clarify_backlog_item", fake_clarify)

    result = backlog_processor.nightly_backlog_drain(db_session)

    db_session.refresh(old_item)
    db_session.refresh(new_item)

    assert result["processed"] == 2
    assert result["failed"] == 0
    assert processed_ids == [old_item.id, new_item.id]
    assert old_item.status == "processed"
    assert new_item.status == "processed"
    captures = db_session.query(models.Capture).order_by(models.Capture.source_id.asc()).all()
    assert [c.source_id for c in captures] == [f"backlog-{old_item.id}", f"backlog-{new_item.id}"]


def test_process_backlog_item_requeues_when_clarification_returns_none(db_session, monkeypatch):
    item = models.BacklogItem(raw_text="needs retry", status="pending", clarify_attempts=0)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    monkeypatch.setattr(backlog_processor, "_clarify_backlog_item", lambda _db, _item: None)

    backlog_processor._process_backlog_item(db_session, item)
    db_session.refresh(item)

    assert item.status == "pending"
    assert item.clarify_attempts == 1
    assert db_session.query(models.Capture).count() == 0


def test_process_backlog_item_marks_failed_after_max_attempts(db_session, monkeypatch):
    item = models.BacklogItem(
        raw_text="will fail",
        status="pending",
        clarify_attempts=backlog_processor.MAX_CLARIFY_ATTEMPTS - 1,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    monkeypatch.setattr(backlog_processor, "_clarify_backlog_item", lambda _db, _item: None)

    backlog_processor._process_backlog_item(db_session, item)
    db_session.refresh(item)

    assert item.status == "failed"
    assert item.clarify_attempts == backlog_processor.MAX_CLARIFY_ATTEMPTS
    assert item.last_error == "Max clarification attempts reached"

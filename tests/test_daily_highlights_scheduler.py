from datetime import date, datetime

import pytest

from app import daily_highlights_scheduler


def test_has_job_window_opened_before_and_after_schedule():
    today = date(2026, 4, 9)

    assert daily_highlights_scheduler._has_job_window_opened(
        today, datetime(2026, 4, 9, 1, 59), 2, 0
    ) is False
    assert daily_highlights_scheduler._has_job_window_opened(
        today, datetime(2026, 4, 9, 2, 0), 2, 0
    ) is True
    assert daily_highlights_scheduler._has_job_window_opened(
        today, datetime(2026, 4, 9, 3, 15), 2, 0
    ) is True


def test_run_background_scheduler_catchup_runs_highlights_and_backlog_once(monkeypatch):
    calls = []
    fixed_now = datetime(2026, 4, 9, 5, 0, 0)

    class FakeDateTime:
        min = datetime.min

        @classmethod
        def utcnow(cls):
            return fixed_now

        @classmethod
        def combine(cls, a_date, a_time):
            return datetime.combine(a_date, a_time)

    class DummySession:
        def close(self):
            pass

    monkeypatch.setattr(daily_highlights_scheduler, "datetime", FakeDateTime)
    monkeypatch.setattr(daily_highlights_scheduler.Config, "HIGHLIGHTS_RUN_HOUR", 2)
    monkeypatch.setattr(daily_highlights_scheduler.Config, "HIGHLIGHTS_RUN_MINUTE", 0)
    monkeypatch.setattr(daily_highlights_scheduler, "Session", lambda bind: DummySession())
    monkeypatch.setattr(daily_highlights_scheduler.daily_highlights, "run_daily_highlights", lambda db: calls.append("highlights") or {"status": "ok"})

    import app.backlog_processor as backlog_processor
    import app.db as db_module

    monkeypatch.setattr(backlog_processor, "nightly_backlog_drain", lambda db: calls.append("backlog") or {"status": "ok"})
    monkeypatch.setattr(db_module, "engine", object())

    def stop_after_first_sleep(_seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(daily_highlights_scheduler.time, "sleep", stop_after_first_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        daily_highlights_scheduler.run_background_scheduler()

    assert calls == ["highlights", "backlog"]

import json
from types import SimpleNamespace

from app import main, models


def test_suggest_next_action_prefers_explicit_next_action():
    clar = {
        "type": "project",
        "project_name": "Project name",
        "project_shortname": "PRJ",
        "next_action": "PRJ --- Do the thing",
    }
    assert main._suggest_next_action(clar) == "PRJ --- Do the thing"


def test_suggest_next_action_builds_project_prefill_from_shortname():
    clar = {
        "type": "project",
        "project_name": "Resistentin tärkkelyksen lisääminen ruokavalioon",
        "project_shortname": "RTARK",
    }
    assert main._suggest_next_action(clar) == "RTARK --- Resistentin tärkkelyksen lisääminen ruokavalioon"


def test_suggest_next_action_avoids_double_prefix():
    clar = {
        "type": "project",
        "clarified_text": "RTARK --- Existing prefix",
        "project_shortname": "RTARK",
    }
    assert main._suggest_next_action(clar) == "RTARK --- Existing prefix"


def test_approval_detail_passes_prefill_to_template(db_session, monkeypatch):
    capture = models.Capture(
        raw_text="raw",
        source="test",
        clarify_json=json.dumps(
            {
                "type": "project",
                "project_name": "Project name",
                "project_shortname": "PRJ",
            }
        ),
    )
    db_session.add(capture)
    db_session.commit()
    db_session.refresh(capture)

    class _Templates:
        @staticmethod
        def TemplateResponse(*args):
            return args[-1]

    monkeypatch.setattr(main, "templates", _Templates())

    context = main.approval_detail(capture.id, SimpleNamespace(), db_session)
    assert context["next_action_prefill"] == "PRJ --- Project name"

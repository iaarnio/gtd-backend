import asyncio
import json
from types import SimpleNamespace

from app import main, models


class _FakeRequest:
    def __init__(self, form_data):
        self._form_data = form_data

    async def form(self):
        return self._form_data


def test_na_checkbox_overrides_project_type_on_save(db_session):
    capture = models.Capture(
        raw_text="foo",
        source="test",
        decision_status="proposed",
        clarify_json=json.dumps(
            {
                "type": "project",
                "project_name": "Original project",
                "project_shortname": "ORIG",
            }
        ),
    )
    db_session.add(capture)
    db_session.commit()
    db_session.refresh(capture)

    request = _FakeRequest(
        {
            "project_name": "Project should not win",
            "project_shortname": "PRJ",
            "next_action": "PRJ --- first action",
            "is_next_action": "on",
        }
    )
    asyncio.run(main.approval_update_clarification(capture.id, request, db_session))

    updated = db_session.get(models.Capture, capture.id)
    clar = json.loads(updated.clarify_json)
    assert clar["type"] == "next_action"
    assert clar["clarified_text"] == "PRJ --- first action"


def test_approvals_list_contains_only_proposed(db_session, monkeypatch):
    proposed = models.Capture(raw_text="p", source="test", decision_status="proposed")
    approved = models.Capture(raw_text="a", source="test", decision_status="approved")
    db_session.add_all([proposed, approved])
    db_session.commit()

    class _Templates:
        @staticmethod
        def TemplateResponse(_template_name, context):
            return context

    monkeypatch.setattr(main, "templates", _Templates())
    monkeypatch.setattr(main, "is_rtm_auth_valid", lambda: True)

    context = main.approvals_list(SimpleNamespace(), db_session)
    statuses = [c["decision_status"] for c in context["captures"]]
    assert statuses == ["proposed"]

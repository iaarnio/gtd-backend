import json
from types import SimpleNamespace

from app import models, rtm_commit


def test_project_generates_two_entries_and_na_only_on_first_action():
    entries = rtm_commit._compute_commit_entries(
        {
            "type": "project",
            "project_name": "Resistentin tärkkelyksen lisääminen ruokavalioon",
            "project_shortname": "RTÄRK",
            "next_action": "RTÄRK --- Tutki mistä saa vihreää banaanijauhetta",
        }
    )

    assert len(entries) == 2
    project_smart_add, project_task_name = entries[0]
    action_smart_add, action_task_name = entries[1]

    assert "§§§" in project_task_name
    assert "#na" not in project_smart_add
    assert "#na" in action_smart_add
    assert action_task_name.startswith("RTÄRK ---")


def test_standalone_next_action_does_not_get_na_tag():
    entries = rtm_commit._compute_commit_entries(
        {
            "type": "next_action",
            "next_action": "check zurich event 26.3.",
            "clarified_text": "check zurich event 26.3.",
        }
    )
    assert len(entries) == 1
    assert "#na" not in entries[0][0]


def test_anchor_not_created_when_same_named_task_exists_in_rtm(db_session, monkeypatch):
    capture = models.Capture(raw_text="foo", source="test", decision_status="proposed")
    db_session.add(capture)
    db_session.commit()

    monkeypatch.setattr(
        rtm_commit,
        "_anchor_task_exists_in_rtm",
        lambda auth_token, anchor_name: True,
    )
    monkeypatch.setattr(
        rtm_commit,
        "create_timeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not create timeline")),
    )
    monkeypatch.setattr(
        rtm_commit,
        "add_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not add task")),
    )

    import app.rtm_auth as rtm_auth

    monkeypatch.setattr(
        rtm_auth,
        "get_rtm_auth",
        lambda: SimpleNamespace(auth_token="token"),
    )

    rtm_commit._ensure_anchor_for_pending_approvals(db_session)

    anchor = db_session.query(models.Anchor).one()
    state = json.loads(anchor.external_state)
    assert state["status"] == "already_exists"
    assert state["anchor_name"] == "Tarkista GTD-hyväksynnät"

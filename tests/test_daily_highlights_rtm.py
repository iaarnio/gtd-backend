import pytest

from app import daily_highlights


def test_parse_getlist_response_extracts_lists_taskseries_and_tasks():
    raw_xml = """
    <rsp stat="ok">
      <tasks rev="1">
        <list id="10">
          <taskseries id="20" name="Do thing">
            <task id="30" completed="" />
          </taskseries>
        </list>
      </tasks>
    </rsp>
    """
    parsed = daily_highlights._parse_getlist_response(raw_xml)
    assert len(parsed["lists"]) == 1
    assert parsed["lists"][0]["id"] == "10"
    assert parsed["lists"][0]["taskseries"][0]["id"] == "20"
    assert parsed["lists"][0]["taskseries"][0]["task"][0]["id"] == "30"


def test_task_tag_mutation_raises_on_rtm_fail(monkeypatch):
    monkeypatch.setattr(
        daily_highlights,
        "rtm_call",
        lambda *args, **kwargs: {
            "raw": '<rsp stat="fail"><err code="1" msg="No timeline specified" /></rsp>'
        },
    )
    with pytest.raises(RuntimeError, match="No timeline specified"):
        daily_highlights._rtm_task_tag_mutation(
            "rtm.tasks.addTag",
            {"list_id": "1", "taskseries_id": "2", "task_id": "3", "tags": "highlight-today"},
        )

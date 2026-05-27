from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import ChatMessageRequest
from app.tools.registry import get_tool, invoke_tool, registered_tool_names, registered_tool_specs


def test_registry_exposes_initial_tools():
    names = registered_tool_names()
    assert "pdf_to_erp" in names
    assert "erp_qa" in names
    assert get_tool("pdf_to_erp") is not None
    assert get_tool("erp_qa") is not None


def test_registry_exposes_tool_call_specs():
    specs = {item["name"]: item for item in registered_tool_specs()}

    assert "pdf_to_erp" in specs
    assert "erp_qa" in specs
    assert "parameters" in specs["pdf_to_erp"]
    assert "action" in specs["pdf_to_erp"]["parameters"]["properties"]
    assert "query" in specs["erp_qa"]["parameters"]["properties"]


def test_registry_invokes_erp_qa(monkeypatch):
    def _fake_answer(org_id, message, erp):
        return ("registry erp result", ["search_materials"], {})

    monkeypatch.setattr("app.tools.erp_qa.answer_with_erp_tools", _fake_answer)
    res = invoke_tool(
        "erp_qa",
        ChatMessageRequest(message="查物料 M001", org_id="org-test", user_id="u-test"),
    )

    assert res is not None
    assert res.active_task is not None
    assert res.active_task.type == "erp_qa"
    assert res.messages[0].content == "registry erp result"


def test_registry_unknown_tool_returns_none():
    assert get_tool("missing") is None
    assert invoke_tool("missing", ChatMessageRequest(message="hello", org_id="org-test")) is None

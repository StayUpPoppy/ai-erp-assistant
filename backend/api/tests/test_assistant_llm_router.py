import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant_llm_router import decide_with_llm, probe_llm_router, should_use_plain_chat_fast_path
from app.assistant_session_store import reset_sessions_for_tests
from app.routes import assistant_messages_route, assistant_messages_stream_route, assistant_session_route
from app.schemas import ChatMessageRequest, CreateIngestionRequest
from app.store import create_ingestion, store


def _reset_in_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _build_request(path: str = "/assistant/messages"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-assistant-llm-test"
    return request


def _new_ingestion(file_hash: str):
    return create_ingestion(
        CreateIngestionRequest(
            file_id=f"file-{file_hash}",
            file_hash=file_hash,
            user_id="u-test",
            org_id="org-test",
        )
    )


def test_llm_router_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("ASSISTANT_LLM_ROUTER_ENABLED", raising=False)
    assert decide_with_llm(ChatMessageRequest(message="查物料 M001", org_id="org-test")) is None


def test_llm_router_probe_reports_disabled(monkeypatch):
    monkeypatch.delenv("ASSISTANT_LLM_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_VISION_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    res = probe_llm_router(ChatMessageRequest(message="查物料 M001", org_id="org-test"))

    assert res.enabled is False
    assert res.ok is False
    assert res.attempted is False
    assert res.error is not None


def test_llm_router_probe_calls_model(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: '{"tool_call":{"name":"erp_qa","arguments":{"query":"查物料 M001"}},"reason":"probe"}',
    )

    res = probe_llm_router(ChatMessageRequest(message="查物料 M001", org_id="org-test"))

    assert res.enabled is True
    assert res.api_key_configured is True
    assert res.ok is True
    assert res.attempted is True
    assert res.tool_name == "erp_qa"
    assert res.arguments["query"] == "查物料 M001"


def test_llm_router_parses_erp_query(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: '{"route":"erp_qa","action":null,"message":"查物料 M001","fields":{},"reason":"query"}',
    )
    decision = decide_with_llm(ChatMessageRequest(message="查物料 M001", org_id="org-test"))

    assert decision is not None
    assert decision.route == "erp_qa"
    assert decision.message == "查物料 M001"


def test_llm_router_timeout_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )

    decision = decide_with_llm(ChatMessageRequest(message="查物料 M001", org_id="org-test"))

    assert decision is None


def test_llm_router_parses_tool_call_format(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (
            '{"tool_call":{"name":"erp_qa","arguments":{"query":"查物料 M001"}},'
            '"reason":"query"}'
        ),
    )
    decision = decide_with_llm(ChatMessageRequest(message="帮我看看 M001", org_id="org-test"))

    assert decision is not None
    assert decision.tool_name == "erp_qa"
    assert decision.message == "查物料 M001"
    assert decision.arguments["query"] == "查物料 M001"


def test_llm_router_sends_tool_specs_to_model(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return '{"tool_call":{"name":"assistant","arguments":{}},"reason":"help"}'

    monkeypatch.setattr("app.assistant_llm_router.chat_completion_json", _fake_chat)
    decision = decide_with_llm(ChatMessageRequest(message="你好", org_id="org-test"))

    assert decision is not None
    context = captured["messages"][1]["content"]
    assert "available_tools" in context
    assert "pdf_to_erp" in context
    assert "erp_qa" in context
    assert captured["kwargs"]["max_tokens"] == 512
    assert captured["kwargs"]["timeout_seconds"] == 20.0


def test_plain_chat_fast_path_allows_obvious_chat():
    assert should_use_plain_chat_fast_path(ChatMessageRequest(message="你好，介绍一下你自己", org_id="org-test"))
    assert should_use_plain_chat_fast_path(ChatMessageRequest(message="帮我写一段欢迎语", org_id="org-test"))


def test_plain_chat_fast_path_blocks_business_or_active_task():
    assert not should_use_plain_chat_fast_path(ChatMessageRequest(message="你好，帮我查物料 M001", org_id="org-test"))
    assert not should_use_plain_chat_fast_path(
        ChatMessageRequest(message="你好，看看这个订单进度", org_id="org-test", active_task_id="ing-1")
    )


def test_llm_router_sanitizes_unconfirmed_create_draft(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: '{"route":"pdf_to_erp","action":"create_draft","fields":{},"reason":"unsafe"}',
    )
    decision = decide_with_llm(
        ChatMessageRequest(message="看起来可以了吧", org_id="org-test", active_task_id="ing-1")
    )

    assert decision is not None
    assert decision.route == "pdf_to_erp"
    assert decision.action == "get_status"


def test_assistant_uses_llm_decision_for_erp_query(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: '{"route":"erp_qa","message":"查物料 M001","fields":{},"reason":"query"}',
    )

    def _fake_answer(org_id, message, erp):
        return ("LLM 路由后的 ERP 结果", ["search_materials"], {})

    monkeypatch.setattr("app.tools.erp_qa.answer_with_erp_tools", _fake_answer)
    res = assistant_messages_route(
        ChatMessageRequest(message="帮我看看 M001", org_id="org-test"),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "erp_qa"
    assert res.messages[0].content == "LLM 路由后的 ERP 结果"


def test_assistant_executes_llm_tool_call_for_erp_query(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: '{"tool_call":{"name":"erp_qa","arguments":{"query":"查物料 M001"}},"reason":"query"}',
    )

    def _fake_answer(org_id, message, erp):
        assert message == "查物料 M001"
        return ("tool call ERP result", ["search_materials"], {})

    monkeypatch.setattr("app.tools.erp_qa.answer_with_erp_tools", _fake_answer)
    res = assistant_messages_route(
        ChatMessageRequest(message="帮我看看 M001", org_id="org-test"),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "erp_qa"
    assert res.messages[0].content == "tool call ERP result"


def test_assistant_uses_llm_for_plain_chat(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(AssertionError("router should be skipped")),
    )
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_text",
        lambda _messages, **_kwargs: "你好，我可以正常聊天，也可以在需要时调用 ERP 工具。",
    )

    res = assistant_messages_route(
        ChatMessageRequest(message="你好，介绍一下你能做什么", org_id="org-test"),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "assistant"
    assert res.active_task.status == "DONE"
    assert res.ui is not None
    assert res.ui.type == "assistant_reply"
    assert "正常聊天" in res.messages[0].content

@pytest.mark.anyio
async def test_assistant_streams_llm_plain_chat(monkeypatch):
    reset_sessions_for_tests()
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(AssertionError("router should be skipped")),
    )
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_text_stream",
        lambda _messages, **_kwargs: iter(["hello", " stream"]),
    )

    response = assistant_messages_stream_route(
        ChatMessageRequest(session_id="s-stream", message="hello", org_id="org-test"),
        _build_request("/assistant/messages/stream"),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: session" in body
    assert 'data: {"content": "hello"}' in body
    assert 'data: {"content": " stream"}' in body
    assert "event: final" in body

    session = assistant_session_route("s-stream", _build_request("/assistant/sessions/s-stream"))
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[1].content == "hello stream"


@pytest.mark.anyio
async def test_assistant_stream_router_timeout_returns_final(monkeypatch):
    reset_sessions_for_tests()
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setenv("ASSISTANT_PLAIN_CHAT_FAST_PATH_ENABLED", "false")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )

    def _fake_answer(org_id, message, erp):
        return ("物料查询 fallback 结果", ["search_materials"], {})

    monkeypatch.setattr("app.tools.erp_qa.answer_with_erp_tools", _fake_answer)
    response = assistant_messages_stream_route(
        ChatMessageRequest(session_id="s-timeout", message="查物料 M001", org_id="org-test"),
        _build_request("/assistant/messages/stream"),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: final" in body
    assert "物料查询 fallback 结果" in body


def test_assistant_uses_llm_decision_for_pdf_fields(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    ing = _new_ingestion("hash-assistant-llm-fields")
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setattr(
        "app.assistant_llm_router.chat_completion_json",
        lambda _messages, **_kwargs: (
            '{"route":"pdf_to_erp","action":"submit_missing_fields",'
            '"fields":{"vendor_code":"V001","doc_date":"2026-05-24","currency":"CNY","material_code":"M001","line_qty":"1"},'
            '"reason":"fields"}'
        ),
    )

    res = assistant_messages_route(
        ChatMessageRequest(message="这些字段补一下", org_id="org-test", active_task_id=ing.ingestion_id),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "pdf_to_erp"
    assert res.active_task.status == "WAITING_CONFIRMATION"
    assert res.tool_result is not None
    assert res.tool_result.ingestion is not None
    assert res.tool_result.ingestion.resolved_fields["vendor_code"] == "V001"

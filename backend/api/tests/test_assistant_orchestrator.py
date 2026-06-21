import os
from io import BytesIO
from pathlib import Path
import sys

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant_session_store import reset_sessions_for_tests
from app.routes import assistant_files_route, assistant_messages_route, assistant_session_route
from app.schemas import ChatMessageRequest, CreateIngestionRequest
from app.store import create_ingestion, store


def _reset_in_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()
    reset_sessions_for_tests()


def _build_request(path: str = "/assistant/messages") -> Request:
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
    request.state.request_id = "req-assistant-test"
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


def test_assistant_routes_pdf_task_to_pdf_to_erp():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    ing = _new_ingestion("hash-assistant-pdf-status")

    res = assistant_messages_route(
        ChatMessageRequest(
            message="查进度",
            org_id="org-test",
            user_id="u-test",
            active_task_id=ing.ingestion_id,
        ),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "pdf_to_erp"
    assert res.active_task.ingestion_id == ing.ingestion_id
    assert res.ui is not None
    assert res.ui.type == "processing"


def test_assistant_routes_erp_query(monkeypatch):
    def _fake_answer(org_id, message, erp):
        assert org_id == "org-test"
        assert "M001" in message
        return ("物料 M001：测试物料", ["search_materials"], {})

    monkeypatch.setattr("app.tools.erp_qa.answer_with_erp_tools", _fake_answer)
    res = assistant_messages_route(
        ChatMessageRequest(message="查物料 M001", org_id="org-test", user_id="u-test"),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "erp_qa"
    assert res.messages[0].content == "物料 M001：测试物料"
    assert res.ui is not None
    assert res.ui.type == "erp_query_result"


def test_assistant_plain_message_returns_help():
    _reset_in_memory_store()
    res = assistant_messages_route(
        ChatMessageRequest(message="你好", org_id="org-test", user_id="u-test"),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "assistant"
    assert res.ui is not None
    assert res.ui.type == "assistant_help"


def test_assistant_message_persists_session_history():
    _reset_in_memory_store()
    res = assistant_messages_route(
        ChatMessageRequest(session_id="s-history", message="hello", org_id="org-test", user_id="u-test"),
        _build_request(),
    )

    assert res.session_id == "s-history"
    session = assistant_session_route("s-history", _build_request("/assistant/sessions/s-history"))
    assert session.session_id == "s-history"
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[0].content == "hello"
    assert session.active_task is not None
    assert session.active_task.type == "assistant"


def test_assistant_session_missing_returns_empty_history():
    _reset_in_memory_store()
    session = assistant_session_route("s-new", _build_request("/assistant/sessions/s-new"))

    assert session.session_id == "s-new"
    assert session.messages == []
    assert session.active_task is None


@pytest.mark.anyio
async def test_assistant_files_upload_routes_to_pdf_to_erp(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda _ingestion_id: False)
    monkeypatch.setattr("app.routes.save_binary_file", lambda **_kwargs: "__local__/uploads/org-test/order.pdf")

    upload = UploadFile(
        filename="order.pdf",
        file=BytesIO(b"%PDF-1.4 mock"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    res = await assistant_files_route(
        request=_build_request("/assistant/files"),
        file=upload,
        user_id="u-test",
        org_id="org-test",
        session_id="s-test",
        extraction_profile_id=None,
    )

    assert res.session_id == "s-test"
    assert res.active_task is not None
    assert res.active_task.type == "pdf_to_erp"
    assert res.tool_result is not None
    assert res.tool_result.ingestion is not None
    assert res.tool_result.ingestion.source_file_name == "order.pdf"
    assert res.tool_result.ingestion.source_file_size == len(b"%PDF-1.4 mock")
    assert res.tool_result.ingestion.source_file_content_type == "application/pdf"
    assert res.tool_result.ingestion.source_file_uploaded_at

    session = assistant_session_route("s-test", _build_request("/assistant/sessions/s-test"))
    assert session.active_task is not None
    assert session.active_task.type == "pdf_to_erp"
    assert session.active_task.ingestion_id == res.active_task.ingestion_id
    assert [m.role for m in session.messages] == ["user", "assistant"]

import os
from io import BytesIO
from pathlib import Path
import sys

import pytest
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import chat_files_route, chat_messages_route
from app.schemas import ChatMessageRequest, CreateIngestionRequest, IngestionStatus
from app.store import create_ingestion, store


def _reset_in_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _build_request(path: str = "/chat/messages") -> Request:
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
    request.state.request_id = "req-chat-test"
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


def test_chat_messages_reports_pdf_to_erp_status():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    ing = _new_ingestion("hash-chat-status")

    res = chat_messages_route(
        ChatMessageRequest(
            message="现在进度到哪了",
            org_id="org-test",
            user_id="u-test",
            active_task_id=ing.ingestion_id,
        ),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.type == "pdf_to_erp"
    assert res.active_task.ingestion_id == ing.ingestion_id
    assert res.active_task.status == "PROCESSING"
    assert res.ui is not None
    assert res.ui.type == "processing"


def test_chat_messages_submit_fields_then_create_draft(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    ing = _new_ingestion("hash-chat-draft")

    def _fake_create_draft(doc_type, payload, idempotency_key):
        return ("PO-CHAT-001", "https://mock-erp.local/drafts/PO-CHAT-001")

    monkeypatch.setattr("app.store.erp_client.create_draft", _fake_create_draft)

    resolved = chat_messages_route(
        ChatMessageRequest(
            action="submit_missing_fields",
            message="补充字段",
            org_id="org-test",
            user_id="u-test",
            active_task_id=ing.ingestion_id,
            fields={
                "vendor_code": "V001",
                "doc_date": "2026-05-24",
                "currency": "CNY",
                "material_code": "M001",
                "line_qty": "1",
                "customerName": "北京某公司",
            },
        ),
        _build_request(),
    )
    assert resolved.active_task is not None
    assert resolved.active_task.status == "WAITING_CONFIRMATION"
    assert resolved.ui is not None
    assert resolved.ui.type == "upload_confirm"
    assert resolved.tool_result is not None
    assert resolved.tool_result.ingestion is not None
    assert resolved.tool_result.ingestion.preview_data is not None
    assert resolved.tool_result.ingestion.preview_data.order.customerName == "北京某公司"

    draft = chat_messages_route(
        ChatMessageRequest(
            message="确认上传",
            org_id="org-test",
            user_id="u-test",
            active_task_id=ing.ingestion_id,
        ),
        _build_request(),
    )

    assert draft.active_task is not None
    assert draft.active_task.status == "DONE"
    assert draft.tool_result is not None
    assert draft.tool_result.draft is not None
    assert draft.tool_result.draft.status == IngestionStatus.DRAFT_CREATED
    assert draft.tool_result.draft.draft_no == "PO-CHAT-001"
    assert draft.ui is not None
    assert draft.ui.type == "draft_result"


def test_chat_messages_extracts_missing_fields_from_text():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    ing = _new_ingestion("hash-chat-natural-fields")

    res = chat_messages_route(
        ChatMessageRequest(
            message="供应商编码是V001，单据日期是2026-05-24，币别是CNY，物料编码是M001，数量是1",
            org_id="org-test",
            user_id="u-test",
            active_task_id=ing.ingestion_id,
        ),
        _build_request(),
    )

    assert res.active_task is not None
    assert res.active_task.status == "WAITING_CONFIRMATION"
    assert res.tool_result is not None
    assert res.tool_result.ingestion is not None
    assert res.tool_result.ingestion.resolved_fields["vendor_code"] == "V001"
    assert res.tool_result.ingestion.resolved_fields["material_code"] == "M001"


@pytest.mark.anyio
async def test_chat_files_upload_returns_pdf_to_erp_tool_response(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda _ingestion_id: False)
    monkeypatch.setattr("app.routes.save_binary_file", lambda **_kwargs: "__local__/uploads/org-test/order.pdf")

    upload = UploadFile(
        filename="order.pdf",
        file=BytesIO(b"%PDF-1.4 mock"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    res = await chat_files_route(
        request=_build_request("/chat/files"),
        file=upload,
        user_id="u-test",
        org_id="org-test",
        session_id="s-test",
        extraction_profile_id=None,
    )

    assert res.session_id == "s-test"
    assert res.active_task is not None
    assert res.active_task.type == "pdf_to_erp"
    assert res.active_task.ingestion_id
    assert res.ui is not None
    assert res.ui.type == "processing"
    assert res.tool_result is not None
    assert res.tool_result.ingestion is not None
    assert res.tool_result.ingestion.source_file_name == "order.pdf"

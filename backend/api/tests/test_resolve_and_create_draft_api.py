import os
from pathlib import Path
import sys

from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import confirm_preview_route, create_draft_route, resolve_ingestion_route
from app.schemas import (
    ConfirmPreviewRequest,
    CreateIngestionRequest,
    ErrorCode,
    DocType,
    IngestionStatus,
    OrderPreviewData,
    OrderPreviewDetail,
    OrderPreviewHeader,
    ResolveIngestionRequest,
)
from app.store import create_ingestion, store


def _reset_in_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _new_ingestion_payload(file_hash: str) -> CreateIngestionRequest:
    return CreateIngestionRequest(
        file_id=f"file-{file_hash}",
        file_hash=file_hash,
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )


def _build_request(path: str) -> Request:
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
    request.state.request_id = "req-test"
    return request


def test_create_draft_route_rejects_when_missing_fields():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-draft-missing"))

    try:
        create_draft_route(created.ingestion_id, _build_request("/ingestions/test/create-draft"))
        assert False, "expected HTTPException for missing required fields"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == ErrorCode.MISSING_REQUIRED_FIELDS.value


def test_resolve_then_create_draft_success_and_idempotent(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-draft-ok"))

    calls = {"count": 0}

    def _fake_create_draft(doc_type, payload, idempotency_key):
        calls["count"] += 1
        return ("PO-DRAFT-TEST01", "https://mock-erp.local/drafts/PO-DRAFT-TEST01")

    monkeypatch.setattr("app.store.erp_client.create_draft", _fake_create_draft)
    resolved = resolve_ingestion_route(
        created.ingestion_id,
        ResolveIngestionRequest(
            fields={
                "vendor_code": "V001",
                "doc_date": "2026-04-29",
                "currency": "CNY",
                "material_code": "M001",
                "line_qty": "1",
            }
        ),
        _build_request("/ingestions/test/resolve"),
    )
    assert resolved.status == IngestionStatus.VALIDATED
    assert resolved.missing_fields == []

    first = create_draft_route(created.ingestion_id, _build_request("/ingestions/test/create-draft"))
    second = create_draft_route(created.ingestion_id, _build_request("/ingestions/test/create-draft"))

    assert first.status == IngestionStatus.DRAFT_CREATED
    assert first.draft_no == "PO-DRAFT-TEST01"
    assert second.draft_no == first.draft_no
    assert second.idempotency_key == first.idempotency_key
    assert calls["count"] == 1


def test_confirm_preview_then_create_draft_success(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-preview-ok"))

    calls = {"count": 0}

    def _fake_create_draft(doc_type, payload, idempotency_key):
        calls["count"] += 1
        assert payload.get("customerName") == "北京优向国际能源装备有限公司"
        assert payload.get("datynk_details_json")
        return ("PO-DRAFT-PREVIEW01", "https://mock-erp.local/drafts/PO-DRAFT-PREVIEW01")

    monkeypatch.setattr("app.store.erp_client.create_draft", _fake_create_draft)

    confirmed = confirm_preview_route(
        created.ingestion_id,
        ConfirmPreviewRequest(
            preview_data=OrderPreviewData(
                order=OrderPreviewHeader(
                    org="英科1厂",
                    customerName="北京优向国际能源装备有限公司",
                    customerPoNo="111111",
                    salesUser="顾晓龄",
                    orderDate="2026-05-13",
                    orderStatus="pending",
                    deliveryAddr="望京园402号楼12层1507",
                    rate=1,
                    currency="CNY",
                    deliveryDate="2026-05-13",
                ),
                details=[
                    OrderPreviewDetail(
                        materialCode="S01P019430",
                        productName="压缩弹簧",
                        productSpec="左旋7*55*122*8.5",
                        ph="60Si2Mn",
                        qty=1,
                        price=1.7699115044,
                        taxPrice=2,
                        amount=1.7699115044,
                        allAmount=2,
                        tax=13,
                        taxAmount=0.2300884956,
                        gift=False,
                        remark="",
                    )
                ],
            )
        ),
        _build_request("/ingestions/test/confirm-preview"),
    )
    assert confirmed.status == IngestionStatus.VALIDATED
    assert confirmed.preview_data is not None
    assert confirmed.resolved_fields.get("customerName") == "北京优向国际能源装备有限公司"
    assert confirmed.resolved_fields.get("material_code") == "S01P019430"

    draft = create_draft_route(created.ingestion_id, _build_request("/ingestions/test/create-draft"))
    assert draft.draft_no == "PO-DRAFT-PREVIEW01"
    assert calls["count"] == 1


def test_datynk_sale_order_create_draft_uses_po_even_if_existing_hint_is_inv(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-datynk-existing-inv"))
    created.doc_type_hint = DocType.INV
    created.missing_fields = []
    created.resolved_fields = {
        "org": "英科1厂",
        "customerName": "Test Customer",
        "doc_date": "2026-05-13",
        "currency": "CNY",
        "delivery_date": "2026-05-13",
        "material_code": "S01P019430",
        "line_qty": "1",
        "datynk_details_json": "[]",
    }
    store.ingestions[created.ingestion_id] = created

    def _fake_create_draft(doc_type, payload, idempotency_key):
        assert doc_type == "PO"
        assert idempotency_key.endswith(":PO")
        return ("SO-DRAFT-REAL01", "")

    monkeypatch.setattr("app.store.erp_client.create_draft", _fake_create_draft)

    draft = create_draft_route(created.ingestion_id, _build_request("/ingestions/test/create-draft"))

    assert draft.draft_no == "SO-DRAFT-REAL01"

import os
from pathlib import Path
import sys

from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import CreateIngestionRequest, ErrorCode, IngestionStatus, OrderPreviewData, OrderPreviewDetail, OrderPreviewHeader
from app.routes import process_ingestion_route
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


def _build_request() -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/internal/ingestions/test/process",
        "raw_path": b"/internal/ingestions/test/process",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-test"
    return request


def test_process_ingestion_route_advances_status_in_memory():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-process-success"))

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert result.error_code is None
    assert len(result.audit_events) >= 5


def test_process_ingestion_parses_text_object_when_bytes_available(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-parse",
        file_hash="hash-parse-text-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/abc12345-draft-po_note.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)

    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: b"Purchase Order V001\nDate 2026-05-06\nCurrency CNY\nMaterial M001\nQty 10\n",
    )

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert (result.parsed_char_count or 0) > 0
    assert result.extract_preview
    assert result.resolved_fields.get("doc_date") == "2026-05-06"
    assert result.resolved_fields.get("currency") == "CNY"
    assert result.resolved_fields.get("vendor_code") == "V001"
    assert result.resolved_fields.get("material_code") == "M001"
    assert result.resolved_fields.get("line_qty") == "10"
    assert result.missing_fields
    assert len(result.vendor_candidates) >= 1
    assert len(result.material_candidates) >= 1
    assert result.preview_data is not None
    assert result.preview_data.order.org == "org-test"
    assert len(result.preview_data.details) >= 1


def test_process_ingestion_rebuilds_stale_preview_data(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-stale-preview",
        file_hash="hash-stale-preview-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/abc12345-draft-po_note.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    created.preview_data = OrderPreviewData(order=OrderPreviewHeader(), details=[OrderPreviewDetail()])

    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: b"Purchase Order V001\nDate 2026-05-06\nCurrency CNY\nMaterial M001\nQty 10\n",
    )

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.preview_data is not None
    assert result.preview_data.order.orderDate == "2026-05-06"
    assert result.preview_data.details[0].materialCode == "M001"
    assert result.preview_data.details[0].qty == 10


def test_process_ingestion_gr_auto_validated_when_text_complete(monkeypatch):
    """文件名含 _gr 提示 GR，正文凑齐必填时可自动 VALIDATED。"""
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-gr",
        file_hash="hash-gr-auto-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/abc12345-draft-gr_demo.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    body = (
        b"Goods receipt\n"
        b"V002\n"
        b"Date 2026-05-10\n"
        b"Currency CNY\n"
        b"PO-88001\n"
        b"Material M050\n"
        b"qty received: 20\n"
    )
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _k: body)
    result = process_ingestion_route(created.ingestion_id, _build_request())
    assert result.doc_type_hint and result.doc_type_hint.value == "GR"
    assert result.status == IngestionStatus.VALIDATED
    assert result.missing_fields == []
    assert result.resolved_fields.get("vendor_code") == "V002"
    assert result.resolved_fields.get("po_no")
    assert result.resolved_fields.get("material_code") == "M050"
    assert result.resolved_fields.get("qty_received") == "20"


def test_process_ingestion_inv_auto_validated_when_text_complete(monkeypatch):
    """文件名含 invoice 提示 INV，正文凑齐必填时可自动 VALIDATED。"""
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-inv",
        file_hash="hash-inv-auto-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/xyz99887-draft-invoice_demo.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    body = (
        b"Tax Invoice\n"
        b"V005\n"
        b"Date 2026-05-12\n"
        b"USD\n"
        b"Invoice No INV-ZZ99\n"
        b"Invoice Date 2026-05-12\n"
    )
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _k: body)
    result = process_ingestion_route(created.ingestion_id, _build_request())
    assert result.doc_type_hint and result.doc_type_hint.value == "INV"
    assert result.status == IngestionStatus.VALIDATED
    assert result.missing_fields == []
    assert result.resolved_fields.get("vendor_code") == "V005"
    assert result.resolved_fields.get("invoice_no") == "INV-ZZ99"
    assert result.resolved_fields.get("invoice_date") == "2026-05-12"


def test_datynk_sale_order_mode_rejects_invoice_named_upload(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-datynk-invoice-name",
        file_hash="hash-datynk-invoice-name-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/customer-invoice-upload.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: b"Tax Invoice\nCustomer ABC\nDate 2026-05-12\nCurrency CNY\nMaterial M001\nQty 2\n",
    )

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status == IngestionStatus.FAILED
    assert result.error_code == ErrorCode.UNSUPPORTED_DOCUMENT.value
    assert result.preview_data is None
    assert any("forced=datynk_sale_order" in ev.message for ev in result.audit_events)


def test_datynk_sale_order_mode_accepts_purchase_order_with_invoice_terms(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-datynk-pogs",
        file_hash="hash-datynk-pogs-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/pogs-POGSVC2600205.txt",
        source_file_name="POGSVC2600205.pdf",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: (
            b"Purchase Order\n"
            b"Order No.: POGSVC2600205\n"
            b"Buyer: Global-set Valve Components Jiangsu Co., LTD\n"
            b"Supplier: Zhejiang Yingke\n"
            b"Vendor Code: 010054\n"
            b"Date 2026-03-06\n"
            b"Currency CNY\n"
            b"Material Code: SOGEYC2600\n"
            b"Qty: 5000\n"
            b"Delivery Date: 2026/3/27\n"
            b"Unit Price: 4.9\n"
            b"PO Number must be stated on packing list and invoice.\n"
            b"Domestic supplier pls provide 13% VAT invoice.\n"
        ),
    )

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status != IngestionStatus.FAILED
    assert result.error_code != ErrorCode.UNSUPPORTED_DOCUMENT.value
    assert result.doc_type_hint and result.doc_type_hint.value == "PO"
    assert result.preview_data is not None
    assert result.resolved_fields.get("customerName") == "Global-set Valve Components Jiangsu Co., LTD"
    assert result.resolved_fields.get("material_code") == "SOGEYC2600"


def test_datynk_sale_order_mode_sends_incomplete_purchase_order_to_user_input(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-datynk-pogs-partial",
        file_hash="hash-datynk-pogs-partial-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/pogs-partial-POGSVC2600205.txt",
        source_file_name="POGSVC2600205.pdf",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: (
            b"Purchase Order\n"
            b"Order No.: POGSVC2600205\n"
            b"Buyer: Global-set Valve Components Jiangsu Co., LTD\n"
            b"Supplier: Zhejiang Yingke\n"
            b"Vendor Code: 010054\n"
            b"Date 2026-03-06\n"
            b"Currency CNY\n"
            b"PO Number must be stated on packing list and invoice.\n"
            b"Domestic supplier pls provide 13% VAT invoice.\n"
        ),
    )

    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert result.error_code is None
    assert result.error_details == {}
    assert result.doc_type_hint and result.doc_type_hint.value == "PO"
    assert "material_code" in result.missing_fields
    assert "line_qty" in result.missing_fields


def test_process_ingestion_po_incomplete_stays_need_user_input(monkeypatch):
    """正文未抽全 PO 行字段时，不自动 VALIDATED。"""
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    payload = CreateIngestionRequest(
        file_id="file-parse-partial",
        file_hash="hash-parse-partial-1",
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="uploads/org/2026-05-01/abc12345-draft-po_partial.txt",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )
    created = create_ingestion(payload)
    monkeypatch.setattr(
        "app.workflow.get_object_bytes",
        lambda _k: b"Purchase Order V001\nDate 2026-05-06\nCurrency CNY\n",
    )
    result = process_ingestion_route(created.ingestion_id, _build_request())
    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert result.missing_fields


def test_process_ingestion_route_returns_404_when_missing():
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    try:
        process_ingestion_route("not-found", _build_request())
        assert False, "expected HTTPException for missing ingestion"
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == ErrorCode.INGESTION_NOT_FOUND.value


def test_process_ingestion_route_exposes_workflow_error_code(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    created = create_ingestion(_new_ingestion_payload("hash-process-failed"))

    def _fake_workflow_failure(ingestion, erp, append_event):
        append_event(ingestion, IngestionStatus.FAILED, "forced workflow failure for testing")
        ingestion.error_code = ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value
        return ingestion

    monkeypatch.setattr("app.store.run_ingestion_processing_workflow", _fake_workflow_failure)
    result = process_ingestion_route(created.ingestion_id, _build_request())

    assert result.status == IngestionStatus.FAILED
    assert result.error_code == ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value

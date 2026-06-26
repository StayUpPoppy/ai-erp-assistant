from datetime import datetime
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import MockErpClient
from app.pdf_pipeline import DocumentParseResult
from app.schemas import (
    AuditEvent,
    DocType,
    ErrorCode,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    OrderPreviewDetail,
    OrderPreviewHeader,
)
from app.workflow import NodeExecutionError, run_ingestion_processing_workflow
from app.qwen_vision_extract import QwenVisionApplyResult


def _append_event(ingestion: IngestionResponse, status: IngestionStatus, message: str) -> None:
    ingestion.status = status
    ingestion.audit_events.append(
        AuditEvent(
            at=datetime.utcnow().isoformat() + "Z",
            status=status,
            message=message,
        )
    )


def _new_ingestion() -> IngestionResponse:
    return IngestionResponse(
        ingestion_id="ing-test",
        file_id="file-test",
        file_hash="hash-test",
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
        status=IngestionStatus.UPLOADED,
    )


def test_workflow_timeout_maps_to_node_specific_error(monkeypatch):
    def _raise_timeout(_state):
        raise NodeExecutionError(
            node_name="map",
            reason="retry timeout exceeded elapsed_ms=300 max_elapsed_ms=200",
            failure_type="timeout",
        )

    monkeypatch.setattr("app.workflow._run_linearly", _raise_timeout)
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)

    ingestion = run_ingestion_processing_workflow(
        ingestion=_new_ingestion(),
        erp=MockErpClient(),
        append_event=_append_event,
    )
    assert ingestion.status == IngestionStatus.FAILED
    assert ingestion.error_code == ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value


def test_workflow_retry_exhausted_maps_to_node_specific_error(monkeypatch):
    def _raise_exhausted(_state):
        raise NodeExecutionError(
            node_name="extract",
            reason="retry exhausted attempts=2 max_retries=1",
            failure_type="retry_exhausted",
        )

    monkeypatch.setattr("app.workflow._run_linearly", _raise_exhausted)
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)

    ingestion = run_ingestion_processing_workflow(
        ingestion=_new_ingestion(),
        erp=MockErpClient(),
        append_event=_append_event,
    )
    assert ingestion.status == IngestionStatus.FAILED
    assert ingestion.error_code == ErrorCode.WORKFLOW_EXTRACT_RETRY_EXHAUSTED.value


def test_workflow_unknown_node_falls_back_to_generic_error(monkeypatch):
    def _raise_unknown(_state):
        raise NodeExecutionError(
            node_name="custom_node",
            reason="custom failed",
            failure_type="retry_exhausted",
        )

    monkeypatch.setattr("app.workflow._run_linearly", _raise_unknown)
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)

    ingestion = run_ingestion_processing_workflow(
        ingestion=_new_ingestion(),
        erp=MockErpClient(),
        append_event=_append_event,
    )
    assert ingestion.status == IngestionStatus.FAILED
    assert ingestion.error_code == ErrorCode.WORKFLOW_RETRY_EXHAUSTED.value


def test_workflow_unsupported_document_maps_to_error(monkeypatch):
    def _raise_unsupported(_state):
        raise NodeExecutionError(
            node_name="extract",
            reason="unsupported_document insufficient_purchase_order_evidence",
            failure_type="node",
        )

    monkeypatch.setattr("app.workflow._run_linearly", _raise_unsupported)
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)

    ingestion = run_ingestion_processing_workflow(
        ingestion=_new_ingestion(),
        erp=MockErpClient(),
        append_event=_append_event,
    )
    assert ingestion.status == IngestionStatus.FAILED
    assert ingestion.error_code == ErrorCode.UNSUPPORTED_DOCUMENT.value


def test_purchase_order_evidence_rejects_unrelated_pdf(monkeypatch):
    from app.workflow import _purchase_order_evidence

    monkeypatch.setenv("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE", "true")
    ingestion = _new_ingestion()
    ingestion.source_file_name = "resume.pdf"
    ok, reason = _purchase_order_evidence(
        ingestion,
        "Curriculum vitae. Education, work experience, project summary, skills, contact information.",
    )
    assert not ok
    assert reason == "obvious_non_order_document"


def test_purchase_order_evidence_continues_for_uncertain_text(monkeypatch):
    from app.workflow import _purchase_order_evidence

    monkeypatch.setenv("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE", "true")
    ingestion = _new_ingestion()
    ingestion.source_file_name = "scanned-order.pdf"
    ok, reason = _purchase_order_evidence(
        ingestion,
        "OCR text is partial and noisy. Some table rows may be unreadable.",
    )
    assert ok
    assert reason.startswith("insufficient_purchase_order_evidence_continue")


def test_validate_order_preview_rejects_empty_preview(monkeypatch):
    from app.workflow import _validate_order_preview

    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    preview = OrderPreviewData(order=OrderPreviewHeader(), details=[OrderPreviewDetail()])

    ok, reason, metrics = _validate_order_preview(preview)

    assert not ok
    assert reason.startswith("invalid_order_preview")
    assert metrics["valid_detail_rows"] == 0


def test_validate_order_preview_accepts_realistic_preview(monkeypatch):
    from app.workflow import _validate_order_preview

    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName="Yingke", customerPoNo="PO-001"),
        details=[OrderPreviewDetail(productName="Spring", productSpec="D10", qty=12)],
    )

    ok, reason, metrics = _validate_order_preview(preview)

    assert ok
    assert reason == "valid_header_and_detail_row"
    assert metrics["valid_detail_rows"] == 1


def test_node_build_preview_rejects_missing_preview_when_validation_enabled(monkeypatch):
    from app.workflow import WorkflowState, _node_build_preview

    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setattr("app.workflow.build_order_preview_data", lambda _ingestion: None)
    ing = _new_ingestion()
    state: WorkflowState = {
        "ingestion": ing,
        "erp": MockErpClient(),
        "append_event": _append_event,
        "mapping_metrics": {},
        "document_text": "",
    }

    with pytest.raises(NodeExecutionError) as exc_info:
        _node_build_preview(state)

    assert exc_info.value.reason == "unsupported_document missing_order_preview"
    assert ing.error_details["category"] == "unsupported_document"
    assert ing.error_details["reason"] == "missing_order_preview"


def test_node_map_continues_when_erp_search_raises():
    """主数据映射阶段：单个 ERP 查询失败时降级为空列表，避免简历等非单据 PDF 因上游 5xx 整单失败。"""
    from app.erp_client import ErpClientError, MockErpClient
    from app.workflow import WorkflowState, _node_map

    class Flaky(MockErpClient):
        def search_vendors(self, org_id: str, keyword: str):
            raise ErpClientError("ERP_UPSTREAM_ERROR", "upstream", 503, {})

    ing = _new_ingestion()
    ing.status = IngestionStatus.EXTRACTED

    def append(ingestion: IngestionResponse, status: IngestionStatus, message: str) -> None:
        ingestion.status = status
        ingestion.audit_events.append(
            AuditEvent(at=datetime.utcnow().isoformat() + "Z", status=status, message=message),
        )

    state: WorkflowState = {
        "ingestion": ing,
        "erp": Flaky(),
        "append_event": append,
        "mapping_metrics": {},
        "document_text": "some vendor text 华为",
    }
    _node_map(state)
    assert ing.vendor_candidates == []
    assert len(ing.material_candidates) >= 1


def test_node_map_runs_erp_searches_concurrently():
    from app.erp_client import MockErpClient
    from app.workflow import WorkflowState, _node_map

    class Slow(MockErpClient):
        def _wait(self, value):
            time.sleep(0.2)
            return [value]

        def search_vendors(self, org_id: str, keyword: str):
            return self._wait({"vendor_code": "V001"})

        def search_materials(self, org_id: str, keyword: str):
            return self._wait({"material_code": "M001"})

        def search_warehouses(self, org_id: str, keyword: str):
            return self._wait({"warehouse_code": "WH01"})

        def search_tax_codes(self, org_id: str, keyword: str):
            return self._wait({"tax_code": "T13"})

    ing = _new_ingestion()
    ing.status = IngestionStatus.EXTRACTED

    def append(ingestion: IngestionResponse, status: IngestionStatus, message: str) -> None:
        ingestion.status = status
        ingestion.audit_events.append(
            AuditEvent(at=datetime.utcnow().isoformat() + "Z", status=status, message=message),
        )

    state: WorkflowState = {
        "ingestion": ing,
        "erp": Slow(),
        "append_event": append,
        "mapping_metrics": {},
        "document_text": "some vendor text",
    }

    started = time.perf_counter()
    _node_map(state)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.55
    assert ing.vendor_candidates == [{"vendor_code": "V001"}]
    assert ing.material_candidates == [{"material_code": "M001"}]
    assert ing.warehouse_candidates == [{"warehouse_code": "WH01"}]
    assert ing.tax_code_candidates == [{"tax_code": "T13"}]


def _parsed(text: str, fmt: str = "pdf_hybrid_pymupdf_rapidocr_250dpi") -> DocumentParseResult:
    return DocumentParseResult(text=text, format_label=fmt, route="hybrid", quality_score=0.92)


def test_workflow_uses_final_pdf_parse_without_second_ocr(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    final_text = (
        "Global-set Valve Components Jiangsu Co., LTD\n"
        "Order No. :POGSVC2600205\n"
        "Vendor Code: 010054\n"
        "Issue Date: 2026/3/6\n"
        "Currency CNY\n"
        "Item | Part No | Code | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | SOGEYC2600 | 020800003 | 13.5x27.3 X-750 | 5000 | 4.9 | 24500 | 2026/3/27\n"
        "2 | SOGEYC2601 | 020800004 | 14.5x28.3 X-750 | 2000 | 5.1 | 10200 | 2026/3/27\n"
    )
    parse_calls = {"count": 0}

    def parse_once(_raw, _name=""):
        parse_calls["count"] += 1
        return _parsed(final_text)

    monkeypatch.setattr("app.workflow.extract_document_from_bytes", parse_once)
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/POGSVC2600205.pdf"
    ing.source_file_name = "POGSVC2600205.pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert parse_calls["count"] == 1
    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert result.parse_format_label == "pdf_hybrid_pymupdf_rapidocr_250dpi"
    assert result.preview_data is not None
    assert result.preview_data.order.customerPoNo == "POGSVC2600205"
    assert result.preview_data.details[0].customerMaterialNo == "020800003"
    assert result.preview_data.details[0].materialCode == ""
    assert "material_code" in result.missing_fields
    assert any("客户物料对应表" in issue.message for issue in result.issues)
    assert not any("forced_ocr_retry" in event.message for event in result.audit_events)


def test_workflow_calls_llm_extractor_once(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    text = (
        "Purchase Order\nBuyer: Acme\nOrder No.: PO-ONE\nIssue Date: 2026-03-06\nCurrency CNY\n"
        "Item | Material | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | M001 | 2 | 10 | 20 | 2026-03-27\n"
    )
    monkeypatch.setattr("app.workflow.extract_document_from_bytes", lambda *_args: _parsed(text))
    calls = {"count": 0}

    def fake_llm(_ingestion, _text):
        calls["count"] += 1
        return False

    monkeypatch.setattr("app.workflow.try_apply_llm_preview", fake_llm)
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/PO-ONE.pdf"
    ing.source_file_name = "PO-ONE.pdf"

    run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert calls["count"] == 1


def test_workflow_qwen_vision_success_skips_local_parse_and_text_llm(monkeypatch):
    from app.order_preview import apply_preview_to_ingestion, preview_to_resolved_fields

    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_FORCE_ALL", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF-1.7 fake")
    monkeypatch.setattr(
        "app.workflow.extract_document_from_bytes",
        lambda *_args: (_ for _ in ()).throw(AssertionError("local parse should be skipped")),
    )
    monkeypatch.setattr(
        "app.workflow.try_apply_llm_preview",
        lambda *_args: (_ for _ in ()).throw(AssertionError("text LLM should be skipped")),
    )

    def fake_qwen(ingestion, _raw, _name, _content_type):
        preview = OrderPreviewData(
            order=OrderPreviewHeader(
                org=ingestion.org_id,
                customerName="Acme",
                customerPoNo="PO-QWEN",
                orderDate="2026-06-26",
                currency="CNY",
                deliveryDate="2026-07-01",
            ),
            details=[OrderPreviewDetail(materialCode="CUST-001", productName="Spring", qty=2)],
        )
        ingestion.doc_type_hint = DocType.PO
        apply_preview_to_ingestion(ingestion, preview)
        ingestion.resolved_fields.update({k: v for k, v in preview_to_resolved_fields(preview).items() if str(v).strip()})
        ingestion.model_version = "qwen3.7-plus"
        ingestion.prompt_version = "qwen-vision-order-preview-v1"
        return QwenVisionApplyResult(
            attempted=True,
            applied=True,
            pages=1,
            images=1,
            elapsed_ms=12,
            summary_text="Purchase Order\nOrder No.: PO-QWEN\n1 | CUST-001 | Spring | 2",
        )

    monkeypatch.setattr("app.workflow.try_apply_qwen_vision_preview", fake_qwen)
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/PO-QWEN.pdf"
    ing.source_file_name = "PO-QWEN.pdf"
    ing.source_file_content_type = "application/pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert result.parse_format_label == "qwen_vision"
    assert result.model_version == "qwen3.7-plus"
    assert result.prompt_version == "qwen-vision-order-preview-v1"
    assert result.preview_data is not None
    assert result.preview_data.order.customerPoNo == "PO-QWEN"
    assert any("qwen vision structured fields extracted" in event.message for event in result.audit_events)


def test_workflow_qwen_vision_failure_falls_back_to_local_parse(monkeypatch):
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_FORCE_ALL", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setenv("QWEN_VISION_FALLBACK_TO_LOCAL", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF-1.7 fake")
    monkeypatch.setattr(
        "app.workflow.try_apply_qwen_vision_preview",
        lambda *_args: QwenVisionApplyResult(attempted=True, applied=False, reason="bad_json"),
    )
    text = (
        "Purchase Order\nBuyer: Acme\nOrder No.: PO-FALLBACK\nIssue Date: 2026-03-06\nCurrency CNY\n"
        "Item | Material | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | M001 | 2 | 10 | 20 | 2026-03-27\n"
    )
    parse_calls = {"count": 0}

    def local_parse(*_args):
        parse_calls["count"] += 1
        return _parsed(text)

    monkeypatch.setattr("app.workflow.extract_document_from_bytes", local_parse)
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/PO-FALLBACK.pdf"
    ing.source_file_name = "PO-FALLBACK.pdf"
    ing.source_file_content_type = "application/pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert parse_calls["count"] == 1
    assert result.parse_format_label == "pdf_hybrid_pymupdf_rapidocr_250dpi"
    assert result.preview_data is not None
    assert result.preview_data.order.customerPoNo == "PO-FALLBACK"
    assert any("Qwen视觉抽取失败" in issue.message for issue in result.issues)


def test_workflow_incomplete_final_parse_requests_user_input_without_retry(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    monkeypatch.setattr(
        "app.workflow.extract_document_from_bytes",
        lambda *_args: _parsed("Purchase Order\nOrder No.: POGSVCEMPTY\n", "pdf_rapidocr_250dpi"),
    )
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/POGSVCEMPTY.pdf"
    ing.source_file_name = "POGSVCEMPTY.pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert result.status == IngestionStatus.NEED_USER_INPUT
    assert result.preview_data is not None
    assert result.preview_data.order.customerPoNo == "POGSVCEMPTY"
    assert "material_code" in result.missing_fields
    assert "line_qty" in result.missing_fields
    assert result.parse_format_label == "pdf_rapidocr_250dpi"
    assert not any("forced_ocr_retry" in event.message for event in result.audit_events)

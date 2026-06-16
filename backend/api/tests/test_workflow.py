from datetime import datetime
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import MockErpClient
from app.schemas import (
    AuditEvent,
    ErrorCode,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    OrderPreviewDetail,
    OrderPreviewHeader,
)
from app.workflow import NodeExecutionError, run_ingestion_processing_workflow


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


def test_workflow_forced_ocr_retry_recovers_low_quality_first_pass(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    monkeypatch.setattr(
        "app.workflow.extract_text_from_bytes",
        lambda _raw, _name="": ("Purchase Order\nOrder No.: POGSVC2600205\n", "pdf_text"),
    )
    monkeypatch.setattr(
        "app.workflow.extract_pdf_text_with_forced_chinese_ocr",
        lambda _raw, _name="", max_pages=3: (
            "Global-set Valve Components Jiangsu Co., LTD Address: Yao Lane Paragraph,122 Highway,"
            "Picheng Town Danyang City,Jiangsu Province (212300)\n"
            "Order No. :POGSVC2600205\n"
            "Vendor Code: 010054\n"
            "Issue Date : 6 / 2\n"
            "Fax: 0511-86322635 - 2026/3/6\n"
            "Item | Part No | Drawing No | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
            "1 | SOGEYC2600 | sooson00s | 13.5x27.3 X-750 | 5000 | 4] 2026//27 49 24500\n"
            "2 | SOGEYC2601 | sooson00t | 14.5x28.3 X-750 | 2000 | 5.1 | 10200 | 2026/3/27\n",
            "pdf_text+forced_ocr_pages_2",
        ),
    )
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/POGSVC2600205.pdf"
    ing.source_file_name = "POGSVC2600205.pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert result.status == IngestionStatus.VALIDATED
    assert result.parse_format_label == "pdf_text+forced_ocr_pages_2"
    assert result.preview_data is not None
    assert result.preview_data.order.customerName == "Global-set Valve Components Jiangsu Co., LTD"
    assert result.preview_data.order.customerPoNo == "POGSVC2600205"
    assert result.preview_data.order.orderDate == "2026-03-06"
    assert result.preview_data.details[0].materialCode == "SOGEYC2600"
    assert result.preview_data.details[0].productSpec == "13.5x27.3"
    assert result.preview_data.details[0].ph == "X-750"
    assert result.preview_data.details[0].qty == 5000
    assert result.resolved_fields["material_code"] == "SOGEYC2600"
    assert any("forced_ocr_retry attempted applied=1" in event.message for event in result.audit_events)



def test_workflow_global_set_code_column_maps_to_material_code(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"plain text bytes")
    monkeypatch.setattr(
        "app.workflow.extract_text_from_bytes",
        lambda _raw, _name="": (
            "Purchase Order\n"
            "Global-set Valve Components Jiangsu Co., LTD\n"
            "Order No.: POGSVC2600205\n"
            "Vendor Code: 010054\n"
            "Issue Date: 2026/3/6\n"
            "Currency CNY\n"
            "Item | Part No | Code | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
            "1 | SOGEYC2600 | 020800003 | 13.5x27.3 x-750 | 5000 | 4.9 | 24500 | 2026/3/27\n"
            "2 | SOGSVC2600 | 020800004 | 11.5x23.5 X-750 | 5000 | 3 | 15000 | 2026/3/27\n",
            "plain_text(utf-8)",
        ),
    )
    ing = _new_ingestion()
    ing.source_file_name = "POGSVC2600205.txt"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert result.status == IngestionStatus.VALIDATED
    assert result.preview_data is not None
    assert [detail.materialCode for detail in result.preview_data.details[:2]] == ["020800003", "020800004"]

    assert result.preview_data.details[0].customerMaterialNo == ""
    assert result.preview_data.details[0].productSpec == "13.5x27.3"
    assert result.preview_data.details[0].ph == "X-750"
    assert result.preview_data.details[0].qty == 5000
    assert result.resolved_fields["material_code"] == "020800003"


def test_workflow_chinese_ocr_retry_prefers_real_chinese_party_fields(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    monkeypatch.setattr(
        "app.workflow.extract_text_from_bytes",
        lambda _raw, _name="": (
            "Purchase Order\n"
            "Buyer: Global-set Valve Components Jiangsu Co., LTD\n"
            "Delivery Address: Yao Lane Paragraph,122 Highway,Picheng Town Danyang City,Jiangsu Province (212300)\n"
            "Order No.: POGSVC2600205\n"
            "Vendor Code: 010054\n"
            "Issue Date: 2026/3/6\n"
            "Currency CNY\n"
            "Item | Part No | Drawing No | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
            "1 | SOGEYC2600 | sooson00s | 13.5x27.3 X-750 | 5000 | 4.9 | 24500 | 2026/3/27\n",
            "pdf_text+ocr_first_page",
        ),
    )
    monkeypatch.setattr(
        "app.workflow.extract_pdf_text_with_forced_chinese_ocr",
        lambda _raw, _name="", max_pages=3: (
            "Purchase Order\n"
            "Global-set Valve Components Jiangsu Co., LTD\n"
            "格鲁赛特阀门配件江苏有限公司\n"
            "Delivery Address:\n"
            "Yao Lane Paragraph,122 Highway,Picheng Town Danyang City,Jiangsu Province (212300)\n"
            "江苏省丹阳市埤城镇122省道尧巷段（212300）\n"
            "Order No.: POGSVC2600205\n"
            "Vendor Code: 010054\n"
            "Issue Date: 2026/3/6\n"
            "Currency CNY\n"
            "Item | Part No | Drawing No | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
            "1 | SOGEYC2600 | sooson00s | 13.5x27.3 X-750 | 5000 | 4.9 | 24500 | 2026/3/27\n",
            "pdf_text+ocr_paddle_ch_pages_2",
        ),
    )
    ing = _new_ingestion()
    ing.source_file_object_key = "__local__/uploads/org-test/2099-01-01/POGSVC2600205.pdf"
    ing.source_file_name = "POGSVC2600205.pdf"

    result = run_ingestion_processing_workflow(ingestion=ing, erp=MockErpClient(), append_event=_append_event)

    assert result.status == IngestionStatus.VALIDATED
    assert result.parse_format_label == "pdf_text+ocr_paddle_ch_pages_2"
    assert result.preview_data is not None
    assert result.preview_data.order.customerName == "格鲁赛特阀门配件江苏有限公司"
    assert result.preview_data.order.deliveryAddr == "江苏省丹阳市埤城镇122省道尧巷段（212300）"
    assert result.preview_data.details[0].materialCode == "SOGEYC2600"
    assert result.preview_data.details[0].qty == 5000
    assert any("reason=missing_or_non_chinese_party_fields" in event.message for event in result.audit_events)


def test_workflow_forced_ocr_retry_keeps_first_pass_when_not_better(monkeypatch):
    monkeypatch.setenv("WORKFLOW_VALIDATE_ORDER_PREVIEW", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setattr("app.workflow.StateGraph", None)
    monkeypatch.setattr("app.workflow.END", None)
    monkeypatch.setattr("app.workflow.get_object_bytes", lambda _key: b"%PDF fake")
    monkeypatch.setattr(
        "app.workflow.extract_text_from_bytes",
        lambda _raw, _name="": ("Purchase Order\nOrder No.: POGSVCEMPTY\n", "pdf_text"),
    )
    monkeypatch.setattr(
        "app.workflow.extract_pdf_text_with_forced_chinese_ocr",
        lambda _raw, _name="", max_pages=3: ("Purchase Order\nOrder No.: POGSVCEMPTY\n", "pdf_text+forced_ocr_empty"),
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
    assert result.parse_format_label == "pdf_text"
    assert any("forced_ocr_retry attempted applied=0" in event.message for event in result.audit_events)

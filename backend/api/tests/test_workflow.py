from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import MockErpClient
from app.schemas import AuditEvent, ErrorCode, IngestionResponse, IngestionStatus
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

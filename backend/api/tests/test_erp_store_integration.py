import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import ErpClientError
from app.schemas import CreateIngestionRequest, ErrorCode, IngestionStatus, ResolveIngestionRequest
from app.store import create_draft_for_ingestion, create_ingestion, resolve_ingestion, store


def _reset_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _payload(file_hash: str) -> CreateIngestionRequest:
    return CreateIngestionRequest(
        file_id=f"file-{file_hash}",
        file_hash=file_hash,
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )


def test_resolve_ingestion_maps_erp_validate_error(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_store()
    created = create_ingestion(_payload("hash-erp-validate-fail"))

    def _raise_validate(_doc_type, _payload, required_keys=None):
        raise ErpClientError(code="MASTER_DATA_NOT_FOUND", message="vendor missing", status_code=404)

    monkeypatch.setattr("app.store.erp_client.validate_draft", _raise_validate)
    resolved = resolve_ingestion(created.ingestion_id, ResolveIngestionRequest(fields={"vendor_code": "V-X"}))

    assert resolved is not None
    assert resolved.status == IngestionStatus.FAILED
    assert resolved.error_code == ErrorCode.ERP_MASTER_DATA_NOT_FOUND.value
    assert resolved.error_details.get("category") == "master_data"
    assert resolved.error_details.get("erp_status_code") == 404
    assert resolved.error_details.get("erp_error_code") == "MASTER_DATA_NOT_FOUND"
    assert resolved.error_details.get("upstream_request_id") is None
    assert isinstance(resolved.error_details.get("field_errors"), list)


def test_create_draft_maps_erp_timeout_error(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_store()
    created = create_ingestion(_payload("hash-erp-draft-timeout"))
    # 先把缺失字段补齐，确保能进入 create_draft 调用路径。
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": "CNY"}
    store.ingestions[created.ingestion_id] = created

    def _raise_create(_doc_type, _payload, _idempotency_key):
        raise ErpClientError(code="UPSTREAM_TIMEOUT", message="timeout", status_code=504)

    monkeypatch.setattr("app.store.erp_client.create_draft", _raise_create)
    draft = create_draft_for_ingestion(created.ingestion_id)
    assert draft is None
    updated = store.ingestions[created.ingestion_id]
    assert updated.status == IngestionStatus.FAILED
    assert updated.error_code == ErrorCode.ERP_UPSTREAM_TIMEOUT.value
    assert updated.error_details.get("category") == "timeout"
    assert updated.error_details.get("erp_status_code") == 504
    assert updated.error_details.get("erp_error_code") == "UPSTREAM_TIMEOUT"
    assert "raw" in updated.error_details

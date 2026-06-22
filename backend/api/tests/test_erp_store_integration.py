import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import ErpClientError
from app.schemas import CreateIngestionRequest, ErrorCode, IngestionStatus, ResolveIngestionRequest
from app.storage_client import ObjectStorageUnavailableError, StoredObjectStat
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


def test_create_draft_reads_source_attachment_without_source_ingestion_id(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_ENABLED", "true")
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_MAX_BYTES", str(30 * 1024 * 1024))
    _reset_store()
    created = create_ingestion(_payload("hash-erp-source-file"))
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-06-22", "currency": "CNY"}
    created.source_file_object_key = "uploads/org-test/order.pdf"
    created.source_file_name = "采购订单.pdf"
    created.source_file_content_type = "application/pdf"
    store.ingestions[created.ingestion_id] = created
    raw = b"%PDF-1.7 source-file"
    monkeypatch.setattr(
        "app.store.stat_object",
        lambda *_args, **_kwargs: StoredObjectStat(size=len(raw), content_type="application/pdf"),
    )
    monkeypatch.setattr("app.store.iter_object_bytes", lambda *_args, **_kwargs: iter([raw]))
    captured: dict[str, object] = {}

    def _create(_doc_type, payload, _idempotency_key, source_attachment=None):
        captured["payload"] = payload
        captured["attachment"] = source_attachment
        return "DRAFT-SOURCE", "https://erp.example/draft/DRAFT-SOURCE"

    monkeypatch.setattr("app.store.erp_client.create_draft", _create)
    result = create_draft_for_ingestion(created.ingestion_id)

    assert result is not None
    assert "sourceIngestionId" not in captured["payload"]
    attachment = captured["attachment"]
    assert attachment.file_name == "采购订单.pdf"
    assert attachment.file_type == "application/pdf"
    assert attachment.content == raw


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        (b"%PDF-1.7 test", "application/pdf"),
        (b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\npng", "image/png"),
    ],
)
def test_create_draft_accepts_supported_source_file_signatures(monkeypatch, raw, expected_type):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_ENABLED", "true")
    _reset_store()
    created = create_ingestion(_payload(f"hash-{expected_type}"))
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-06-22", "currency": "CNY"}
    created.source_file_object_key = "uploads/org-test/source"
    created.source_file_name = "source.bin"
    store.ingestions[created.ingestion_id] = created
    monkeypatch.setattr(
        "app.store.stat_object",
        lambda *_args, **_kwargs: StoredObjectStat(size=len(raw), content_type="application/octet-stream"),
    )
    monkeypatch.setattr("app.store.iter_object_bytes", lambda *_args, **_kwargs: iter([raw]))
    captured: dict[str, object] = {}

    def _create(_doc_type, _payload, _idempotency_key, source_attachment=None):
        captured["attachment"] = source_attachment
        return "DRAFT-FILE", ""

    monkeypatch.setattr("app.store.erp_client.create_draft", _create)
    result = create_draft_for_ingestion(created.ingestion_id)

    assert result is not None
    assert captured["attachment"].file_type == expected_type


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing", "ERP_SOURCE_FILE_MISSING"),
        ("storage", "ERP_SOURCE_FILE_STORAGE_UNAVAILABLE"),
        ("empty", "ERP_SOURCE_FILE_EMPTY"),
        ("large", "ERP_SOURCE_FILE_TOO_LARGE"),
        ("unsupported", "ERP_SOURCE_FILE_UNSUPPORTED"),
    ],
)
def test_source_attachment_failure_blocks_erp_create(monkeypatch, case, expected_code):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_ENABLED", "true")
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_MAX_BYTES", "16")
    _reset_store()
    created = create_ingestion(_payload(f"hash-source-{case}"))
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-06-22", "currency": "CNY"}
    if case != "missing":
        created.source_file_object_key = "uploads/org-test/source.bin"
    store.ingestions[created.ingestion_id] = created

    if case == "storage":
        def _raise_storage(*_args, **_kwargs):
            raise ObjectStorageUnavailableError("offline")

        monkeypatch.setattr("app.store.stat_object", _raise_storage)
    elif case == "empty":
        monkeypatch.setattr(
            "app.store.stat_object",
            lambda *_args, **_kwargs: StoredObjectStat(size=0, content_type="application/pdf"),
        )
    elif case == "large":
        monkeypatch.setattr(
            "app.store.stat_object",
            lambda *_args, **_kwargs: StoredObjectStat(size=17, content_type="application/pdf"),
        )
    elif case == "unsupported":
        raw = b"bad"
        monkeypatch.setattr(
            "app.store.stat_object",
            lambda *_args, **_kwargs: StoredObjectStat(size=len(raw), content_type="application/octet-stream"),
        )
        monkeypatch.setattr("app.store.iter_object_bytes", lambda *_args, **_kwargs: iter([raw]))

    calls = {"count": 0}

    def _create(*_args, **_kwargs):
        calls["count"] += 1
        return "SHOULD-NOT-CREATE", ""

    monkeypatch.setattr("app.store.erp_client.create_draft", _create)
    result = create_draft_for_ingestion(created.ingestion_id)

    assert result is None
    assert calls["count"] == 0
    updated = store.ingestions[created.ingestion_id]
    assert updated.status == IngestionStatus.FAILED
    assert updated.error_details["category"] == "source_file"
    assert updated.error_details["erp_error_code"] == expected_code


def test_draft_with_empty_url_is_idempotent(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_ENABLED", "false")
    _reset_store()
    created = create_ingestion(_payload("hash-empty-draft-url"))
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-06-22", "currency": "CNY"}
    store.ingestions[created.ingestion_id] = created
    calls = {"count": 0}

    def _create(_doc_type, _payload, _idempotency_key):
        calls["count"] += 1
        return "DRAFT-EMPTY-URL", ""

    monkeypatch.setattr("app.store.erp_client.create_draft", _create)
    first = create_draft_for_ingestion(created.ingestion_id)
    second = create_draft_for_ingestion(created.ingestion_id)

    assert first is not None and second is not None
    assert first.draft_no == second.draft_no == "DRAFT-EMPTY-URL"
    assert calls["count"] == 1


def test_erp_error_details_redact_base64_content(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setenv("ERP_SOURCE_ATTACHMENT_ENABLED", "false")
    _reset_store()
    created = create_ingestion(_payload("hash-redact-base64"))
    created.missing_fields = []
    created.resolved_fields = {"vendor_code": "V001", "doc_date": "2026-06-22", "currency": "CNY"}
    store.ingestions[created.ingestion_id] = created

    def _raise(_doc_type, _payload, _idempotency_key):
        raise ErpClientError(
            code="ERP_UPSTREAM_ERROR",
            message="rejected",
            status_code=400,
            details={"body": {"files": [{"base64Content": "secret-base64"}]}},
        )

    monkeypatch.setattr("app.store.erp_client.create_draft", _raise)
    result = create_draft_for_ingestion(created.ingestion_id)

    assert result is None
    raw = store.ingestions[created.ingestion_id].error_details["raw"]
    assert raw["body"]["files"][0]["base64Content"] == "[REDACTED]"

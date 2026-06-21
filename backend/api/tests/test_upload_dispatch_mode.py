import os
from pathlib import Path
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import upload
from app.schemas import IngestionStatus, UploadRequest
from app.store import get_ingestion, store


def _reset_in_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _build_request() -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/uploads",
        "raw_path": b"/uploads",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-upload-dispatch"
    return request


def _payload(file_hash: str) -> UploadRequest:
    return UploadRequest(
        file_name="order.pdf",
        file_hash=file_hash,
        user_id="u-test",
        org_id="org-test",
        source_file_object_key="__local__/uploads/org-test/2099-01-01/order.pdf",
    )


def test_upload_route_keeps_async_mode_when_queue_unavailable(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    monkeypatch.delenv("INGESTION_QUEUE_FALLBACK_MODE", raising=False)
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda _ingestion_id: False)
    called = {"count": 0}

    def _fake_process_ingestion(_ingestion_id: str):
        called["count"] += 1
        return None

    monkeypatch.setattr("app.routes.process_ingestion", _fake_process_ingestion)

    resp = upload(_payload("hash-upload-queue-none"), _build_request())
    ingestion = get_ingestion(resp.ingestion_id)

    assert resp.status == IngestionStatus.UPLOADED
    assert ingestion is not None
    assert ingestion.status == IngestionStatus.UPLOADED
    assert called["count"] == 0
    assert ingestion.audit_events[-1].message == "queue unavailable; task remains uploaded until async worker/queue recovers"


def test_upload_route_can_inline_fallback_when_explicitly_enabled(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    monkeypatch.setenv("INGESTION_QUEUE_FALLBACK_MODE", "inline")
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda _ingestion_id: False)
    called = {"count": 0}

    def _fake_process_ingestion(ingestion_id: str):
        called["count"] += 1
        ingestion = get_ingestion(ingestion_id)
        if ingestion is not None:
            ingestion.status = IngestionStatus.NEED_USER_INPUT
        return ingestion

    monkeypatch.setattr("app.routes.process_ingestion", _fake_process_ingestion)

    resp = upload(_payload("hash-upload-queue-inline"), _build_request())
    ingestion = get_ingestion(resp.ingestion_id)

    assert resp.status == IngestionStatus.NEED_USER_INPUT
    assert ingestion is not None
    assert called["count"] == 1
    assert any("falling back to inline processing" in event.message for event in ingestion.audit_events)


def test_upload_route_reuses_completed_ingestion_without_enqueue(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    enqueue_calls: list[str] = []
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda ingestion_id: enqueue_calls.append(ingestion_id) or True)

    first = upload(_payload("hash-upload-reuse-complete"), _build_request())
    ingestion = get_ingestion(first.ingestion_id)
    assert ingestion is not None
    ingestion.status = IngestionStatus.DRAFT_CREATED
    ingestion.draft_no = "DRAFT-1"
    store.ingestions[ingestion.ingestion_id] = ingestion

    second = upload(_payload("hash-upload-reuse-complete"), _build_request())
    reused = get_ingestion(second.ingestion_id)

    assert second.ingestion_id == first.ingestion_id
    assert second.status == IngestionStatus.DRAFT_CREATED
    assert reused is not None
    assert reused.status == IngestionStatus.DRAFT_CREATED
    assert reused.draft_no == "DRAFT-1"
    assert enqueue_calls == [first.ingestion_id]


def test_upload_route_resets_canceled_ingestion_and_enqueues(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    enqueue_calls: list[str] = []
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda ingestion_id: enqueue_calls.append(ingestion_id) or True)

    first = upload(_payload("hash-upload-reuse-canceled"), _build_request())
    ingestion = get_ingestion(first.ingestion_id)
    assert ingestion is not None
    ingestion.status = IngestionStatus.CANCELED
    ingestion.error_code = "INGESTION_CANCELED"
    ingestion.error_details = {"reason": "cleared by user"}
    store.ingestions[ingestion.ingestion_id] = ingestion

    second = upload(_payload("hash-upload-reuse-canceled"), _build_request())
    reset = get_ingestion(second.ingestion_id)

    assert second.ingestion_id == first.ingestion_id
    assert second.status == IngestionStatus.UPLOADED
    assert reset is not None
    assert reset.status == IngestionStatus.UPLOADED
    assert reset.error_code is None
    assert reset.error_details == {}
    assert enqueue_calls == [first.ingestion_id, first.ingestion_id]


@pytest.mark.parametrize(
    "status",
    [IngestionStatus.NEED_USER_INPUT, IngestionStatus.FAILED, IngestionStatus.VALIDATED],
)
def test_upload_route_resets_non_draft_reupload_and_enqueues(monkeypatch, status):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    enqueue_calls: list[str] = []
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda ingestion_id: enqueue_calls.append(ingestion_id) or True)

    first = upload(_payload(f"hash-upload-reprocess-{status.value.lower()}"), _build_request())
    ingestion = get_ingestion(first.ingestion_id)
    assert ingestion is not None
    ingestion.status = status
    ingestion.resolved_fields = {"customerName": "manual fake value"}
    ingestion.preview_data = None
    store.ingestions[ingestion.ingestion_id] = ingestion

    second = upload(_payload(f"hash-upload-reprocess-{status.value.lower()}"), _build_request())
    reset = get_ingestion(second.ingestion_id)

    assert second.ingestion_id == first.ingestion_id
    assert second.status == IngestionStatus.UPLOADED
    assert reset is not None
    assert reset.status == IngestionStatus.UPLOADED
    assert reset.resolved_fields == {}
    assert enqueue_calls == [first.ingestion_id, first.ingestion_id]


def test_upload_route_force_reprocess_resets_and_enqueues(monkeypatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_store()
    enqueued: list[str] = []
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda ingestion_id: enqueued.append(ingestion_id) or True)

    first = upload(_payload("hash-upload-force-reprocess"), _build_request())
    ingestion = get_ingestion(first.ingestion_id)
    assert ingestion is not None
    ingestion.status = IngestionStatus.DRAFT_CREATED
    ingestion.draft_no = "DRAFT-OLD"
    ingestion.resolved_fields = {"line_items_json": "[{}]"}
    store.ingestions[ingestion.ingestion_id] = ingestion

    payload = _payload("hash-upload-force-reprocess")
    payload.force_reprocess = True
    second = upload(payload, _build_request())
    reset = get_ingestion(second.ingestion_id)

    assert second.ingestion_id == first.ingestion_id
    assert second.file_id == first.file_id
    assert second.status == IngestionStatus.UPLOADED
    assert reset is not None
    assert reset.status == IngestionStatus.UPLOADED
    assert reset.draft_no is None
    assert reset.resolved_fields == {}
    assert enqueued[-1] == first.ingestion_id

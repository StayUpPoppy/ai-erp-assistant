from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from app.routes import (
    delete_ingestion_route,
    get_user_source_file_route,
    head_user_source_file_route,
    history_orders_route,
    pending_ingestions_route,
)
from app.schemas import AuditEvent, ErrorCode, IngestionResponse, IngestionStatus
from app.storage_client import ObjectStorageUnavailableError
from app.store import delete_pending_ingestion, list_pending_ingestions_for_user, store


@pytest.fixture(autouse=True)
def clear_memory_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.store.is_database_enabled", lambda: False)
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()
    yield
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _request_for_user(user_id: str) -> SimpleNamespace:
    payload = {"userId": user_id, "realName": f"user-{user_id}", "currentOrgName": "org-test"}
    return SimpleNamespace(
        cookies={"userinfo": quote(json.dumps(payload))},
        headers={},
        state=SimpleNamespace(request_id="test-request"),
    )


def _ingestion(
    ingestion_id: str,
    user_id: str,
    status: IngestionStatus,
    uploaded_at: str | None,
) -> IngestionResponse:
    return IngestionResponse(
        ingestion_id=ingestion_id,
        file_id=f"file-{ingestion_id}",
        file_hash=f"hash-{ingestion_id}",
        user_id=user_id,
        org_id="org-test",
        extract_version="v0",
        model_version="mock",
        prompt_version="prompt",
        status=status,
        source_file_uploaded_at=uploaded_at,
    )


def _source_file_ingestion(ingestion_id: str = "source-31", user_id: str = "31") -> IngestionResponse:
    return _ingestion(ingestion_id, user_id, IngestionStatus.NEED_USER_INPUT, "2026-06-30T08:00:00Z").model_copy(
        update={
            "source_file_object_key": "__local__/uploads/order.pdf",
            "source_file_name": "order.pdf",
            "source_file_size": 16,
            "source_file_content_type": "application/pdf",
        }
    )


def test_pending_ingestions_route_returns_only_current_user_pending_tasks():
    rows = [
        _ingestion("old-31", "31", IngestionStatus.NEED_USER_INPUT, "2026-06-29T08:00:00Z"),
        _ingestion("new-31", "31", IngestionStatus.VALIDATED, "2026-06-30T08:00:00Z"),
        _ingestion("failed-31", "31", IngestionStatus.FAILED, None),
        _ingestion("unsupported-31", "31", IngestionStatus.FAILED, "2026-06-30T12:00:00Z").model_copy(
            update={"error_code": ErrorCode.UNSUPPORTED_DOCUMENT.value}
        ),
        _ingestion("other-user", "58", IngestionStatus.NEED_USER_INPUT, "2026-06-30T09:00:00Z"),
        _ingestion("draft-31", "31", IngestionStatus.DRAFT_CREATED, "2026-06-30T10:00:00Z"),
        _ingestion("canceled-31", "31", IngestionStatus.CANCELED, "2026-06-30T11:00:00Z"),
    ]
    for row in rows:
        store.ingestions[row.ingestion_id] = row

    result = pending_ingestions_route(_request_for_user("31"), limit=20)

    assert [item.ingestion_id for item in result] == ["new-31", "old-31", "failed-31"]
    assert all(item.user_id == "31" for item in result)


def test_pending_ingestions_route_returns_empty_without_cookie():
    store.ingestions["user-31"] = _ingestion("user-31", "31", IngestionStatus.NEED_USER_INPUT, "2026-06-30T08:00:00Z")
    request = SimpleNamespace(cookies={}, state=SimpleNamespace(request_id="test-request"))

    assert pending_ingestions_route(request, limit=20) == []


def test_pending_ingestions_db_path_filters_after_user_query(monkeypatch: pytest.MonkeyPatch):
    queried_user_ids: list[str] = []

    class FakeSession:
        def close(self) -> None:
            pass

    def fake_list_by_user_id(_session: FakeSession, user_id: str) -> list[IngestionResponse]:
        queried_user_ids.append(user_id)
        return [
            _ingestion("db-new", "31", IngestionStatus.UPLOADED, "2026-06-30T09:00:00Z"),
            _ingestion("db-draft", "31", IngestionStatus.DRAFT_CREATED, "2026-06-30T10:00:00Z"),
            _ingestion("db-unsupported", "31", IngestionStatus.FAILED, "2026-06-30T11:00:00Z").model_copy(
                update={"error_code": ErrorCode.UNSUPPORTED_DOCUMENT.value}
            ),
            _ingestion("db-old", "31", IngestionStatus.MAPPED, "2026-06-29T09:00:00Z"),
        ]

    monkeypatch.setattr("app.store.is_database_enabled", lambda: True)
    monkeypatch.setattr("app.store._db_session", lambda: FakeSession())
    monkeypatch.setattr("app.store.ingestion_db.list_by_user_id", fake_list_by_user_id)

    result = list_pending_ingestions_for_user("31")

    assert queried_user_ids == ["31"]
    assert [item.ingestion_id for item in result] == ["db-new", "db-old"]


def test_history_orders_route_filters_sorts_and_paginates_current_user():
    older = _ingestion("history-old", "31", IngestionStatus.DRAFT_CREATED, "2026-06-30T08:00:00Z").model_copy(
        update={
            "draft_no": "DRAFT-OLD",
            "resolved_fields": {"customerName": "旧客户", "customerPoNo": "PO-OLD", "org": "英科一厂"},
            "audit_events": [
                AuditEvent(at="2026-06-30T09:00:00Z", status=IngestionStatus.DRAFT_CREATED, message="done")
            ],
        }
    )
    newer = _ingestion("history-new", "31", IngestionStatus.DRAFT_CREATED, "2026-07-01T08:00:00Z").model_copy(
        update={
            "source_file_name": "new.pdf",
            "draft_no": "DRAFT-NEW",
            "draft_url": "https://erp.example/drafts/new",
            "audit_events": [
                AuditEvent(at="2026-07-01T10:00:00Z", status=IngestionStatus.DRAFT_CREATED, message="done")
            ],
        }
    )
    store.ingestions = {
        older.ingestion_id: older,
        newer.ingestion_id: newer,
        "pending": _ingestion("pending", "31", IngestionStatus.VALIDATED, "2026-07-02T08:00:00Z"),
        "other": _ingestion("other", "58", IngestionStatus.DRAFT_CREATED, "2026-07-03T08:00:00Z").model_copy(
            update={"draft_no": "DRAFT-OTHER"}
        ),
    }

    first = history_orders_route(_request_for_user("31"), offset=0, limit=1)
    second = history_orders_route(_request_for_user("31"), offset=1, limit=1)

    assert [item.ingestion_id for item in first.items] == ["history-new"]
    assert first.items[0].source_file_name == "new.pdf"
    assert first.has_more is True
    assert first.next_offset == 1
    assert [item.ingestion_id for item in second.items] == ["history-old"]
    assert second.items[0].customer_name == "旧客户"
    assert second.has_more is False
    assert second.next_offset is None


def test_history_orders_route_returns_empty_without_cookie():
    request = SimpleNamespace(cookies={}, state=SimpleNamespace(request_id="test-request"))
    result = history_orders_route(request, offset=0, limit=20)
    assert result.items == []


def test_delete_pending_ingestion_route_hard_deletes_record_file_queue_and_session_refs(monkeypatch: pytest.MonkeyPatch):
    ingestion = _source_file_ingestion("delete-me", "31")
    store.ingestions[ingestion.ingestion_id] = ingestion
    store.file_hash_to_ingestion[f"31:{ingestion.file_hash}"] = ingestion.ingestion_id
    deleted_objects: list[str] = []
    cleaned_sessions: list[str] = []
    monkeypatch.setattr("app.store.delete_object", lambda key: deleted_objects.append(str(key)) or True)
    monkeypatch.setattr("app.store.remove_ingestion_job", lambda ingestion_id: 1)
    monkeypatch.setattr("app.routes.remove_ingestion_references", lambda ingestion_id: cleaned_sessions.append(ingestion_id) or 1)

    result = delete_ingestion_route(ingestion.ingestion_id, _request_for_user("31"))

    assert result.deleted is True
    assert result.queue_removed == 1
    assert result.source_file_deleted is True
    assert ingestion.ingestion_id not in store.ingestions
    assert f"31:{ingestion.file_hash}" not in store.file_hash_to_ingestion
    assert deleted_objects == [ingestion.source_file_object_key]
    assert cleaned_sessions == [ingestion.ingestion_id]


def test_delete_pending_ingestion_requires_cookie_and_exact_owner():
    ingestion = _ingestion("delete-owner", "31", IngestionStatus.DRAFT_CREATED, "2026-06-30T08:00:00Z")
    store.ingestions[ingestion.ingestion_id] = ingestion
    no_cookie = SimpleNamespace(cookies={}, state=SimpleNamespace(request_id="test-request"))

    with pytest.raises(HTTPException) as missing:
        delete_ingestion_route(ingestion.ingestion_id, no_cookie)
    with pytest.raises(HTTPException) as other:
        delete_ingestion_route(ingestion.ingestion_id, _request_for_user("58"))

    assert missing.value.status_code == 401
    assert other.value.status_code == 403
    assert ingestion.ingestion_id in store.ingestions


def test_delete_history_ingestion_hard_deletes_local_record_without_calling_erp(monkeypatch: pytest.MonkeyPatch):
    ingestion = _source_file_ingestion("delete-draft", "31").model_copy(
        update={"status": IngestionStatus.DRAFT_CREATED, "draft_no": "DRAFT-1"}
    )
    store.ingestions[ingestion.ingestion_id] = ingestion
    store.file_hash_to_ingestion[f"31:{ingestion.file_hash}"] = ingestion.ingestion_id
    actions: list[object] = []
    cleaned_sessions: list[str] = []
    monkeypatch.setattr("app.store.erp_client", object())
    monkeypatch.setattr("app.store.delete_object", lambda key: actions.append(("file", key)) or True)
    monkeypatch.setattr("app.store.remove_ingestion_job", lambda ingestion_id: actions.append(("queue", ingestion_id)) or 0)
    monkeypatch.setattr("app.routes.remove_ingestion_references", lambda ingestion_id: cleaned_sessions.append(ingestion_id) or 1)

    result = delete_ingestion_route(ingestion.ingestion_id, _request_for_user("31"))

    assert result.deleted is True
    assert result.queue_removed == 0
    assert result.source_file_deleted is True
    assert ingestion.ingestion_id not in store.ingestions
    assert f"31:{ingestion.file_hash}" not in store.file_hash_to_ingestion
    assert actions == [("file", ingestion.source_file_object_key), ("queue", ingestion.ingestion_id)]
    assert cleaned_sessions == [ingestion.ingestion_id]


def test_delete_ingestion_rejects_non_pending_non_history_status(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion("delete-canceled", "31", IngestionStatus.CANCELED, "2026-06-30T08:00:00Z")
    store.ingestions[ingestion.ingestion_id] = ingestion
    monkeypatch.setattr("app.store.delete_object", lambda _key: pytest.fail("file delete must not run"))

    with pytest.raises(HTTPException) as exc:
        delete_ingestion_route(ingestion.ingestion_id, _request_for_user("31"))

    assert exc.value.status_code == 409
    assert exc.value.detail == ErrorCode.INGESTION_DELETE_NOT_ALLOWED.value
    assert ingestion.ingestion_id in store.ingestions


def test_delete_storage_failure_keeps_database_record(monkeypatch: pytest.MonkeyPatch):
    ingestion = _source_file_ingestion("delete-storage-failure", "31").model_copy(
        update={"status": IngestionStatus.DRAFT_CREATED, "draft_no": "DRAFT-STORAGE"}
    )
    store.ingestions[ingestion.ingestion_id] = ingestion
    monkeypatch.setattr(
        "app.store.delete_object",
        lambda _key: (_ for _ in ()).throw(ObjectStorageUnavailableError("storage down")),
    )
    monkeypatch.setattr("app.store.remove_ingestion_job", lambda _ingestion_id: pytest.fail("queue must not be touched"))

    with pytest.raises(HTTPException) as exc:
        delete_ingestion_route(ingestion.ingestion_id, _request_for_user("31"))

    assert exc.value.status_code == 503
    assert ingestion.ingestion_id in store.ingestions


def test_delete_pending_ingestion_database_path_commits_hard_delete(monkeypatch: pytest.MonkeyPatch):
    ingestion = _source_file_ingestion("delete-db", "31").model_copy(
        update={"status": IngestionStatus.DRAFT_CREATED, "draft_no": "DRAFT-DB"}
    )
    actions: list[object] = []

    class FakeSession:
        def commit(self) -> None:
            actions.append("commit")

        def rollback(self) -> None:
            actions.append("rollback")

        def close(self) -> None:
            actions.append("close")

    monkeypatch.setattr("app.store.is_database_enabled", lambda: True)
    monkeypatch.setattr("app.store._db_session", lambda: FakeSession())
    monkeypatch.setattr("app.store.ingestion_db.get_by_id", lambda _session, _id: ingestion)
    monkeypatch.setattr(
        "app.store.ingestion_db.has_other_source_object_reference",
        lambda _session, _object_key, _id: False,
    )
    monkeypatch.setattr(
        "app.store.ingestion_db.delete_by_id",
        lambda _session, ingestion_id: actions.append(("delete", ingestion_id)) or True,
    )
    monkeypatch.setattr("app.store.delete_object", lambda key: actions.append(("file", key)) or True)
    monkeypatch.setattr("app.store.remove_ingestion_job", lambda ingestion_id: actions.append(("queue", ingestion_id)) or 1)

    result = delete_pending_ingestion(ingestion.ingestion_id)

    assert result is not None
    assert result.deleted is True
    assert result.queue_removed == 1
    assert result.source_file_deleted is True
    assert actions == [
        ("file", ingestion.source_file_object_key),
        ("queue", ingestion.ingestion_id),
        ("delete", ingestion.ingestion_id),
        "commit",
        "close",
    ]


def test_delete_history_ingestion_keeps_shared_source_file(monkeypatch: pytest.MonkeyPatch):
    ingestion = _source_file_ingestion("delete-shared", "31").model_copy(
        update={"status": IngestionStatus.DRAFT_CREATED, "draft_no": "DRAFT-SHARED"}
    )
    other = _source_file_ingestion("keep-shared", "31").model_copy(
        update={"source_file_object_key": ingestion.source_file_object_key}
    )
    store.ingestions = {ingestion.ingestion_id: ingestion, other.ingestion_id: other}
    monkeypatch.setattr("app.store.delete_object", lambda _key: pytest.fail("shared file must not be deleted"))
    monkeypatch.setattr("app.store.remove_ingestion_job", lambda _ingestion_id: 0)

    result = delete_pending_ingestion(ingestion.ingestion_id)

    assert result is not None
    assert result.source_file_deleted is False
    assert ingestion.ingestion_id not in store.ingestions
    assert other.ingestion_id in store.ingestions


def test_user_source_file_route_allows_owner(monkeypatch: pytest.MonkeyPatch):
    store.ingestions["source-31"] = _source_file_ingestion()
    monkeypatch.setattr(
        "app.routes.stat_object",
        lambda *_args, **_kwargs: SimpleNamespace(size=16, content_type="application/pdf", etag="etag-1"),
    )
    monkeypatch.setattr("app.routes.iter_object_bytes", lambda *_args, **_kwargs: iter([b"%PDF-1.7 owner"]))

    head = head_user_source_file_route("source-31", _request_for_user("31"))
    get = get_user_source_file_route("source-31", _request_for_user("31"))

    assert head.status_code == 200
    assert head.headers["content-type"] == "application/pdf"
    assert get.status_code == 200
    assert get.headers["accept-ranges"] == "bytes"


def test_user_source_file_route_rejects_other_user(monkeypatch: pytest.MonkeyPatch):
    store.ingestions["source-31"] = _source_file_ingestion()
    monkeypatch.setattr(
        "app.routes.stat_object",
        lambda *_args, **_kwargs: SimpleNamespace(size=16, content_type="application/pdf", etag="etag-1"),
    )

    with pytest.raises(HTTPException) as exc:
        get_user_source_file_route("source-31", _request_for_user("58"))

    assert exc.value.status_code == 403
    assert exc.value.detail == "FORBIDDEN_SOURCE_FILE_OWNER"


def test_user_source_file_route_requires_cookie(monkeypatch: pytest.MonkeyPatch):
    store.ingestions["source-31"] = _source_file_ingestion()
    monkeypatch.setattr(
        "app.routes.stat_object",
        lambda *_args, **_kwargs: SimpleNamespace(size=16, content_type="application/pdf", etag="etag-1"),
    )
    request = SimpleNamespace(cookies={}, headers={}, state=SimpleNamespace(request_id="test-request"))

    with pytest.raises(HTTPException) as exc:
        get_user_source_file_route("source-31", request)

    assert exc.value.status_code == 401
    assert exc.value.detail == "CURRENT_USER_REQUIRED"

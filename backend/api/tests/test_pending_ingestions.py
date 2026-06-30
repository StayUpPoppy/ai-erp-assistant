from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.routes import pending_ingestions_route
from app.schemas import IngestionResponse, IngestionStatus
from app.store import list_pending_ingestions_for_user, store


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


def test_pending_ingestions_route_returns_only_current_user_pending_tasks():
    rows = [
        _ingestion("old-31", "31", IngestionStatus.NEED_USER_INPUT, "2026-06-29T08:00:00Z"),
        _ingestion("new-31", "31", IngestionStatus.VALIDATED, "2026-06-30T08:00:00Z"),
        _ingestion("failed-31", "31", IngestionStatus.FAILED, None),
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
            _ingestion("db-old", "31", IngestionStatus.MAPPED, "2026-06-29T09:00:00Z"),
        ]

    monkeypatch.setattr("app.store.is_database_enabled", lambda: True)
    monkeypatch.setattr("app.store._db_session", lambda: FakeSession())
    monkeypatch.setattr("app.store.ingestion_db.list_by_user_id", fake_list_by_user_id)

    result = list_pending_ingestions_for_user("31")

    assert queried_user_ids == ["31"]
    assert [item.ingestion_id for item in result] == ["db-new", "db-old"]

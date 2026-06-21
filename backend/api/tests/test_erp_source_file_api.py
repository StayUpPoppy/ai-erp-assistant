from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import get_erp_source_file_route, head_erp_source_file_route
from app.ingestion_db import new_row_from_ingestion, row_to_ingestion
from app.schemas import CreateIngestionRequest
from app.storage_client import save_binary_file
from app.store import create_ingestion, store


def _reset_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _request(*, token: str | None = None, byte_range: str | None = None, method: str = "GET") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if byte_range is not None:
        headers.append((b"range", byte_range.encode()))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/integrations/erp/ingestions/test/source-file",
            "raw_path": b"/integrations/erp/ingestions/test/source-file",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = "req-source-file"
    return request


def _create_source_file(monkeypatch, tmp_path, raw: bytes = b"%PDF-1.7 test-pdf"):
    os.environ.pop("DATABASE_URL", None)
    _reset_store()
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_REQUIRED", raising=False)
    monkeypatch.setenv("LOCAL_OBJECT_STORAGE_DIR", str(tmp_path))
    file_hash = "f" * 64
    key = save_binary_file(raw, "采购订单.pdf", file_hash, "org-test", content_type="application/pdf")
    return create_ingestion(
        CreateIngestionRequest(
            file_id="file-source",
            file_hash=file_hash,
            user_id="u-test",
            org_id="org-test",
            source_file_object_key=key,
            source_file_name="采购订单.pdf",
            source_file_size=len(raw),
            source_file_content_type="application/pdf",
            source_file_uploaded_at="2026-06-21T00:00:00+00:00",
        )
    )


@pytest.mark.anyio
async def test_source_file_get_and_head(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)
    monkeypatch.setenv("SOURCE_FILE_API_TOKEN", "source-secret")

    response = get_erp_source_file_route(ingestion.ingestion_id, _request(token="source-secret"))
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert body == b"%PDF-1.7 test-pdf"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == f'"{ingestion.file_hash}"'
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    head = head_erp_source_file_route(ingestion.ingestion_id, _request(token="source-secret", method="HEAD"))
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(body))
    assert head.body == b""


@pytest.mark.anyio
async def test_source_file_supports_single_byte_range(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)
    monkeypatch.setenv("SOURCE_FILE_API_TOKEN", "source-secret")

    response = get_erp_source_file_route(
        ingestion.ingestion_id,
        _request(token="source-secret", byte_range="bytes=0-3"),
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-3/17"
    assert response.headers["content-length"] == "4"
    assert body == b"%PDF"


def test_source_file_rejects_missing_or_wrong_token(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)
    monkeypatch.setenv("SOURCE_FILE_API_TOKEN", "source-secret")

    with pytest.raises(HTTPException) as missing:
        get_erp_source_file_route(ingestion.ingestion_id, _request())
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        get_erp_source_file_route(ingestion.ingestion_id, _request(token="wrong"))
    assert wrong.value.status_code == 401


def test_source_file_rejects_invalid_range(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)
    monkeypatch.setenv("SOURCE_FILE_API_TOKEN", "source-secret")

    with pytest.raises(HTTPException) as invalid:
        get_erp_source_file_route(
            ingestion.ingestion_id,
            _request(token="source-secret", byte_range="bytes=999-1000"),
        )
    assert invalid.value.status_code == 416
    assert invalid.value.headers == {"Content-Range": "bytes */17"}


def test_source_file_endpoint_is_disabled_without_server_token(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)
    monkeypatch.delenv("SOURCE_FILE_API_TOKEN", raising=False)

    with pytest.raises(HTTPException) as disabled:
        get_erp_source_file_route(ingestion.ingestion_id, _request(token="anything"))
    assert disabled.value.status_code == 503


def test_source_file_metadata_round_trips_through_database_context(monkeypatch, tmp_path):
    ingestion = _create_source_file(monkeypatch, tmp_path)

    restored = row_to_ingestion(new_row_from_ingestion(ingestion))

    assert restored.source_file_name == "采购订单.pdf"
    assert restored.source_file_size == len(b"%PDF-1.7 test-pdf")
    assert restored.source_file_content_type == "application/pdf"
    assert restored.source_file_uploaded_at == "2026-06-21T00:00:00+00:00"

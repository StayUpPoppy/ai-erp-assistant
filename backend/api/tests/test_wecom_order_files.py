from __future__ import annotations

import base64
import hashlib
import json
import os
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from app.routes import (
    _assert_wecom_ingest_token_value,
    pending_ingestions_route,
    wecom_order_file_base64_upload,
    wecom_order_file_upload,
)
from app.schemas import IngestionStatus, WecomOrderFileBase64Request
from app.store import store
from app.wecom_order_routes import (
    WecomOrderRoute,
    clear_wecom_order_routes_for_tests,
    upsert_wecom_order_route,
)


def _reset_in_memory_state() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()
    clear_wecom_order_routes_for_tests()


@pytest.fixture(autouse=True)
def in_memory(monkeypatch: pytest.MonkeyPatch):
    os.environ.pop("DATABASE_URL", None)
    _reset_in_memory_state()
    monkeypatch.setattr("app.routes.enqueue_ingestion_job", lambda _ingestion_id: True)
    monkeypatch.setattr("app.routes.save_binary_file", lambda **_kwargs: "__local__/uploads/org-test/order.pdf")
    yield
    _reset_in_memory_state()


def _request(path: str = "/integrations/wecom/order-files") -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = "req-wecom-test"
    return request


def _upload_file(raw: bytes = b"%PDF-1.7 wecom order") -> UploadFile:
    return UploadFile(
        filename="robot-name.pdf",
        file=BytesIO(raw),
        headers=Headers({"content-type": "application/pdf"}),
    )


def _seed_route(
    *,
    group_id: str = "wr-group-1",
    group_name: str = "格鲁赛特阀门配件江苏有限公司-英科1厂",
    customer_name: str = "格鲁赛特阀门配件江苏有限公司",
    factory_name: str = "英科1厂",
    erp_user_id: str = "31",
    org_id: str = "英科1厂",
) -> WecomOrderRoute:
    return upsert_wecom_order_route(
        WecomOrderRoute(
            route_id="route-1",
            wecom_group_id=group_id,
            wecom_group_name=group_name,
            customer_name=customer_name,
            factory_name=factory_name,
            erp_user_id=erp_user_id,
            sales_user_name="张宇涵",
            org_id=org_id,
        )
    )


def _pending_request(user_id: str) -> SimpleNamespace:
    payload = {"userId": user_id, "realName": f"user-{user_id}", "currentOrgName": "英科1厂"}
    return SimpleNamespace(
        cookies={"userinfo": quote(json.dumps(payload))},
        state=SimpleNamespace(request_id="req-pending-test"),
    )


def test_wecom_ingest_auth_requires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WECOM_INGEST_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc:
        _assert_wecom_ingest_token_value("secret")

    assert exc.value.status_code == 503
    assert exc.value.detail == "WECOM_INGEST_DISABLED"


def test_wecom_ingest_auth_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECOM_INGEST_TOKEN", "secret")

    with pytest.raises(HTTPException) as exc:
        _assert_wecom_ingest_token_value("wrong")

    assert exc.value.status_code == 401
    assert exc.value.detail == "INVALID_WECOM_INGEST_TOKEN"
    _assert_wecom_ingest_token_value("secret")


@pytest.mark.anyio
async def test_wecom_multipart_upload_assigns_ingestion_to_mapped_erp_user() -> None:
    _seed_route()
    raw = b"%PDF-1.7 wecom order"
    digest = hashlib.sha256(raw).hexdigest()

    res = await wecom_order_file_upload(
        request=_request(),
        file=_upload_file(raw),
        file_name="PO-20260630.pdf",
        customer_name="格鲁赛特阀门配件江苏有限公司",
        wecom_message_id="msg-1",
        wecom_group_id=None,
        wecom_group_name=None,
        sent_at=None,
        file_hash=digest,
        sender_user_id=None,
        sender_name=None,
        customer_name_hint=None,
        factory_name_hint=None,
        extraction_profile_id=None,
    )

    assert res.ok is True
    assert res.status == IngestionStatus.UPLOADED
    assert res.file_hash == digest
    assert res.user_id == "31"
    assert res.org_id == "英科1厂"
    ingestion = store.ingestions[res.ingestion_id]
    assert ingestion.user_id == "31"
    assert ingestion.org_id == "英科1厂"
    assert ingestion.source_file_name == "PO-20260630.pdf"


@pytest.mark.anyio
async def test_wecom_multipart_hash_mismatch_rejected_without_ingestion() -> None:
    _seed_route()

    with pytest.raises(HTTPException) as exc:
        await wecom_order_file_upload(
            request=_request(),
            file=_upload_file(),
            file_name="PO.pdf",
            customer_name="格鲁赛特阀门配件江苏有限公司",
            wecom_message_id="msg-1",
            wecom_group_id=None,
            wecom_group_name=None,
            sent_at=None,
            file_hash="0" * 64,
            sender_user_id=None,
            sender_name=None,
            customer_name_hint=None,
            factory_name_hint=None,
            extraction_profile_id=None,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "FILE_HASH_MISMATCH"
    assert store.ingestions == {}


@pytest.mark.anyio
async def test_wecom_multipart_too_large_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_route()
    monkeypatch.setattr("app.routes._MAX_UPLOAD_BYTES", 8)

    with pytest.raises(HTTPException) as exc:
        await wecom_order_file_upload(
            request=_request(),
            file=_upload_file(b"%PDF-1.7 too large"),
            file_name="PO.pdf",
            customer_name="格鲁赛特阀门配件江苏有限公司",
            wecom_message_id="msg-1",
            wecom_group_id=None,
            wecom_group_name=None,
            sent_at=None,
            file_hash=None,
            sender_user_id=None,
            sender_name=None,
            customer_name_hint=None,
            factory_name_hint=None,
            extraction_profile_id=None,
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == "FILE_TOO_LARGE"
    assert store.ingestions == {}


@pytest.mark.anyio
async def test_wecom_unmapped_group_returns_409_without_ingestion() -> None:
    with pytest.raises(HTTPException) as exc:
        await wecom_order_file_upload(
            request=_request(),
            file=_upload_file(),
            file_name="PO.pdf",
            customer_name="未知客户",
            wecom_message_id="msg-1",
            wecom_group_id=None,
            wecom_group_name=None,
            sent_at=None,
            customer_name_hint="未知客户",
            factory_name_hint="英科1厂",
            file_hash=None,
            sender_user_id=None,
            sender_name=None,
            extraction_profile_id=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "UNMAPPED_WECOM_CUSTOMER"
    assert exc.value.detail["message"] == "订单接收失败：客户公司未绑定销售员，请维护客户销售员映射后重传。"
    assert store.ingestions == {}


def test_wecom_base64_upload_success_and_pending_visibility() -> None:
    _seed_route()
    raw = b"%PDF-1.7 base64 order"
    digest = hashlib.sha256(raw).hexdigest()
    payload = WecomOrderFileBase64Request(
        fileName="PO-base64.pdf",
        contentType="application/pdf",
        base64Content=base64.b64encode(raw).decode("ascii"),
        customerName="格鲁赛特阀门配件江苏有限公司",
        fileHash=digest,
        wecomMessageId="msg-base64",
    )

    res = wecom_order_file_base64_upload(payload, _request("/integrations/wecom/order-files/base64"))

    assert res.user_id == "31"
    assert store.ingestions[res.ingestion_id].user_id == "31"
    assert [item.ingestion_id for item in pending_ingestions_route(_pending_request("31"), limit=20)] == [res.ingestion_id]
    assert pending_ingestions_route(_pending_request("58"), limit=20) == []


def test_wecom_base64_invalid_content_rejected() -> None:
    _seed_route()
    payload = WecomOrderFileBase64Request(
        fileName="PO-base64.pdf",
        contentType="application/pdf",
        base64Content="not valid ***",
        customerName="格鲁赛特阀门配件江苏有限公司",
        wecomMessageId="msg-base64",
    )

    with pytest.raises(HTTPException) as exc:
        wecom_order_file_base64_upload(payload, _request("/integrations/wecom/order-files/base64"))

    assert exc.value.status_code == 400
    assert exc.value.detail == "INVALID_BASE64_CONTENT"


def test_wecom_base64_hash_mismatch_rejected() -> None:
    _seed_route()
    payload = WecomOrderFileBase64Request(
        fileName="PO-base64.pdf",
        contentType="application/pdf",
        base64Content=base64.b64encode(b"%PDF-1.7 base64 order").decode("ascii"),
        customerName="格鲁赛特阀门配件江苏有限公司",
        fileHash="0" * 64,
        wecomMessageId="msg-base64",
    )

    with pytest.raises(HTTPException) as exc:
        wecom_order_file_base64_upload(payload, _request("/integrations/wecom/order-files/base64"))

    assert exc.value.status_code == 400
    assert exc.value.detail == "FILE_HASH_MISMATCH"

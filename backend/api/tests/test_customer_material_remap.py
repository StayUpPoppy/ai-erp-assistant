from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import remap_customer_materials_route
from app.schemas import (
    CustomerMaterialRemapRequest,
    ErrorCode,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    OrderPreviewDetail,
    OrderPreviewHeader,
    PreviewIssue,
)
from app.store import remap_customer_materials_for_ingestion, store


@pytest.fixture(autouse=True)
def clear_memory_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.store.is_database_enabled", lambda: False)
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()
    yield
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def _request_for_user(user_id: str | None) -> SimpleNamespace:
    cookies = {}
    if user_id:
        payload = {"userId": user_id, "realName": f"user-{user_id}", "currentOrgName": "英科1厂"}
        cookies["userinfo"] = quote(json.dumps(payload))
    return SimpleNamespace(cookies=cookies, state=SimpleNamespace(request_id="remap-test"))


def _preview() -> OrderPreviewData:
    return OrderPreviewData(
        order=OrderPreviewHeader(
            org="英科1厂",
            customerName="测试客户",
            customerPoNo="PO-100",
            salesUser="测试用户",
            orderDate="2026-08-18",
            currency="CNY",
            deliveryDate="2026-08-30",
        ),
        details=[
            OrderPreviewDetail(
                customerMaterialNo="A-100 D",
                materialCode="OLD-1",
                sourceMaterialCode="识别原始编码-1",
                sourceProductSpec="φ7.5×φ1.5×18（8圈）, Inconel 750",
                qty=2,
                price=10,
            ),
            OrderPreviewDetail(
                customerMaterialNo="not-found",
                materialCode="OLD-2",
                sourceMaterialCode="识别原始编码-2",
                productName="旧名称",
                sourceProductSpec="原始未匹配规格",
                qty=3,
            ),
            OrderPreviewDetail(customerMaterialNo="", materialCode="A300", productName="保留名称", qty=4),
        ],
    )


def _ingestion(status: IngestionStatus = IngestionStatus.VALIDATED) -> IngestionResponse:
    return IngestionResponse(
        ingestion_id="remap-1",
        file_id="file-remap-1",
        file_hash="hash-remap-1",
        user_id="31",
        org_id="英科1厂",
        extract_version="v0",
        model_version="mock",
        prompt_version="prompt",
        status=status,
        preview_data=_preview(),
        issues=[
            PreviewIssue(
                path="order.customerName",
                level="warning",
                message="请确认系统识别的客户名称“测试客户”是否与 ERP 客户对应表内的公司名称一致。",
            ),
            PreviewIssue(
                path="details[0].materialCode",
                level="error",
                message="客户物料编码 OLD 未在 ERP 客户物料对应表中找到。",
            ),
        ],
    )


def test_manual_remap_uses_customer_code_only_persists_results_and_reconfirmation(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    store.ingestions[ingestion.ingestion_id] = ingestion
    calls: list[str] = []

    def fake_lookup(customer_name: str):
        calls.append(customer_name)
        return [
            {
                "custMaterialCode": "A100D",
                "materialNumber": "ERP-100",
                "materialName": "映射名称",
                "materialModel": "映射规格",
                "ph": "映射牌号",
            },
            {
                "custMaterialCode": "A300",
                "materialNumber": "ERP-300",
                "materialName": "不应命中",
            },
        ]

    monkeypatch.setattr("app.store.erp_client.get_customer_material_details_by_customer", fake_lookup)

    result = remap_customer_materials_route(
        ingestion.ingestion_id,
        CustomerMaterialRemapRequest(preview_data=_preview()),
        _request_for_user("31"),
    )

    assert calls == ["测试客户"]
    assert (result.matched, result.unmatched, result.skipped) == (1, 1, 1)
    assert result.ingestion.status == IngestionStatus.NEED_USER_INPUT
    assert result.ingestion.error_code == ErrorCode.MISSING_REQUIRED_FIELDS.value
    first, second, third = result.ingestion.preview_data.details
    assert (first.materialCode, first.productName, first.productSpec, first.ph) == (
        "ERP-100",
        "映射名称",
        "映射规格",
        "映射牌号",
    )
    assert first.sourceMaterialCode == "识别原始编码-1"
    assert first.sourceProductSpec == "φ7.5×φ1.5×18（8圈）, Inconel 750"
    assert second.customerMaterialNo == "not-found"
    assert (second.materialCode, second.productName, second.productSpec, second.ph) == ("", "", "", "")
    assert second.sourceMaterialCode == "识别原始编码-2"
    assert second.sourceProductSpec == "原始未匹配规格"
    assert (second.qty, second.price) == (3, None)
    assert (third.materialCode, third.productName, third.qty) == ("A300", "保留名称", 4)
    messages = [issue.message for issue in result.ingestion.issues]
    assert any(
        "请确认系统识别的客户名称“测试客户”是否与 ERP 客户对应表内的公司名称一致。" == message
        for message in messages
    )
    assert not any("客户物料编码 OLD " in message for message in messages)
    assert any("客户物料编码 not-found " in message for message in messages)
    details_payload = json.loads(result.ingestion.resolved_fields["datynk_details_json"])
    assert details_payload[0]["productSpec"] == "映射规格"
    assert details_payload[1]["materialCode"] == ""
    assert result.ingestion.audit_events[-1].status == IngestionStatus.NEED_USER_INPUT
    assert "preview_confirmation_required=1" in result.ingestion.audit_events[-1].message


def test_manual_remap_all_matched_still_requires_confirmation(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    preview = _preview().model_copy(update={"details": [_preview().details[0]]})
    store.ingestions[ingestion.ingestion_id] = ingestion
    monkeypatch.setattr(
        "app.store.erp_client.get_customer_material_details_by_customer",
        lambda _customer: [{"custMaterialCode": "A100D", "materialNumber": "ERP-100"}],
    )

    result = remap_customer_materials_route(
        ingestion.ingestion_id,
        CustomerMaterialRemapRequest(preview_data=preview),
        _request_for_user("31"),
    )

    assert result.ingestion.status == IngestionStatus.NEED_USER_INPUT
    assert result.ingestion.error_code is None
    assert result.ingestion.missing_fields == []


def test_manual_remap_requires_cookie_exact_owner_and_pending_status(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    store.ingestions[ingestion.ingestion_id] = ingestion
    monkeypatch.setattr(
        "app.store.erp_client.get_customer_material_details_by_customer",
        lambda _customer: pytest.fail("ERP lookup must not run"),
    )

    with pytest.raises(HTTPException) as missing_user:
        remap_customer_materials_route(
            ingestion.ingestion_id,
            CustomerMaterialRemapRequest(preview_data=_preview()),
            _request_for_user(None),
        )
    with pytest.raises(HTTPException) as wrong_user:
        remap_customer_materials_route(
            ingestion.ingestion_id,
            CustomerMaterialRemapRequest(preview_data=_preview()),
            _request_for_user("58"),
        )

    ingestion.status = IngestionStatus.DRAFT_CREATED
    with pytest.raises(HTTPException) as completed:
        remap_customer_materials_route(
            ingestion.ingestion_id,
            CustomerMaterialRemapRequest(preview_data=_preview()),
            _request_for_user("31"),
        )

    assert missing_user.value.status_code == 401
    assert wrong_user.value.status_code == 403
    assert completed.value.status_code == 409


def test_manual_remap_requires_customer_name_before_erp_lookup(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    store.ingestions[ingestion.ingestion_id] = ingestion
    preview = _preview()
    preview.order.customerName = ""
    monkeypatch.setattr(
        "app.store.erp_client.get_customer_material_details_by_customer",
        lambda _customer: pytest.fail("ERP lookup must not run without customer name"),
    )

    with pytest.raises(HTTPException) as exc:
        remap_customer_materials_route(
            ingestion.ingestion_id,
            CustomerMaterialRemapRequest(preview_data=preview),
            _request_for_user("31"),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "CUSTOMER_NAME_REQUIRED_FOR_MATERIAL_REMAP"


def test_manual_remap_lookup_failure_returns_503_without_mutating_order(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    original = ingestion.model_dump(mode="json")
    store.ingestions[ingestion.ingestion_id] = ingestion
    monkeypatch.setattr(
        "app.store.erp_client.get_customer_material_details_by_customer",
        lambda _customer: (_ for _ in ()).throw(RuntimeError("ERP down")),
    )

    with pytest.raises(HTTPException) as exc:
        remap_customer_materials_route(
            ingestion.ingestion_id,
            CustomerMaterialRemapRequest(preview_data=_preview()),
            _request_for_user("31"),
        )

    assert exc.value.status_code == 503
    assert store.ingestions[ingestion.ingestion_id].model_dump(mode="json") == original


def test_manual_remap_database_path_commits_updated_ingestion(monkeypatch: pytest.MonkeyPatch):
    ingestion = _ingestion()
    preview = _preview().model_copy(update={"details": [_preview().details[0]]})
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
        "app.store.ingestion_db.update_existing_ingestion",
        lambda _session, updated: actions.append(("upsert", updated.preview_data.details[0].materialCode)) or True,
    )
    monkeypatch.setattr(
        "app.store.erp_client.get_customer_material_details_by_customer",
        lambda _customer: [{"custMaterialCode": "A100D", "materialNumber": "ERP-DB"}],
    )

    result = remap_customer_materials_for_ingestion(ingestion.ingestion_id, preview)

    assert result is not None
    assert result.ingestion.preview_data.details[0].materialCode == "ERP-DB"
    assert actions == [("upsert", "ERP-DB"), "commit", "close"]

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm_extract import (
    _append_llm_quality_issues,
    _clip_llm_document_context,
    _extract_json,
    _extract_json_with_repair,
    _llm_extract_timeout_seconds,
    _purchase_order_to_preview,
    try_apply_llm_preview,
)
from app.schemas import IngestionResponse, IngestionStatus, PurchaseOrder


def test_purchase_order_schema_parses_numeric_strings_and_percent() -> None:
    order = PurchaseOrder.model_validate(
        {
            "order_number": "PO-001",
            "purchaser_name": "买方公司",
            "supplier_name": "供方公司",
            "tax_rate": "13%",
            "total_order_amount": "1,130.50",
            "items": [
                {
                    "material_code": "M001",
                    "quantity": "10",
                    "unit_price_without_tax": "100",
                    "unit_price_with_tax": "113",
                    "total_amount": "",
                }
            ],
        }
    )

    assert order.tax_rate == 13
    assert order.total_order_amount == 1130.5
    assert order.items[0].quantity == 10


def test_purchase_order_maps_to_order_preview_for_datynk_sale_order() -> None:
    order = PurchaseOrder.model_validate(
        {
            "order_number": "PO-001",
            "purchaser_name": "买方公司",
            "supplier_name": "供方公司",
            "order_date": "2026-05-22",
            "payment_terms": "月结",
            "tax_rate": 13,
            "delivery_address": "上海市测试路1号",
            "items": [
                {
                    "material_code": "S01P019430",
                    "material_name": "压缩弹簧",
                    "specification": "7*55*122",
                    "material_texture": "60Si2Mn",
                    "quantity": 2,
                    "unit": "件",
                    "unit_price_without_tax": 10,
                    "unit_price_with_tax": 11.3,
                    "total_amount_without_tax": 20,
                    "total_amount_with_tax": 22.6,
                    "delivery_date": "2026-06-01",
                    "drawing_number": "DR-01",
                }
            ],
        }
    )

    preview = _purchase_order_to_preview(order, "英科1厂")

    assert preview.order.customerName == "买方公司"
    assert preview.order.customerPoNo == "PO-001"
    assert preview.order.deliveryDate == "2026-06-01"
    assert preview.details[0].materialCode == "S01P019430"
    assert preview.details[0].amount == 20
    assert preview.details[0].allAmount == 22.6
    assert "图号/生产单号：DR-01" in preview.details[0].remark


def test_purchase_order_preview_does_not_infer_missing_tax_amounts() -> None:
    order = PurchaseOrder.model_validate(
        {
            "tax_rate": 13,
            "items": [
                {
                    "material_code": "M1",
                    "quantity": 2,
                    "unit_price_without_tax": 10,
                    "total_amount_without_tax": 20,
                },
                {
                    "material_code": "M2",
                    "quantity": 2,
                    "unit_price_with_tax": 11.3,
                    "total_amount_with_tax": 22.6,
                },
            ],
        }
    )

    preview = _purchase_order_to_preview(order, "org-test")

    assert preview.details[0].price == 10
    assert preview.details[0].amount == 20
    assert preview.details[0].taxPrice is None
    assert preview.details[0].allAmount is None
    assert preview.details[1].price is None
    assert preview.details[1].amount is None
    assert preview.details[1].taxPrice == 11.3
    assert preview.details[1].allAmount == 22.6


def test_extract_json_accepts_fenced_json_with_extra_text() -> None:
    parsed = _extract_json(
        '解析结果如下：\n```json\n{"order_number":"4501825923","items":[]}\n```\n请核对'
    )

    assert parsed["order_number"] == "4501825923"


def test_extract_json_escapes_literal_newline_inside_string() -> None:
    parsed = _extract_json('{"order_number":"4501825923","delivery_address":"苏州新区\n泰山路666号","items":[]}')

    assert parsed["delivery_address"] == "苏州新区\n泰山路666号"


def test_extract_json_uses_first_balanced_object() -> None:
    parsed = _extract_json('前缀 {"order_number":"4501825923","items":[{"material_code":"M1"}]} 后缀 {"ignored":true}')

    assert parsed["items"][0]["material_code"] == "M1"


def test_extract_json_with_repair_skips_repair_when_valid(monkeypatch) -> None:
    called = {"repair": False}

    def _fake_repair(*_args, **_kwargs):
        called["repair"] = True
        return "{}"

    monkeypatch.setattr("app.llm_extract._repair_llm_json", _fake_repair)

    parsed, repaired = _extract_json_with_repair('{"order_number":"PO-1","items":[]}')

    assert parsed["order_number"] == "PO-1"
    assert repaired is False
    assert called["repair"] is False


def test_extract_json_with_repair_repairs_malformed_json(monkeypatch) -> None:
    calls = {"repair": 0}

    def _fake_repair(raw_content, parse_error):
        calls["repair"] += 1
        assert "PO-1" in raw_content
        assert parse_error
        return '{"order_number":"PO-1","items":[]}'

    monkeypatch.setattr("app.llm_extract._repair_llm_json", _fake_repair)

    parsed, repaired = _extract_json_with_repair('{"order_number":"PO-1","items":[')

    assert parsed["order_number"] == "PO-1"
    assert repaired is True
    assert calls["repair"] == 1


def test_llm_context_clip_keeps_order_lines_and_drops_noise(monkeypatch) -> None:
    monkeypatch.setenv("LLM_EXTRACT_CONTEXT_MAX_CHARS", "1200")
    noise = "\n".join(f"legal clause filler {i} " + ("x" * 80) for i in range(80))
    text = "\n".join(
        [
            "Purchase Order PO-8899",
            noise,
            "Customer: ACME Manufacturing",
            "Material Code M-100 Quantity 24 Unit Price 9.5 Amount 228",
            "Delivery Date 2026-06-01",
            "footer text",
        ]
    )

    clipped = _clip_llm_document_context(text, {"customerPoNo": "PO-8899"})

    assert len(clipped) <= 1200
    assert "PO-8899" in clipped
    assert "Material Code M-100" in clipped
    assert "legal clause filler 79" not in clipped


def test_llm_extract_timeout_uses_specific_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("LLM_EXTRACT_TIMEOUT_SECONDS", "240")

    assert _llm_extract_timeout_seconds() == 240


def test_purchase_order_schema_accepts_evidence_and_uncertain_fields() -> None:
    parsed = _extract_json(
        '{"purchase_order":{"order_number":"PO-009","items":[{"material_code":"M1","quantity":2,'
        '"evidence":{"material_code":{"source_text":"料号 M1","confidence":0.92}},'
        '"uncertain_fields":["unit_price_without_tax"]}],"evidence":{"order_number":{"source_text":"订单号 PO-009",'
        '"confidence":0.99}},"uncertain_fields":["supplier_name"],"extraction_notes":["金额不一致"]}}'
    )

    order = PurchaseOrder.model_validate(parsed["purchase_order"])

    assert order.order_number == "PO-009"
    assert order.uncertain_fields == ["supplier_name"]
    assert order.items[0].evidence["material_code"]["confidence"] == 0.92
    assert order.items[0].uncertain_fields == ["unit_price_without_tax"]


def test_llm_quality_issues_marks_uncertain_and_amount_mismatch() -> None:
    order = PurchaseOrder.model_validate(
        {
            "order_number": "PO-010",
            "uncertain_fields": ["supplier_name"],
            "items": [
                {
                    "material_code": "M1",
                    "quantity": 2,
                    "unit_price_without_tax": 10,
                    "total_amount": 25,
                    "evidence": {"material_code": {"source_text": "料号 M1", "confidence": 0.7}},
                }
            ],
        }
    )
    ingestion = IngestionResponse(
        ingestion_id="ing-test",
        file_id="file-test",
        file_hash="hash-test",
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock",
        prompt_version="prompt",
        status=IngestionStatus.EXTRACTED,
        missing_fields=[],
        resolved_fields={},
        audit_events=[],
    )

    _append_llm_quality_issues(ingestion, order)

    messages = [issue.message for issue in ingestion.issues]
    assert any("supplier_name" in message for message in messages)
    assert any("置信度较低" in message for message in messages)
    assert any("金额与数量" in message for message in messages)


def _ingestion_with_fields(fields: dict[str, str]) -> IngestionResponse:
    return IngestionResponse(
        ingestion_id="ing-llm-quality",
        file_id="file-llm-quality",
        file_hash="hash-llm-quality",
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock",
        prompt_version="prompt",
        status=IngestionStatus.EXTRACTED,
        missing_fields=[],
        resolved_fields=dict(fields),
        audit_events=[],
    )


def test_try_apply_llm_preview_keeps_rule_preview_when_llm_is_empty(monkeypatch) -> None:
    ingestion = _ingestion_with_fields(
        {
            "customerName": "Global-set Valve Components Jiangsu Co., LTD",
            "customerPoNo": "POGSVC2600205",
            "doc_date": "2026-03-06",
            "currency": "CNY",
            "delivery_date": "2026-03-27",
            "line_items_json": (
                '[{"inventory_code":"SOGEYC2600","name":"sooson00s","productSpec":"13.5x27.3 X-750",'
                '"quantity":"5000","unit_price_excl_tax":"4.9","line_amount_excl_tax":"24500"}]'
            ),
        }
    )

    monkeypatch.setattr("app.llm_extract.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.llm_extract.chat_completion_json",
        lambda *_args, **_kwargs: '{"purchase_order":{"order_number":"","purchaser_name":"","order_date":"","items":[]}}',
    )

    applied = try_apply_llm_preview(ingestion, "Purchase Order POGSVC2600205")

    assert applied is False
    assert ingestion.preview_data is None
    assert ingestion.resolved_fields["customerName"] == "Global-set Valve Components Jiangsu Co., LTD"
    assert ingestion.resolved_fields["customerPoNo"] == "POGSVC2600205"
    assert ingestion.resolved_fields["doc_date"] == "2026-03-06"
    assert ingestion.resolved_fields["line_items_json"]
    assert any("已保留规则预览" in issue.message for issue in ingestion.issues)


def test_try_apply_llm_preview_applies_llm_when_more_complete(monkeypatch) -> None:
    ingestion = _ingestion_with_fields({"customerPoNo": "PO-ROUGH"})

    monkeypatch.setattr("app.llm_extract.llm_available", lambda: True)
    monkeypatch.setattr(
        "app.llm_extract.chat_completion_json",
        lambda *_args, **_kwargs: (
            '{"purchase_order":{"order_number":"PO-BETTER","purchaser_name":"Better Customer",'
            '"order_date":"2026-04-01","delivery_address":"Delivery Road",'
            '"items":[{"material_code":"MAT-001","material_name":"Spring","specification":"10x20",'
            '"quantity":12,"unit_price_without_tax":3.5,"total_amount_without_tax":42,'
            '"delivery_date":"2026-04-15"}]}}'
        ),
    )

    applied = try_apply_llm_preview(ingestion, "Purchase Order PO-BETTER MAT-001")

    assert applied is True
    assert ingestion.preview_data is not None
    assert ingestion.preview_data.order.customerName == "Better Customer"
    assert ingestion.preview_data.order.customerPoNo == "PO-BETTER"
    assert ingestion.preview_data.order.orderDate == "2026-04-01"
    assert ingestion.preview_data.details[0].materialCode == "MAT-001"
    assert ingestion.preview_data.details[0].qty == 12
    assert ingestion.resolved_fields["customerPoNo"] == "PO-BETTER"

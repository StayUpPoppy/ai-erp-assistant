from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.customer_identity import resolve_customer_identity
from app.english_order_enhanced import (
    detect_english_order_route,
    load_material_candidate_groups,
    load_organization_candidates,
    save_english_extraction_candidates,
)
from app.order_preview import apply_customer_material_mapping
from app.qwen_vision_extract import ENGLISH_VISION_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT, VisionImage, _parse_purchase_order
from app.schemas import IngestionResponse, IngestionStatus, OrderPreviewData, OrderPreviewDetail, OrderPreviewHeader, PurchaseOrder


@pytest.mark.parametrize(
    "sample_name,table_text",
    [
        ("flowserve_suzhou", "Line | Part Number / Description | Delivery Date | Quantity | UOM | Unit Price"),
        ("flowserve_india", "ITEM | PART NUMBER / DESCRIPTION | NEED-BY DATE | QUANTITY | UNIT | UNIT PRICE"),
        ("gea", "Material | Description | Quantity | Unit | Unitprice | Required in house date"),
        ("flowserve_germany", "Item | Part No. | Quantity | Unit | Description | Net Price | Delivery date"),
        ("emerson", "Line | Part Number / Revision / Description | Delivery Date | Quantity | UOM | Unit Price"),
        ("flowserve_netherlands", "PO line | Material no | Description | Quantity | Unit | Unit price | Shipment date"),
        ("bray_bilingual", "物料 | Description | Quantity | Unit Price | Delivery Date | Customer Part No."),
    ],
)
def test_known_english_order_header_families_route_to_enhanced(sample_name: str, table_text: str) -> None:
    route = detect_english_order_route(table_text)

    assert route.is_enhanced, sample_name
    assert route.primary_headers
    assert len(route.supporting_headers) >= 2


def test_chinese_order_and_insufficient_text_keep_current_route() -> None:
    chinese = detect_english_order_route("物料编码 | 物料名称 | 规格型号 | 数量 | 单价 | 交货日期")
    insufficient = detect_english_order_route("Purchase Order\nSupplier: Incospring\nTotal amount")

    assert chinese.route == "zh_current"
    assert insufficient.route == "zh_current"


def test_english_qwen_prompt_keeps_english_company_name_and_exposes_candidates() -> None:
    assert "没有中文而清空英文公司名称" in ENGLISH_VISION_SYSTEM_PROMPT
    assert "organization_candidates" in ENGLISH_VISION_SYSTEM_PROMPT
    assert "material_code_candidates" in ENGLISH_VISION_SYSTEM_PROMPT
    assert "如果原图没有中文证据，保持空字符串" in VISION_SYSTEM_PROMPT

    parsed = _parse_purchase_order(
        '{"purchase_order":{"purchaser_name":"Flowserve India Controls","supplier_name":"Incospring (ZheJiang) Co Ltd","items":[]}}',
        english_enhanced=True,
    )
    assert parsed.purchaser_name == "Flowserve India Controls"


def test_english_qwen_request_uses_the_isolated_system_prompt(monkeypatch) -> None:
    from app.qwen_vision_extract import _chat_completion_vision

    captured = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}'

    def _fake_urlopen(req, timeout):
        _ = timeout
        import json

        captured.append(json.loads(req.data.decode("utf-8")))
        return _Response()

    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr("app.qwen_vision_extract.request.urlopen", _fake_urlopen)
    _chat_completion_vision(
        [VisionImage(bytes=b"image", mime_type="image/jpeg", page_number=1)],
        file_name="english.pdf",
        page_count=1,
        truncated=False,
        english_enhanced=True,
    )

    assert captured[0]["messages"][0]["content"] == ENGLISH_VISION_SYSTEM_PROMPT


def test_english_text_llm_uses_isolated_prompt_and_persists_only_internal_candidates(monkeypatch) -> None:
    from app.llm_extract import ENGLISH_SYSTEM_PROMPT, try_apply_llm_preview

    captured = []

    def _fake_chat(messages, **_kwargs):
        captured.append(messages)
        return (
            '{"purchase_order":{"order_number":"PO-100","purchaser_name":"External Buyer Ltd",'
            '"supplier_name":"Incospring (ZheJiang) Co Ltd","items":[{"material_code":"PN-100",'
            '"specification":"10 x 20","quantity":1,"material_code_candidates":[{"value":"PN-100",'
            '"kind":"material_code","source_label":"Part Number"}]}],"organization_candidates":['
            '{"name":"External Buyer Ltd","role":"buyer","source_label":"Buyer"},'
            '{"name":"Incospring (ZheJiang) Co Ltd","role":"supplier","source_label":"Supplier"}]}}'
        )

    ingestion = IngestionResponse(
        ingestion_id="english-llm",
        file_id="file",
        file_hash="hash",
        user_id="user",
        org_id="英科1厂",
        extract_version="v0",
        model_version="",
        prompt_version="",
        status=IngestionStatus.UPLOADED,
    )
    monkeypatch.setattr("app.llm_extract.llm_available", lambda: True)
    monkeypatch.setattr("app.llm_extract.chat_completion_json", _fake_chat)

    applied = try_apply_llm_preview(
        ingestion,
        "Purchase Order\nBuyer: External Buyer Ltd\nPart Number | Description | Quantity | Unit Price\nPN-100 | Spring | 1 | 5",
    )

    assert applied is True
    assert captured[0][0]["content"] == ENGLISH_SYSTEM_PROMPT
    assert ingestion.resolved_fields["english_order_language_route"] == "en_enhanced"
    assert "english_organization_candidates_json" in ingestion.resolved_fields
    assert "english_material_code_candidates_json" in ingestion.resolved_fields
    assert ingestion.preview_data is not None
    assert not hasattr(ingestion.preview_data.details[0], "material_code_candidates")


def test_english_candidates_are_internal_and_preserve_raw_values() -> None:
    order = PurchaseOrder.model_validate(
        {
            "purchaser_name": "Flowserve Fluid Motion and Control (Suzhou) Co., Ltd.",
            "supplier_name": "Yingke Holding Co Ltd",
            "organization_candidates": [
                {"name": "Flowserve Fluid Motion and Control (Suzhou) Co., Ltd.", "role": "buyer", "source_label": "Buyer", "page": 1, "confidence": 0.98},
                {"name": "Yingke Holding Co Ltd", "role": "supplier", "source_label": "Supplier", "page": 1, "confidence": 0.98},
            ],
            "items": [
                {
                    "material_code": "CG5-DNS-55-LYB",
                    "specification": "φ7.5 × φ1.5 (8 rings), Inconel X-750",
                    "material_code_candidates": [
                        {"value": "CG5-DNS-55-LYB\n", "kind": "material_code", "source_label": "Part Number", "page": 1, "confidence": 0.99},
                        {"value": "CG5-DNS-55-LYB REV D", "kind": "material_code_with_revision", "source_label": "Part Number + Revision", "page": 1, "confidence": 0.91},
                        {"value": "10", "kind": "material_code", "source_label": "Line", "page": 1, "confidence": 0.99},
                        {"value": "T04004", "kind": "material_code", "source_label": "Drawing Number", "page": 1, "confidence": 0.99},
                    ],
                }
            ],
        }
    )
    fields: dict[str, str] = {}
    route = detect_english_order_route("Part Number | Description | Quantity | Unit Price")
    save_english_extraction_candidates(fields, order, route)

    companies = load_organization_candidates(fields)
    groups = load_material_candidate_groups(fields, 1)
    assert companies[0]["name"] == "Flowserve Fluid Motion and Control (Suzhou) Co., Ltd."
    assert groups[0][0]["value"] == "CG5-DNS-55-LYB"
    assert any(candidate["value"] == "CG5-DNS-55-LYB REV D" for candidate in groups[0])
    assert any(candidate["value"] == "φ7.5 × φ1.5 (8 rings), Inconel X-750" for candidate in groups[0])
    assert all(candidate["value"] not in {"10", "T04004"} for candidate in groups[0])


class _CustomerErp:
    def search_customers(self, _org_id: str, keyword: str, _page: int, _size: int):
        if keyword == "External Buyer Ltd":
            return []
        return []


def test_english_customer_resolution_prefers_the_only_external_buyer() -> None:
    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="",
        supplier_name="Incospring (ZheJiang) Co Ltd",
        organization_candidates=[
            {"name": "Incospring (ZheJiang) Co Ltd", "role": "supplier"},
            {"name": "External Buyer Ltd", "role": "buyer"},
            {"name": "External Buyer Warehouse", "role": "ship_to"},
        ],
        erp=_CustomerErp(),
    )

    assert result.customer_name == "External Buyer Ltd"
    assert result.resolution_source == "sole_external_buyer"


def test_english_material_candidates_try_revision_then_specification() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName="External Buyer Ltd"),
        details=[
            OrderPreviewDetail(materialCode="PN-100", productSpec="10 x 20 x 3", qty=1),
            OrderPreviewDetail(materialCode="", productSpec="25.4 x 3.2 x 1.2, Inconel 718", qty=2),
        ],
    )
    mapping_rows = [
        {"custMaterialCode": "PN-100 REV D", "materialNumber": "ERP-REV", "materialName": "Revision Material", "materialModel": "M-REV", "ph": "X750"},
        {"custMaterialCode": "25.4 x 3.2 x 1.2, Inconel 718", "materialNumber": "ERP-SPEC", "materialName": "Spec Material", "materialModel": "M-SPEC", "ph": "718"},
    ]
    candidates = [
        [
            {"value": "PN-100", "kind": "material_code"},
            {"value": "PN-100 REV D", "kind": "material_code_with_revision"},
            {"value": "10 x 20 x 3", "kind": "specification"},
        ],
        [{"value": "25.4 x 3.2 x 1.2, Inconel 718", "kind": "specification"}],
    ]

    mapped, metrics, issues = apply_customer_material_mapping(preview, mapping_rows, candidate_groups=candidates)

    assert not issues
    assert mapped.details[0].customerMaterialNo == "PN-100 REV D"
    assert mapped.details[0].materialCode == "ERP-REV"
    assert mapped.details[1].customerMaterialNo == "25.4 x 3.2 x 1.2, Inconel 718"
    assert mapped.details[1].materialCode == "ERP-SPEC"
    assert metrics["matched"] == 2
    assert metrics["ambiguous"] == 0


def test_english_material_candidate_multiple_erp_hits_are_blocked() -> None:
    preview = OrderPreviewData(details=[OrderPreviewDetail(materialCode="PN-100", productName="old", productSpec="old", ph="old", qty=1)])
    mapped, metrics, issues = apply_customer_material_mapping(
        preview,
        [
            {"custMaterialCode": "PN-100", "materialNumber": "ERP-A"},
            {"custMaterialCode": "PN-100 REV D", "materialNumber": "ERP-B"},
        ],
        candidate_groups=[[
            {"value": "PN-100", "kind": "material_code"},
            {"value": "PN-100 REV D", "kind": "material_code"},
        ]],
    )

    assert mapped.details[0].materialCode == ""
    assert mapped.details[0].productName == ""
    assert metrics["ambiguous"] == 1
    assert "多个 ERP 内部物料" in issues[0].message

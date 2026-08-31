import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_payload_preview import build_datynk_sale_order_payload
from app.ingestion_db import new_row_from_ingestion, row_to_ingestion
from app.order_preview import (
    apply_customer_material_mapping,
    apply_preview_to_ingestion,
    build_order_preview_data,
    normalize_customer_material_code,
    preserve_customer_material_numbers_from_sources,
    preview_issues,
    preview_missing_keys,
    preview_to_resolved_fields,
)
from app.schemas import IngestionResponse, IngestionStatus, OrderPreviewData, OrderPreviewDetail, OrderPreviewHeader


def test_preserve_customer_material_numbers_from_sources_moves_only_the_material_code() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName=""),
        details=[
            OrderPreviewDetail(
                customerMaterialNo="",
                sourceMaterialCode="  98S00H-720015JU/HQU3  ",
                materialCode="INTERNAL-IGNORED",
                productName="Spring A",
                productSpec="Spec A",
                ph="X750",
                qty=10,
                price=20,
            ),
            OrderPreviewDetail(
                customerMaterialNo="",
                sourceMaterialCode="   ",
                materialCode="98S00E-720025JT/HQU3",
                productName="Spring B",
                productSpec="Spec B",
                ph="718",
                qty=12,
            ),
            OrderPreviewDetail(customerMaterialNo="MANUAL-KEEP", sourceMaterialCode="SOURCE-IGNORE", materialCode="M-3", qty=3),
            OrderPreviewDetail(customerMaterialNo="", sourceMaterialCode="", materialCode="", qty=4),
        ],
    )

    preserved, count = preserve_customer_material_numbers_from_sources(preview)

    assert count == 2
    assert [detail.customerMaterialNo for detail in preserved.details] == [
        "98S00H-720015JU/HQU3",
        "98S00E-720025JT/HQU3",
        "MANUAL-KEEP",
        "",
    ]
    first = preserved.details[0]
    assert (first.materialCode, first.productName, first.productSpec, first.ph, first.qty, first.price) == (
        "",
        "Spring A",
        "Spec A",
        "X750",
        10,
        20,
    )

    ingestion = IngestionResponse(
        ingestion_id="ing-preserve-customer-material",
        file_id="file",
        file_hash="hash",
        user_id="user",
        org_id="英科1厂",
        extract_version="v0",
        model_version="model",
        prompt_version="prompt",
        status=IngestionStatus.NEED_USER_INPUT,
        resolved_fields=preview_to_resolved_fields(preserved),
    )
    rebuilt = build_order_preview_data(ingestion)
    assert rebuilt is not None
    assert [detail.customerMaterialNo for detail in rebuilt.details] == [
        "98S00H-720015JU/HQU3",
        "98S00E-720025JT/HQU3",
        "MANUAL-KEEP",
        "",
    ]
    assert [detail.materialCode for detail in rebuilt.details] == ["", "", "M-3", ""]


def test_datynk_payload_preview_matches_order_interface_fields() -> None:
    ing = IngestionResponse(
        ingestion_id="ing-1",
        file_id="file-1",
        file_hash="hash-1",
        user_id="u1",
        org_id="英科1厂",
        extract_version="v0",
        model_version="m",
        prompt_version="p",
        status=IngestionStatus.VALIDATED,
        resolved_fields={
            "extracted_purchaser_name": "浙江英科弹簧科技有限公司",
            "extracted_supplier_name": "北京优向国际能源装备有限公司",
        },
        preview_data=OrderPreviewData(
            order=OrderPreviewHeader(
                org="英科1厂",
                customerName="北京优向国际能源装备有限公司",
                customerPoNo="111111",
                salesUser="顾晓龄",
                orderDate="2026-05-13",
                orderStatus="pending",
                deliveryAddr="望京园402号楼12层1507",
                rate=1,
                currency="CNY",
                deliveryDate="2026-05-13",
            ),
            details=[
                OrderPreviewDetail(
                    materialCode="S01P019430",
                    sourceMaterialCode="PDF-M001",
                    productName="压缩弹簧",
                    productSpec="左旋7*55*122*8.5",
                    sourceProductSpec="φ7×φ55×122（8.5圈）",
                    ph="60Si2Mn",
                    qty=1,
                    price=1.7699115044247788,
                    taxPrice=2,
                    amount=1.7699115044247788,
                    allAmount=2,
                    tax=13,
                    taxAmount=0.23008849557522115,
                    gift=False,
                    remark="明细业务备注",
                )
            ],
        ),
    )

    payload = build_datynk_sale_order_payload(ing)

    assert set(payload["order"]) == {
        "org",
        "customerName",
        "customerPoNo",
        "remark",
        "salesUser",
        "createUser",
        "orderDate",
        "orderStatus",
        "deliveryAddr",
        "rate",
        "currency",
        "deliveryDate",
    }
    assert "jhq" not in payload["order"]
    assert payload["order"]["remark"] == "来源AI助手"
    assert payload["order"]["createUser"] == payload["order"]["salesUser"]
    assert payload["order"]["rate"] == 1.0
    assert payload["details"][0]["materialCode"] == "S01P019430"
    assert payload["details"][0]["productSpec"] == "左旋7*55*122*8.5"
    assert payload["details"][0]["remark"] == "明细业务备注"
    assert "sourceMaterialCode" not in payload["details"][0]
    assert "sourceProductSpec" not in payload["details"][0]
    assert "customerMaterialSpec" not in payload["details"][0]
    assert "extracted_purchaser_name" not in payload["order"]
    assert "extracted_supplier_name" not in payload["order"]


def test_source_product_spec_persists_in_preview_context_but_not_erp_details() -> None:
    ing = IngestionResponse(
        ingestion_id="ing-source-spec",
        file_id="file-source-spec",
        file_hash="hash-source-spec",
        user_id="u1",
        org_id="英科1厂",
        extract_version="v0",
        model_version="m",
        prompt_version="p",
        status=IngestionStatus.NEED_USER_INPUT,
        preview_data=OrderPreviewData(
            order=OrderPreviewHeader(customerName="Acme"),
            details=[
                OrderPreviewDetail(
                    materialCode="M001",
                    sourceMaterialCode="PDF-M001\n原始编码第二行",
                    productSpec="ERP 规格",
                    sourceProductSpec="φ7.5 × φ1.5（8圈）\nInconel 750",
                    qty=2,
                )
            ],
        ),
    )

    row = new_row_from_ingestion(ing)
    restored = row_to_ingestion(row)

    assert restored.preview_data is not None
    assert restored.preview_data.details[0].sourceMaterialCode == "PDF-M001\n原始编码第二行"
    assert restored.preview_data.details[0].sourceProductSpec == "φ7.5 × φ1.5（8圈）\nInconel 750"
    resolved_fields = preview_to_resolved_fields(restored.preview_data)
    stored_details = json.loads(resolved_fields["datynk_details_json"])
    assert "sourceMaterialCode" not in stored_details[0]
    assert "sourceProductSpec" not in stored_details[0]
    assert json.loads(resolved_fields["source_material_codes_json"]) == [
        "PDF-M001\n原始编码第二行"
    ]
    assert json.loads(resolved_fields["source_product_specs_json"]) == [
        "φ7.5 × φ1.5（8圈）\nInconel 750"
    ]

    rebuilt = build_order_preview_data(
        restored.model_copy(update={"preview_data": None, "resolved_fields": resolved_fields})
    )
    assert rebuilt is not None
    assert rebuilt.details[0].sourceMaterialCode == "PDF-M001\n原始编码第二行"
    assert rebuilt.details[0].sourceProductSpec == "φ7.5 × φ1.5（8圈）\nInconel 750"


def test_order_preview_keeps_tax_and_non_tax_fields_separate() -> None:
    ing = IngestionResponse(
        ingestion_id="ing-amount",
        file_id="file-amount",
        file_hash="hash-amount",
        user_id="u1",
        org_id="org1",
        extract_version="v0",
        model_version="m",
        prompt_version="p",
        status=IngestionStatus.EXTRACTED,
        resolved_fields={
            "line_items_json": (
                '[{"inventory_code":"M1","quantity":"2","unit_price_excl_tax":"10",'
                '"unit_price_incl_tax":"11.3","line_amount_excl_tax":"20","line_amount_incl_tax":"22.6"}]'
            )
        },
    )

    preview = build_order_preview_data(ing)

    assert preview is not None
    detail = preview.details[0]
    assert detail.price == 10
    assert detail.taxPrice == 11.3
    assert detail.amount == 20
    assert detail.allAmount == 22.6


def test_customer_material_mapping_exact_and_normalized_match() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName="Acme"),
        details=[
            OrderPreviewDetail(
                materialCode="N100",
                sourceMaterialCode="原始编码 A",
                productName="Old A",
                productSpec="Spec A",
                sourceProductSpec="原始规格 A",
                qty=1,
            ),
            OrderPreviewDetail(
                materialCode=" n-200 ",
                sourceMaterialCode="原始编码 B",
                productName="Old B",
                productSpec="Spec B",
                sourceProductSpec="原始规格 B",
                qty=2,
            ),
            OrderPreviewDetail(
                materialCode="X999",
                sourceMaterialCode="原始编码 C",
                productName="Old C",
                productSpec="Spec C",
                sourceProductSpec="原始规格 C",
                qty=3,
            ),
        ],
    )
    mapped, metrics, issues = apply_customer_material_mapping(
        preview,
        [
            {
                "custMaterialCode": "N100",
                "materialNumber": "S01P019433",
                "materialName": "Internal A",
                "materialModel": "Internal Spec A",
                "ph": "55CrSiA",
            },
            {
                "custMaterialCode": "N200",
                "materialNumber": "S01P019427",
                "materialName": "Internal B",
                "materialModel": "Internal Spec B",
                "ph": "60Si2Mn",
            },
        ],
    )

    assert mapped.details[0].customerMaterialNo == "N100"
    assert mapped.details[0].materialCode == "S01P019433"
    assert mapped.details[0].sourceMaterialCode == "原始编码 A"
    assert mapped.details[0].productName == "Internal A"
    assert mapped.details[0].productSpec == "Internal Spec A"
    assert mapped.details[0].sourceProductSpec == "原始规格 A"
    assert mapped.details[0].ph == "55CrSiA"
    assert mapped.details[1].customerMaterialNo == "n-200"
    assert mapped.details[1].materialCode == "S01P019427"
    assert mapped.details[1].sourceMaterialCode == "原始编码 B"
    assert mapped.details[1].productName == "Internal B"
    assert mapped.details[1].productSpec == "Internal Spec B"
    assert mapped.details[1].sourceProductSpec == "原始规格 B"
    assert mapped.details[1].ph == "60Si2Mn"
    assert mapped.details[2].customerMaterialNo == "X999"
    assert mapped.details[2].materialCode == ""
    assert mapped.details[2].sourceMaterialCode == "原始编码 C"
    assert mapped.details[2].productName == ""
    assert mapped.details[2].productSpec == ""
    assert mapped.details[2].sourceProductSpec == "原始规格 C"
    assert mapped.details[2].ph == ""
    assert mapped.details[2].qty == 3
    assert metrics == {"mapping_rows": 2, "matched": 2, "exact": 1, "normalized": 1, "unmatched": 1}
    assert len(issues) == 1
    assert issues[0].path == "details[2].materialCode"
    assert issues[0].level == "error"
    assert issues[0].message == (
        "客户物料编码 X999 未在 ERP 客户物料对应表中找到，"
        "请先到客户物料对应表创建客户物料与内部物料的对应关系，"
        "然后方可显示内部物料编码、物料名称、物料规格以及物料牌号等关系。"
    )


def test_customer_material_mapping_erp_blank_fields_clear_extracted_values() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName="Acme"),
        details=[
            OrderPreviewDetail(
                materialCode="N100",
                productName="Extracted Name",
                productSpec="Extracted Spec",
                ph="Extracted Grade",
                qty=2,
                price=10,
            )
        ],
    )

    mapped, metrics, issues = apply_customer_material_mapping(
        preview,
        [{"custMaterialCode": "N100", "materialNumber": "S01P019433"}],
    )

    assert mapped.details[0].customerMaterialNo == "N100"
    assert mapped.details[0].materialCode == "S01P019433"
    assert mapped.details[0].productName == ""
    assert mapped.details[0].productSpec == ""
    assert mapped.details[0].ph == ""
    assert mapped.details[0].qty == 2
    assert mapped.details[0].price == 10
    assert metrics == {"mapping_rows": 1, "matched": 1, "exact": 1, "normalized": 0, "unmatched": 0}
    assert issues == []


def test_normalize_customer_material_code_handles_full_width_and_separators() -> None:
    assert normalize_customer_material_code(" Ｎ-１００. ") == "N100"


def test_preview_issues_validate_tax_and_amount_relations() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(customerName="Acme"),
        details=[
            OrderPreviewDetail(
                materialCode="M001",
                qty=2,
                price=10,
                taxPrice=12,
                amount=25,
                allAmount=30,
                tax=13,
                taxAmount=1,
            )
        ],
    )

    paths = {issue.path for issue in preview_issues(preview)}

    assert "details[0].amount" in paths
    assert "details[0].allAmount" in paths
    assert "details[0].taxAmount" in paths
    assert "details[0].taxPrice" in paths


def test_preview_does_not_require_price_amount_and_tax_fields() -> None:
    preview = OrderPreviewData(
        order=OrderPreviewHeader(
            org="英科1厂",
            customerName="Acme",
            orderDate="2026-06-23",
            currency="CNY",
            deliveryDate="2026-06-30",
        ),
        details=[OrderPreviewDetail(materialCode="M001", qty=2)],
    )
    missing = preview_missing_keys(preview)

    assert not ({"price", "taxPrice", "amount", "allAmount", "tax"} & set(missing))

    ing = IngestionResponse(
        ingestion_id="ing-required-money",
        file_id="file-required-money",
        file_hash="hash-required-money",
        user_id="u1",
        org_id="英科1厂",
        extract_version="v0",
        model_version="m",
        prompt_version="p",
        status=IngestionStatus.EXTRACTED,
    )
    apply_preview_to_ingestion(ing, preview)

    assert not ({"price", "taxPrice", "amount", "allAmount", "tax"} & set(ing.missing_fields))

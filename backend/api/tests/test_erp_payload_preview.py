from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_payload_preview import build_datynk_sale_order_payload
from app.order_preview import (
    apply_customer_material_mapping,
    apply_preview_to_ingestion,
    build_order_preview_data,
    normalize_customer_material_code,
    preview_issues,
    preview_missing_keys,
)
from app.schemas import IngestionResponse, IngestionStatus, OrderPreviewData, OrderPreviewDetail, OrderPreviewHeader


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
                    productName="压缩弹簧",
                    productSpec="左旋7*55*122*8.5",
                    ph="60Si2Mn",
                    qty=1,
                    price=1.7699115044247788,
                    taxPrice=2,
                    amount=1.7699115044247788,
                    allAmount=2,
                    tax=13,
                    taxAmount=0.23008849557522115,
                    gift=False,
                    remark="",
                )
            ],
        ),
    )

    payload = build_datynk_sale_order_payload(ing)

    assert set(payload["order"]) == {
        "org",
        "customerName",
        "customerPoNo",
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
    assert payload["order"]["createUser"] == payload["order"]["salesUser"]
    assert payload["order"]["rate"] == 1.0
    assert payload["details"][0]["materialCode"] == "S01P019430"


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
            OrderPreviewDetail(materialCode="N100", productName="Old A", productSpec="Spec A", qty=1),
            OrderPreviewDetail(materialCode=" n-200 ", productName="Old B", productSpec="Spec B", qty=2),
            OrderPreviewDetail(materialCode="X999", productName="Old C", productSpec="Spec C", qty=3),
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
    assert mapped.details[0].productName == "Old A"
    assert mapped.details[0].productSpec == "Spec A"
    assert mapped.details[0].ph == ""
    assert mapped.details[1].customerMaterialNo == "n-200"
    assert mapped.details[1].materialCode == "S01P019427"
    assert mapped.details[1].productName == "Old B"
    assert mapped.details[1].productSpec == "Spec B"
    assert mapped.details[2].customerMaterialNo == "X999"
    assert mapped.details[2].materialCode == ""
    assert metrics == {"mapping_rows": 2, "matched": 2, "exact": 1, "normalized": 1, "unmatched": 1}
    assert len(issues) == 1
    assert issues[0].path == "details[2].materialCode"
    assert issues[0].level == "error"
    assert "客户物料对应表" in issues[0].message


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

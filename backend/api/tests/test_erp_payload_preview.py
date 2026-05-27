from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_payload_preview import build_datynk_sale_order_payload
from app.order_preview import build_order_preview_data
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
        "orderDate",
        "orderStatus",
        "deliveryAddr",
        "rate",
        "currency",
        "deliveryDate",
    }
    assert "jhq" not in payload["order"]
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

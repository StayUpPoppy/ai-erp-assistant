from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qwen_vision_extract import (
    VisionImage,
    is_qwen_vision_supported_file,
    qwen_vision_health_payload,
    try_apply_qwen_vision_preview,
)
from app.schemas import IngestionResponse, IngestionStatus


def _new_ingestion() -> IngestionResponse:
    return IngestionResponse(
        ingestion_id="ing-qwen",
        file_id="file-qwen",
        file_hash="hash-qwen",
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
        status=IngestionStatus.UPLOADED,
    )


def test_qwen_vision_supported_file_signatures() -> None:
    assert is_qwen_vision_supported_file(b"%PDF-1.7\n", "order.bin", "application/octet-stream")
    assert is_qwen_vision_supported_file(b"\xff\xd8\xff\xe0", "order.bin", "application/octet-stream")
    assert is_qwen_vision_supported_file(b"\x89PNG\r\n\x1a\n", "order.bin", "application/octet-stream")
    assert not is_qwen_vision_supported_file(b"plain text", "order.txt", "text/plain")


def test_qwen_vision_health_payload_masks_key(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_FORCE_ALL", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")

    health = qwen_vision_health_payload()

    assert health["qwen_vision_extract_enabled"] is True
    assert health["qwen_vision_force_all"] is True
    assert health["qwen_vision_api_key_configured"] is True
    assert health["qwen_vision_include_local_text"] is True
    assert isinstance(health["qwen_vision_local_text_max_chars"], int)
    assert "secret" not in str(health)


def test_qwen_vision_user_prompt_includes_local_parse_text(monkeypatch) -> None:
    from app.qwen_vision_extract import _user_text_prompt

    monkeypatch.setenv("QWEN_VISION_INCLUDE_LOCAL_TEXT", "true")
    monkeypatch.setenv("QWEN_VISION_LOCAL_TEXT_MAX_CHARS", "200")

    prompt = _user_text_prompt(
        "order.pdf",
        2,
        False,
        local_text="| 物料名称 | 物料规格 | 物料牌号 |\n| 弹簧，16CD25 | ISO 27509 IX | INCONEL X-750 |",
        local_format="pdf_native_pymupdf",
    )

    assert "<local_parse_text format=\"pdf_native_pymupdf\">" in prompt
    assert "INCONEL X-750" in prompt


def test_try_apply_qwen_vision_preview_applies_structured_result(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr(
        "app.qwen_vision_extract._source_images",
        lambda *_args: ([VisionImage(bytes=b"img", mime_type="image/jpeg", page_number=1)], 1, False),
    )
    monkeypatch.setattr(
        "app.qwen_vision_extract._chat_completion_vision",
        lambda *_args, **_kwargs: """
        {"purchase_order":{"order_number":"PO-QWEN","purchaser_name":"Acme 格鲁赛特阀门配件江苏有限公司","supplier_name":"YingKe","order_date":"2026-06-26","payment_terms":"","tax_rate":13,"delivery_address":"Yao Lane 江苏省丹阳市埤城镇122省道尧巷段（212300）","total_order_amount":20,"items":[{"material_code":"CUST-001","material_name":"Spring","specification":"D10","material_texture":"X-750","quantity":2,"unit":"pcs","unit_price_without_tax":10,"unit_price_with_tax":11.3,"total_amount":20,"total_amount_without_tax":20,"total_amount_with_tax":22.6,"delivery_date":"2026-07-01","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}
        """,
    )
    ingestion = _new_ingestion()

    result = try_apply_qwen_vision_preview(ingestion, b"\xff\xd8\xff\xe0", "order.jpg", "image/jpeg")

    assert result.applied is True
    assert ingestion.preview_data is not None
    assert ingestion.preview_data.order.customerPoNo == "PO-QWEN"
    assert ingestion.preview_data.order.customerName == "格鲁赛特阀门配件江苏有限公司"
    assert ingestion.preview_data.order.deliveryAddr == "江苏省丹阳市埤城镇122省道尧巷段（212300）"
    assert ingestion.preview_data.details[0].materialCode == "CUST-001"
    assert ingestion.preview_data.details[0].ph == "X-750"
    assert ingestion.model_version == "qwen3.7-plus"
    assert ingestion.prompt_version == "qwen-vision-order-preview-v2"


def test_qwen_vision_fills_material_grade_from_local_table(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr(
        "app.qwen_vision_extract._source_images",
        lambda *_args: ([VisionImage(bytes=b"img", mime_type="image/jpeg", page_number=1)], 1, False),
    )
    monkeypatch.setattr(
        "app.qwen_vision_extract._chat_completion_vision",
        lambda *_args, **_kwargs: """
        {"purchase_order":{"order_number":"PO-QWEN","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"CUST-001","material_name":"弹簧","specification":"ISO 27509 IX","material_texture":"","quantity":2,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-002","material_name":"弹簧","specification":"特殊弹簧,左旋","material_texture":"","quantity":3,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}
        """,
    )
    ingestion = _new_ingestion()

    result = try_apply_qwen_vision_preview(
        ingestion,
        b"\xff\xd8\xff\xe0",
        "order.jpg",
        "image/jpeg",
        local_text=(
            "| 物料名称 | 物料规格 | 物料牌号 |\n"
            "| --- | --- | --- |\n"
            "| 弹簧，16CD25 | ISO 27509 IX | INCONEL X-750 |\n"
            "| 弹簧，3CD1 | 特殊弹簧,左旋 | INCONEL X-750 |\n"
        ),
        local_format="pdf_native_pymupdf",
    )

    assert result.applied is True
    assert ingestion.preview_data is not None
    assert [detail.ph for detail in ingestion.preview_data.details] == ["INCONEL X-750", "INCONEL X-750"]
    assert ingestion.preview_data.details[0].productSpec == "ISO 27509 IX"


def test_qwen_vision_overrides_spec_from_local_spec_column_and_keeps_full_name(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr(
        "app.qwen_vision_extract._source_images",
        lambda *_args: ([VisionImage(bytes=b"img", mime_type="image/jpeg", page_number=1)], 1, False),
    )
    monkeypatch.setattr(
        "app.qwen_vision_extract._chat_completion_vision",
        lambda *_args, **_kwargs: """
        {"purchase_order":{"order_number":"PO-QWEN","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"CUST-001","material_name":"弹簧","specification":"16CD25-ISO","material_texture":"","quantity":2,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-002","material_name":"弹簧","specification":"16CD25-ISO","material_texture":"","quantity":3,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-003","material_name":"弹簧","specification":"3CD1-SSP-LH","material_texture":"","quantity":4,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-004","material_name":"弹簧","specification":"3CD1-SSP-RH","material_texture":"","quantity":5,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-005","material_name":"弹簧","specification":"SNY3CS3","material_texture":"","quantity":6,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}
        """,
    )
    ingestion = _new_ingestion()

    result = try_apply_qwen_vision_preview(
        ingestion,
        b"\xff\xd8\xff\xe0",
        "order.jpg",
        "image/jpeg",
        local_text=(
            "| 产品名称 | 规格型号 |\n"
            "| --- | --- |\n"
            "| 弹簧，16CD25-ISO 27509 IX-RH，INCONEL X-750-NC | ISO 27509 IX,右旋 |\n"
            "| 弹簧，16CD25-ISO 27509 IX-LH，INCONEL X-750-NC | ISO 27509 IX,左旋 |\n"
            "| 弹簧，3CD1-SSP-LH，INCONEL X-750 | 特殊弹簧,左旋 |\n"
            "| 弹簧，3CD1-SSP-RH，INCONEL X-750 | 特殊弹簧,右旋 |\n"
            "| 弹簧，SNY3CS3，INCONEL X-750 | -- |\n"
        ),
        local_format="pdf_native_pymupdf",
    )

    assert result.applied is True
    assert ingestion.preview_data is not None
    assert [detail.productName for detail in ingestion.preview_data.details] == [
        "弹簧，16CD25-ISO 27509 IX-RH，INCONEL X-750-NC",
        "弹簧，16CD25-ISO 27509 IX-LH，INCONEL X-750-NC",
        "弹簧，3CD1-SSP-LH，INCONEL X-750",
        "弹簧，3CD1-SSP-RH，INCONEL X-750",
        "弹簧，SNY3CS3，INCONEL X-750",
    ]
    assert [detail.productSpec for detail in ingestion.preview_data.details] == [
        "ISO 27509 IX,右旋",
        "ISO 27509 IX,左旋",
        "特殊弹簧,左旋",
        "特殊弹簧,右旋",
        "",
    ]
    assert [detail.ph for detail in ingestion.preview_data.details] == [
        "INCONEL X-750-NC",
        "INCONEL X-750-NC",
        "INCONEL X-750",
        "INCONEL X-750",
        "INCONEL X-750",
    ]


def test_qwen_vision_splits_grade_from_spec_without_false_positive(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_VISION_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("QWEN_VISION_API_KEY", "secret")
    monkeypatch.setattr(
        "app.qwen_vision_extract._source_images",
        lambda *_args: ([VisionImage(bytes=b"img", mime_type="image/jpeg", page_number=1)], 1, False),
    )
    monkeypatch.setattr(
        "app.qwen_vision_extract._chat_completion_vision",
        lambda *_args, **_kwargs: """
        {"purchase_order":{"order_number":"PO-QWEN","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"CUST-001","material_name":"弹簧","specification":"13.5x27.3 INCONEL X-750","material_texture":"","quantity":2,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]},{"material_code":"CUST-002","material_name":"弹簧","specification":"16CD25-ISO","material_texture":"","quantity":3,"unit":"pcs","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}
        """,
    )
    ingestion = _new_ingestion()

    result = try_apply_qwen_vision_preview(ingestion, b"\xff\xd8\xff\xe0", "order.jpg", "image/jpeg")

    assert result.applied is True
    assert ingestion.preview_data is not None
    assert ingestion.preview_data.details[0].productSpec == "13.5x27.3"
    assert ingestion.preview_data.details[0].ph == "INCONEL X-750"
    assert ingestion.preview_data.details[1].productSpec == "16CD25-ISO"
    assert ingestion.preview_data.details[1].ph == ""


def test_qwen_vision_prompt_reuses_text_order_rules() -> None:
    from app.qwen_vision_extract import ORDER_EXTRACTION_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT

    assert VISION_SYSTEM_PROMPT.startswith(ORDER_EXTRACTION_SYSTEM_PROMPT)
    assert "你是制造业采购订单抽取引擎" in VISION_SYSTEM_PROMPT
    assert "不要根据税率在含税/不含税金额之间互相推算" in VISION_SYSTEM_PROMPT
    assert "quantity * unit_price_without_tax" in VISION_SYSTEM_PROMPT
    assert "视觉输入补充规则" in VISION_SYSTEM_PROMPT
    assert "物料牌号" in VISION_SYSTEM_PROMPT
    assert "Material Grade" in VISION_SYSTEM_PROMPT
    assert "规格型号/规格/型号/Spec/Specification" in VISION_SYSTEM_PROMPT
    assert "material_name 保留完整产品名称" in VISION_SYSTEM_PROMPT

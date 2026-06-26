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
    assert "secret" not in str(health)


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
        {"purchase_order":{"order_number":"PO-QWEN","purchaser_name":"Acme","supplier_name":"YingKe","order_date":"2026-06-26","payment_terms":"","tax_rate":13,"delivery_address":"Shanghai","total_order_amount":20,"items":[{"material_code":"CUST-001","material_name":"Spring","specification":"D10","material_texture":"X-750","quantity":2,"unit":"pcs","unit_price_without_tax":10,"unit_price_with_tax":11.3,"total_amount":20,"total_amount_without_tax":20,"total_amount_with_tax":22.6,"delivery_date":"2026-07-01","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}
        """,
    )
    ingestion = _new_ingestion()

    result = try_apply_qwen_vision_preview(ingestion, b"\xff\xd8\xff\xe0", "order.jpg", "image/jpeg")

    assert result.applied is True
    assert ingestion.preview_data is not None
    assert ingestion.preview_data.order.customerPoNo == "PO-QWEN"
    assert ingestion.preview_data.details[0].materialCode == "CUST-001"
    assert ingestion.model_version == "qwen3.7-plus"
    assert ingestion.prompt_version == "qwen-vision-order-preview-v1"

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document_extract import heuristic_fill_fields, heuristic_vendor_code
from app.ingestion_export import build_document_parse_export
from app.schemas import DocType, DocumentParseExport, IngestionResponse, IngestionStatus


def test_heuristic_currency_cny_chinese() -> None:
    text = "采购订单\n币种：人民币元\n日期 2026-03-04\n"
    got = heuristic_fill_fields(text)
    assert got.get("currency") == "CNY"
    assert got.get("doc_date") == "2026-03-04"


def test_heuristic_doc_date_chinese() -> None:
    text = "订单\n签订日期：2026年3月4日\n"
    got = heuristic_fill_fields(text)
    assert got.get("doc_date") == "2026-03-04"


def test_heuristic_vendor_supplier_code_label() -> None:
    text = "订单\n供应商编码：VENDOR-01\n"
    got = heuristic_vendor_code(text)
    assert got.get("vendor_code") == "VENDOR-01"


def test_document_parse_export_model_roundtrip() -> None:
    ing = IngestionResponse(
        ingestion_id="ing-1",
        file_id="file-1",
        file_hash="abc",
        user_id="u1",
        org_id="o1",
        extract_version="v0",
        model_version="mock",
        prompt_version="p1",
        status=IngestionStatus.NEED_USER_INPUT,
        doc_type_hint=DocType.PO,
        resolved_fields={"doc_date": "2026-01-02", "currency": "CNY"},
        missing_fields=["vendor_code"],
        parsed_char_count=100,
        parse_format_label="pdf_text",
        extract_preview="preview…",
        vendor_candidates=[{"code": "V1", "name": "Acme"}],
    )
    raw = build_document_parse_export(ing, include_full_text=False)
    m = DocumentParseExport.model_validate(raw)
    assert m.schema_version == "1.0"
    assert m.ingestion_id == "ing-1"
    assert m.extracted_fields["currency"] == "CNY"
    assert m.missing_required_fields == ["vendor_code"]
    assert m.parse["text_preview"] == "preview…"
    assert m.mapping_candidates["vendor"][0]["code"] == "V1"
    assert m.parse.get("full_text") is None
    assert m.line_items == []


def test_document_export_line_items_from_json() -> None:
    import json as _json

    ing = IngestionResponse(
        ingestion_id="ing-2",
        file_id="f",
        file_hash="h",
        user_id="u",
        org_id="o",
        extract_version="v0",
        model_version="m",
        prompt_version="p",
        status=IngestionStatus.PARSED,
        resolved_fields={"line_items_json": _json.dumps([{"inventory_code": "X1", "quantity": "2"}])},
        missing_fields=[],
    )
    m = DocumentParseExport.model_validate(build_document_parse_export(ing))
    assert m.line_items[0]["inventory_code"] == "X1"
    assert "line_items_json" not in m.extracted_fields

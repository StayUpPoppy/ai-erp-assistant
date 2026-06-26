from io import BytesIO
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document_extract import (
    classify_doc_type_from_name,
    classify_doc_type_from_text,
    extract_pdf_text_with_forced_chinese_ocr,
    extract_pdf_text_with_forced_ocr,
    extract_document_from_bytes,
    extract_text_from_bytes,
    heuristic_fill_fields,
    heuristic_vendor_code,
    mapping_search_snippet,
    object_key_to_display_name,
    resolved_upload_file_name,
    truncate_for_api,
)


def test_object_key_to_display_name():
    key = "uploads/org-demo/2026-05-06/deadbeef0000-invoice_acme.pdf"
    assert object_key_to_display_name(key) == "invoice_acme.pdf"


def test_resolved_upload_file_name_fallback():
    assert resolved_upload_file_name(None, "po_vendor.docx") == "po_vendor.docx"
    assert resolved_upload_file_name("", "  gr_note.pdf ") == "gr_note.pdf"
    key = "uploads/org-demo/x-hash-invoice.pdf"
    assert resolved_upload_file_name(key, "ignored.docx") == "hash-invoice.pdf"


def test_extract_plain_text():
    raw = "采购订单 PO-1\n日期 2026-05-01\nCNY".encode("utf-8")
    text, fmt = extract_text_from_bytes(raw, "order.txt")
    assert "2026-05-01" in text
    assert "utf-8" in fmt


def test_extract_csv_comma_joins_cells():
    raw = "vendor_code,doc_date,line_qty\nV001,2026-01-01,10\n".encode("utf-8")
    text, fmt = extract_text_from_bytes(raw, "export.csv")
    assert "V001" in text and "10" in text
    assert " | " in text
    assert "csv_rows" in fmt


def test_extract_csv_semicolon_sniffed():
    raw = "PO;Material;Qty\n45001;M010;5\n".encode("utf-8")
    text, fmt = extract_text_from_bytes(raw, "sap_export.csv")
    assert "45001" in text and "M010" in text and "5" in text
    assert "csv_rows" in fmt


def _minimal_xlsx_bytes() -> bytes:
    """最小 OOXML：共享字符串 + sheet（inlineStr / s 索引 / 无 t 的非数字 v）。"""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = f"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="{ns}" count="1" uniqueCount="1">
  <si><t>SharedPO-Ref</t></si>
</sst>""".encode("utf-8")
    sheet = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{ns}">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Inline-GR</t></is></c>
      <c r="B1" t="s"><v>0</v></c>
      <c r="C1"><v>PO-88888</v></c>
    </row>
  </sheetData>
</worksheet>""".encode("utf-8")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_extract_xlsx_shared_strings_inline_and_plain_v():
    raw = _minimal_xlsx_bytes()
    text, fmt = extract_text_from_bytes(raw, "export.xlsx")
    assert fmt == "xlsx_text"
    assert "SharedPO-Ref" in text
    assert "Inline-GR" in text
    assert "PO-88888" in text


def test_extract_docx_minimal():
    doc_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Purchase Order</w:t><w:t> PO-9900</w:t></w:r></w:p></w:body>
</w:document>"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
    text, fmt = extract_text_from_bytes(buf.getvalue(), "memo.docx")
    assert "PO-9900" in text
    assert fmt == "docx_text"


def test_classify_and_heuristic():
    assert classify_doc_type_from_name("my_invoice.pdf") == "INV"
    assert classify_doc_type_from_text("This is a Purchase Order for materials") == "PO"
    h = heuristic_fill_fields("Date: 2026-04-30 and pay in USD please")
    assert h.get("doc_date") == "2026-04-30"
    assert h.get("currency") == "USD"


def test_heuristic_fill_prefers_labeled_order_date():
    text = "交货日 2026-01-15\n订单日期：2026-05-20\n"
    h = heuristic_fill_fields(text)
    assert h.get("doc_date") == "2026-05-20"


def test_heuristic_fill_labeled_order_date_slash():
    text = "制单日期：2026/6/8\n其它 2026-01-01\n"
    h = heuristic_fill_fields(text)
    assert h.get("doc_date") == "2026-06-08"


def test_heuristic_fill_labeled_order_date_cn():
    text = "采购订单\n签订日期：2026年12月3日\n"
    h = heuristic_fill_fields(text)
    assert h.get("doc_date") == "2026-12-03"


def test_classify_doc_type_from_name_extended():
    assert classify_doc_type_from_name("acme-vat-inv-2026.pdf") == "INV"
    assert classify_doc_type_from_name("credit-note-CN001.pdf") == "INV"
    assert classify_doc_type_from_name("goods-receipt-plant1.pdf") == "GR"
    assert classify_doc_type_from_name("upload-gr_202605.pdf") == "GR"
    assert classify_doc_type_from_name("pur-ord-4500123.pdf") == "PO"
    assert classify_doc_type_from_name("sales-report-q1.pdf") is None


def test_classify_doc_type_from_text_extended():
    assert classify_doc_type_from_text("Commercial invoice INV-1 dated today") == "INV"
    assert classify_doc_type_from_text("Commercial Invoice\nPO Number: PO-2026-01\nInvoice Date 2026-05-01") == "INV"
    assert classify_doc_type_from_text("Proforma invoice for export clearance") == "INV"
    assert classify_doc_type_from_text("Stock receipt for movement type 101") == "GR"
    assert classify_doc_type_from_text("Goods Receipt\nPurchase Order 4500012345\nMaterial M001") == "GR"
    assert classify_doc_type_from_text("Warehouse receipt line 10") == "GR"
    assert (
        classify_doc_type_from_text(
            "Purchase Order\n"
            "Order No.: POGSVC2600205\n"
            "Buyer: Global-set Valve Components Jiangsu Co., LTD\n"
            "Supplier: Yingke\n"
            "Material Code Qty-UOM Delivery Date Unit Price Total Price\n"
            "Domestic supplier pls provide 13% VAT invoice."
        )
        == "PO"
    )
    assert classify_doc_type_from_text("Supplier PO 4500012345 confirmation") == "PO"
    assert classify_doc_type_from_text("Random packing list without keywords") is None


def test_heuristic_vendor_code():
    assert heuristic_vendor_code("Supplier V001 for project") == {"vendor_code": "V001"}
    assert heuristic_vendor_code("供应商编号: ACME-01") == {"vendor_code": "ACME-01"}


def test_mapping_search_snippet():
    assert mapping_search_snippet("  PO-100\nsecond line") == "PO-100"


def test_extract_png_routes_to_ocr(monkeypatch):
    """不依赖本机安装 Tesseract：mock OCR 引擎。"""
    pytest.importorskip("PIL")
    from PIL import Image

    img = Image.new("RGB", (120, 50), color=(255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()

    monkeypatch.setattr(
        "app.ocr_engine._ocr_tesseract",
        lambda raw_bytes, file_name: ("PO\n2026-06-01\nV010\nCNY\n", "ocr_tesseract_cli(lang=eng)"),
    )

    text, fmt = extract_text_from_bytes(raw, "scan.png")
    assert "V010" in text
    assert "ocr_tesseract" in fmt or "ocr_image" in fmt


def _blank_pdf(page_count: int = 1) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        for _ in range(page_count):
            doc.new_page(width=300, height=300)
        return doc.tobytes()
    finally:
        doc.close()


def test_pdf_scanned_page_uses_rapidocr_at_250_dpi(monkeypatch):
    from app.pdf_pipeline import TextBlock

    captured = {}

    def fake_ocr(_page, page_number, _file_name, dpi):
        captured["dpi"] = dpi
        return (
            "Purchase Order\nOrder No: PO-888\nM001 | 2 | 10.00",
            [TextBlock(page_number, "PO-888", (10, 10, 80, 30), "rapidocr", 0.95)],
            0.95,
            "ocr_rapid(ch_en)",
        )

    monkeypatch.setattr("app.pdf_pipeline._ocr_page", fake_ocr)
    result = extract_document_from_bytes(_blank_pdf(), "scan.pdf")

    assert captured["dpi"] == 250
    assert result.route == "rapidocr"
    assert result.format_label == "pdf_rapidocr_250dpi"
    assert result.pages[0].ocr_confidence == 0.95
    assert result.pages[0].blocks[0].bbox == (10, 10, 80, 30)
    assert "PO-888" in result.text


def test_pdf_native_page_skips_ocr(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        page = doc.new_page(width=500, height=500)
        page.insert_text((50, 80), "Purchase Order PO-9999 VendorLine CNY 2026-05-08")
        raw = doc.tobytes()
    finally:
        doc.close()

    monkeypatch.setattr("app.pdf_pipeline._ocr_page", lambda *_args, **_kwargs: pytest.fail("OCR must not run"))
    result = extract_document_from_bytes(raw, "native.pdf")

    assert result.route == "native"
    assert result.format_label == "pdf_native_pymupdf"
    assert "PO-9999" in result.text


def test_pdf_mixed_pages_keep_page_order(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        page = doc.new_page(width=500, height=500)
        page.insert_text((50, 80), "Purchase Order PO-NATIVE VendorLine CNY 2026-05-08")
        doc.new_page(width=500, height=500)
        raw = doc.tobytes()
    finally:
        doc.close()

    def fake_ocr(_page, page_number, _file_name, _dpi):
        return (f"scan page {page_number} M002 | 3 | 20", [], 0.91, "ocr_rapid(ch_en)")

    monkeypatch.setattr("app.pdf_pipeline._ocr_page", fake_ocr)
    result = extract_document_from_bytes(raw, "mixed.pdf")

    assert result.route == "hybrid"
    assert [page.source for page in result.pages] == ["native", "rapidocr"]
    assert result.text.index("[PAGE 1]") < result.text.index("[PAGE 2]")


def test_pdf_low_confidence_keeps_local_result(monkeypatch):
    monkeypatch.setattr(
        "app.pdf_pipeline._ocr_page",
        lambda *_args: ("Purchase Order PO-LOCAL", [], 0.50, "ocr_rapid(ch_en)"),
    )

    result = extract_document_from_bytes(_blank_pdf(), "low.pdf")

    assert result.route == "rapidocr"
    assert "PO-LOCAL" in result.text
    assert result.fallback_reason


def test_pdf_quality_hint_keeps_local_result_without_external_warning(monkeypatch):
    monkeypatch.setattr(
        "app.pdf_pipeline._ocr_page",
        lambda *_args: ("Purchase Order PO-LOCAL", [], 0.50, "ocr_rapid(ch_en)"),
    )
    result = extract_document_from_bytes(_blank_pdf(), "fallback.pdf")

    assert "PO-LOCAL" in result.text
    assert result.fallback_reason
    assert result.warnings == []


def test_pdf_table_extraction_keeps_coordinates():
    from app.pdf_pipeline import _extract_tables

    class FakeTable:
        bbox = (20, 100, 280, 220)

        @staticmethod
        def extract():
            return [["Material", "Qty", "Price"], ["M001", "2", "10"]]

    class FakePage:
        @staticmethod
        def find_tables():
            return type("Finder", (), {"tables": [FakeTable()]})()

    tables = _extract_tables(FakePage(), 1)

    assert tables[0].bbox == (20.0, 100.0, 280.0, 220.0)
    assert tables[0].rows[1] == ("M001", "2", "10")


def test_pdf_forced_ocr_runs_even_when_text_layer_is_dense(monkeypatch):
    raw = b"%PDF-1.4 dense text layer"
    dense = "purchase order " * 20
    called = {"max_pages": None}

    monkeypatch.setattr("app.document_extract._extract_pdf_text_layer", lambda raw_bytes, fn="": (dense, "pypdf"))

    def _forced_ocr(raw_bytes, fn="", max_pages_override=None):
        called["max_pages"] = max_pages_override
        return "forced ocr POGSVC2600205", 3

    monkeypatch.setattr("app.document_extract._ocr_pdf_pages_supplement", _forced_ocr)

    text, fmt = extract_pdf_text_with_forced_ocr(raw, "dense.pdf", max_pages=3)

    assert "purchase order" in text
    assert "POGSVC2600205" in text
    assert fmt == "pdf_text+forced_ocr_pages_3"
    assert called["max_pages"] == 3


def test_pdf_forced_chinese_ocr_uses_chinese_engine_overrides(monkeypatch):
    raw = b"%PDF-1.4 dense text layer"
    dense = "Purchase Order\n"
    captured = {}

    monkeypatch.setattr("app.document_extract._extract_pdf_text_layer", lambda raw_bytes, fn="": (dense, "pypdf"))

    def _forced_ocr(raw_bytes, fn="", max_pages_override=None, ocr_kwargs=None):
        captured["max_pages"] = max_pages_override
        captured["ocr_kwargs"] = ocr_kwargs
        return "格鲁赛特阀门配件江苏有限公司\n江苏省丹阳市埤城镇122省道尧巷段（212300）", 2

    monkeypatch.setattr("app.document_extract._ocr_pdf_pages_supplement", _forced_ocr)

    text, fmt = extract_pdf_text_with_forced_chinese_ocr(raw, "dense.pdf", max_pages=2)

    assert "格鲁赛特阀门配件江苏有限公司" in text
    assert fmt == "pdf_text+ocr_paddle_ch_pages_2"
    assert captured["max_pages"] == 2
    assert captured["ocr_kwargs"]["engine_override"] == "paddle"
    assert captured["ocr_kwargs"]["paddle_lang_override"] == "ch"
    assert captured["ocr_kwargs"]["tesseract_lang_override"] == "chi_sim+eng"


def test_truncate_for_api():
    long = "x" * 3000
    assert len(truncate_for_api(long, max_chars=100)) == 100
    assert truncate_for_api("short") == "short"

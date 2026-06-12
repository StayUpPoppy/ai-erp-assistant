from io import BytesIO
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document_extract import (
    classify_doc_type_from_name,
    classify_doc_type_from_text,
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


def test_pdf_sparse_merges_first_page_ocr(monkeypatch):
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter

    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    raw = buf.getvalue()

    monkeypatch.setattr(
        "app.document_extract._ocr_pdf_pages_supplement",
        lambda raw_bytes, fn="": ("mocked ocr V888", 2),
    )

    text, fmt = extract_text_from_bytes(raw, "scan.pdf")
    assert "V888" in text
    assert fmt == "pdf_text+ocr_pages_2"


def test_pdf_sparse_uses_mineru_before_page_ocr(monkeypatch):
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter

    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    raw = buf.getvalue()
    called = {"page_ocr": False}

    monkeypatch.setenv("MINERU_ENABLED", "true")
    monkeypatch.setattr(
        "app.document_extract._mineru_pdf_supplement",
        lambda raw_bytes, fn="": ("mineru markdown M999", "mineru_markdown"),
    )

    def _page_ocr(raw_bytes, fn=""):
        called["page_ocr"] = True
        return "should not run", 1

    monkeypatch.setattr("app.document_extract._ocr_pdf_pages_supplement", _page_ocr)

    text, fmt = extract_text_from_bytes(raw, "scan.pdf")
    assert "M999" in text
    assert fmt == "pdf_text+mineru_markdown"
    assert called["page_ocr"] is False


def test_pdf_sparse_falls_back_to_page_ocr_when_mineru_empty(monkeypatch):
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter

    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    raw = buf.getvalue()

    monkeypatch.setenv("MINERU_ENABLED", "true")
    monkeypatch.setattr("app.document_extract._mineru_pdf_supplement", lambda raw_bytes, fn="": ("", "mineru_error"))
    monkeypatch.setattr("app.document_extract._ocr_pdf_pages_supplement", lambda raw_bytes, fn="": ("paddle text", 1))

    text, fmt = extract_text_from_bytes(raw, "scan.pdf")
    assert "paddle text" in text
    assert fmt == "pdf_text+ocr_first_page"


def test_pdf_sparse_single_page_ocr_label(monkeypatch):
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter

    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    raw = buf.getvalue()
    monkeypatch.setattr(
        "app.document_extract._ocr_pdf_pages_supplement",
        lambda raw_bytes, fn="": ("only", 1),
    )
    _, fmt = extract_text_from_bytes(raw, "one.pdf")
    assert fmt == "pdf_text+ocr_first_page"


def test_pdf_dense_text_layer_skips_ocr(monkeypatch):
    raw = b"%PDF-1.4 dense text layer"
    dense = "purchase order " * 20
    called = {"ocr": False}

    monkeypatch.setattr("app.document_extract._extract_pdf_text_layer", lambda raw_bytes, fn="": (dense, "pypdf"))

    def _fail_if_ocr(raw_bytes, fn=""):
        called["ocr"] = True
        return "should not run", 1

    monkeypatch.setattr("app.document_extract._ocr_pdf_pages_supplement", _fail_if_ocr)

    text, fmt = extract_text_from_bytes(raw, "dense.pdf")
    assert text == dense
    assert fmt == "pdf_text"
    assert called["ocr"] is False


def test_truncate_for_api():
    long = "x" * 3000
    assert len(truncate_for_api(long, max_chars=100)) == 100
    assert truncate_for_api("short") == "short"

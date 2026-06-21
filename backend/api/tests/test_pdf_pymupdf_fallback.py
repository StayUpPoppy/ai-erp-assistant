"""pypdf 不可用时 PDF 文字层回退 PyMuPDF。"""

from __future__ import annotations

import builtins
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document_extract import extract_text_from_bytes


def test_pdf_extract_uses_pymupdf_when_pypdf_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="PyMuPDF")
    real_import = builtins.__import__

    def fake_import(name: str, globals_=None, locals_=None, fromlist=(), level=0):
        if name == "pypdf" or (level == 0 and name.startswith("pypdf")):
            raise ImportError("simulated missing pypdf")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    import fitz

    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "PO-9999 VendorLine CNY 2026-05-08")
        raw = doc.tobytes()
    finally:
        doc.close()

    text, fmt = extract_text_from_bytes(raw, "订单.pdf")
    assert "PO-9999" in text
    assert fmt == "pdf_native_pymupdf"

import io
import json
from io import BytesIO
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_ocr_http_parses_default_text_field(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("OCR_ENGINE", "http")
    monkeypatch.setenv("OCR_HTTP_URL", "http://mock-ocr.local/recognize")
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "false")

    img = Image.new("RGB", (40, 20), color=(240, 240, 240))
    buf = BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()

    payload = {"text": "发票号 INV-9001\n"}

    def fake_urlopen(req, timeout=0, context=None):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    with patch("app.ocr_engine.request.urlopen", fake_urlopen):
        from app.ocr_engine import ocr_image_bytes

        text, fmt = ocr_image_bytes(raw, "x.png")
    assert "INV-9001" in text
    assert "ocr_http" in fmt


def test_ocr_http_nested_text_field(monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("OCR_ENGINE", "http")
    monkeypatch.setenv("OCR_HTTP_URL", "http://mock.local/x")
    monkeypatch.setenv("OCR_HTTP_RESPONSE_TEXT_FIELD", "data.text")
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "false")

    img = Image.new("RGB", (20, 20), color=(1, 2, 3))
    buf = BytesIO()
    img.save(buf, format="PNG")

    def fake_urlopen(req, timeout=0, context=None):
        return io.BytesIO(json.dumps({"data": {"text": "nested ok"}}).encode())

    with patch("app.ocr_engine.request.urlopen", fake_urlopen):
        from app.ocr_engine import ocr_image_bytes

        text, fmt = ocr_image_bytes(buf.getvalue(), "a.png")
    assert "nested ok" in text


def test_ocr_engine_http_without_url_falls_back_tesseract(monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("OCR_ENGINE", "http")
    monkeypatch.delenv("OCR_HTTP_URL", raising=False)
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "true")

    img = Image.new("RGB", (30, 20), color=(255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")

    monkeypatch.setattr(
        "app.ocr_engine._ocr_tesseract",
        lambda raw, fn: ("fb-text", "ocr_tesseract(lang=eng)"),
    )

    from app.ocr_engine import ocr_image_bytes

    text, fmt = ocr_image_bytes(buf.getvalue(), "z.png")
    assert "fb-text" in text
    assert "fallback" in fmt


def test_ocr_engine_paddle_selected_without_tesseract_fallback(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "paddle")
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "false")

    called = {"tesseract": False}

    monkeypatch.setattr(
        "app.ocr_engine._ocr_paddle",
        lambda raw, fn: ("paddle text", "ocr_paddle(lang=ch)"),
    )

    def _unexpected_tesseract(raw, fn):
        called["tesseract"] = True
        return "tesseract text", "ocr_tesseract_cli(lang=eng)"

    monkeypatch.setattr("app.ocr_engine._ocr_tesseract", _unexpected_tesseract)

    from app.ocr_engine import ocr_image_bytes

    text, fmt = ocr_image_bytes(b"fake-image", "scan.png")
    assert text == "paddle text"
    assert fmt == "ocr_paddle(lang=ch)"
    assert called["tesseract"] is False

import io
import json
from io import BytesIO
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_parse_recognize_response_extracts_content() -> None:
    from app import aliyun_ocr

    inner = json.dumps({"content": "  发票 A001  "})
    outer = json.dumps({"Data": inner})
    text, fmt = aliyun_ocr._parse_recognize_response(outer)
    assert "A001" in text
    assert fmt == "aliyun_recognize_general"


def test_parse_recognize_response_api_error() -> None:
    from app import aliyun_ocr

    body = json.dumps({"Code": "InvalidAccessKeyId.NotFound", "Message": "bad"})
    text, fmt = aliyun_ocr._parse_recognize_response(body)
    assert text == ""
    assert "aliyun_api_" in fmt


def test_aliyun_credentials_configured(monkeypatch) -> None:
    from app import aliyun_ocr

    monkeypatch.delenv("ALIYUN_OCR_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIYUN_OCR_ACCESS_KEY_SECRET", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", raising=False)
    assert aliyun_ocr.aliyun_credentials_configured() is False

    monkeypatch.setenv("ALIYUN_OCR_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("ALIYUN_OCR_ACCESS_KEY_SECRET", "y")
    assert aliyun_ocr.aliyun_credentials_configured() is True


def test_ocr_engine_aliyun_mock_success(monkeypatch) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("OCR_ENGINE", "aliyun")
    monkeypatch.setenv("ALIYUN_OCR_ACCESS_KEY_ID", "test-ak")
    monkeypatch.setenv("ALIYUN_OCR_ACCESS_KEY_SECRET", "test-sk")
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "false")

    img = Image.new("RGB", (20, 20), color=(10, 20, 30))
    buf = BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()

    inner = json.dumps({"content": "阿里云行"})
    outer = json.dumps({"Data": inner})

    def fake_urlopen(req, timeout=0, context=None):
        return io.BytesIO(outer.encode("utf-8"))

    with patch("app.aliyun_ocr.request.urlopen", fake_urlopen):
        from app.ocr_engine import ocr_image_bytes

        text, fmt = ocr_image_bytes(raw, "doc.png")

    assert "阿里云" in text
    assert "aliyun" in fmt or "recognize" in fmt


def test_ocr_engine_aliyun_no_creds_falls_back_tesseract(monkeypatch) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setenv("OCR_ENGINE", "aliyun")
    monkeypatch.delenv("ALIYUN_OCR_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIYUN_OCR_ACCESS_KEY_SECRET", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", raising=False)
    monkeypatch.setenv("OCR_ENGINE_AUTO_FALLBACK", "true")

    img = Image.new("RGB", (24, 16), color=(200, 200, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")

    monkeypatch.setattr(
        "app.ocr_engine._ocr_tesseract",
        lambda raw, fn: ("fb-aliyun", "ocr_tesseract(lang=eng)"),
    )

    from app.ocr_engine import ocr_image_bytes

    text, fmt = ocr_image_bytes(buf.getvalue(), "z.png")
    assert "fb-aliyun" in text
    assert "fallback" in fmt

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mineru_client import MineruClientError, parse_pdf_bytes_with_mineru


def test_mineru_v4_signed_upload_and_poll(monkeypatch) -> None:
    import app.mineru_client as client

    calls = []

    def fake_json(method, url, payload=None, timeout=30.0, headers=None):
        calls.append((method, url, payload, headers))
        if url.endswith("/file-urls/batch"):
            return {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.example/file"]}}
        return {
            "code": 0,
            "data": {
                "batch_id": "batch-1",
                "extract_result": [{"file_name": "order.pdf", "state": "done", "full_zip_url": "https://cdn.example/result.zip"}],
            },
        }

    uploaded = {}
    monkeypatch.setenv("MINERU_ENABLED", "true")
    monkeypatch.setenv("MINERU_MODE", "precise_v4")
    monkeypatch.setenv("MINERU_API_TOKEN", "secret-token")
    monkeypatch.setenv("MINERU_PRECISE_API_BASE", "https://mineru.net/api/v4")
    monkeypatch.setenv("MINERU_MODEL_VERSION", "vlm")
    monkeypatch.setattr(client, "_json_request", fake_json)
    monkeypatch.setattr(client, "_put_file", lambda url, raw, timeout: uploaded.update(url=url, raw=raw))
    monkeypatch.setattr(client, "_download_archive_markdown", lambda *_args, **_kwargs: "# Purchase Order\n|M001|2|10|")

    text, fmt = parse_pdf_bytes_with_mineru(b"pdf", "order.pdf")

    assert text.startswith("# Purchase Order")
    assert fmt == "mineru_v4_vlm_markdown"
    assert uploaded == {"url": "https://upload.example/file", "raw": b"pdf"}
    assert calls[0][0:2] == ("POST", "https://mineru.net/api/v4/file-urls/batch")
    assert calls[1][0:2] == ("GET", "https://mineru.net/api/v4/extract-results/batch/batch-1")
    assert calls[0][3]["Authorization"] == "Bearer secret-token"
    assert calls[0][2]["files"][0]["is_ocr"] is True
    assert calls[0][2]["enable_table"] is True
    assert calls[0][2]["enable_formula"] is False


def test_mineru_v4_requires_token(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_ENABLED", "true")
    monkeypatch.setenv("MINERU_MODE", "precise_v4")
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)

    with pytest.raises(MineruClientError, match="token"):
        parse_pdf_bytes_with_mineru(b"pdf", "order.pdf")


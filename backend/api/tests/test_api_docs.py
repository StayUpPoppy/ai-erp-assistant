from __future__ import annotations

from pathlib import Path
import logging
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.logging_config

app.logging_config.setup_logging = lambda: logging.getLogger("ai_erp_api_test")

from main import app, redoc_docs, swagger_docs  # noqa: E402


def test_swagger_uses_proxy_safe_relative_openapi_url() -> None:
    html = swagger_docs().body.decode("utf-8")

    assert "AI ERP Assistant API - Swagger UI" in html
    assert "url: './openapi.json'" in html
    assert "url: '/openapi.json'" not in html


def test_redoc_uses_proxy_safe_relative_openapi_url() -> None:
    html = redoc_docs().body.decode("utf-8")

    assert "AI ERP Assistant API - ReDoc" in html
    assert 'spec-url="./openapi.json"' in html


def test_openapi_keeps_source_file_contract() -> None:
    schema = app.openapi()

    assert schema["servers"] == [{"url": ".", "description": "Current API base"}]
    path = schema["paths"]["/integrations/erp/ingestions/{ingestion_id}/source-file"]
    assert path["get"]["security"] == [{"SourceFileBearer": []}]
    assert schema["components"]["securitySchemes"]["SourceFileBearer"]["scheme"] == "bearer"

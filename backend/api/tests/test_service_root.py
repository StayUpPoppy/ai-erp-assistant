"""根路径与轻量集成发现。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import service_index


def test_get_root_service_index():
    data = service_index()
    assert data.get("service") == "ai-erp-assistant-api"
    assert data["links"]["health"] == "/health"
    assert data["links"]["ingestion_create_draft"]["path"] == "/ingestions/{ingestion_id}/create-draft"

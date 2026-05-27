from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import health


def test_health_reports_llm_router_configuration(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LLM_ROUTER_ENABLED", "true")
    monkeypatch.setenv("LLM_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "model-for-router-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/")
    monkeypatch.setenv("LLM_PROMPT_VERSION", "prompt-test")

    h = health()

    assert h.llm_router_enabled is True
    assert h.llm_extract_enabled is False
    assert h.llm_api_key_configured is True
    assert h.llm_model == "model-for-router-test"
    assert h.llm_base_url == "https://llm.example.test"
    assert h.llm_prompt_version == "prompt-test"

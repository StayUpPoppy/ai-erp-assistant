from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import MockErpClient
from app.erp_qa import answer_with_erp_tools
from app.erp_qa_reports import (
    erp_qa_reports_health_payload,
    invalidate_report_definitions_cache,
    load_report_definitions,
)


def test_load_definitions_empty_without_file(monkeypatch):
    monkeypatch.delenv("ERP_QA_REPORTS_PATH", raising=False)
    import app.erp_qa_reports as rq

    monkeypatch.setattr(rq, "reports_config_file", lambda: None)
    rq.invalidate_report_definitions_cache()
    assert rq.load_report_definitions() == []
    assert rq.erp_qa_reports_health_payload()["erp_qa_report_definitions_count"] == 0


def test_ambiguous_reports_asks_clarification(tmp_path, monkeypatch):
    p = tmp_path / "rep.json"
    p.write_text(
        """
{
  "version": 1,
  "reports": [
    {
      "id": "a",
      "label": "报表甲",
      "match": { "any_substrings": ["库存"] },
      "http": { "method": "GET", "path": "/api/a/page", "query": { "org": "{{org_id}}" } },
      "response": { "datynk_envelope": true, "records_path": "data.records" }
    },
    {
      "id": "b",
      "label": "报表乙",
      "match": { "any_substrings": ["库存"] },
      "http": { "method": "GET", "path": "/api/b/page", "query": { "org": "{{org_id}}" } },
      "response": { "datynk_envelope": true, "records_path": "data.records" }
    }
  ]
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ERP_QA_REPORTS_PATH", str(p))
    invalidate_report_definitions_cache()
    ans, tools, raw = answer_with_erp_tools("org-x", "查一下库存情况", MockErpClient())
    assert "报表甲" in ans and "报表乙" in ans
    assert any("ambiguous" in t for t in tools)
    assert raw == {}


def test_single_report_invokes_fetch_config_read(tmp_path, monkeypatch):
    p = tmp_path / "rep.json"
    p.write_text(
        """
{
  "version": 1,
  "reports": [
    {
      "id": "only_one",
      "label": "唯一库存表",
      "match": { "any_substrings": ["即时库存"], "all_substrings": ["M1"] },
      "http": {
        "method": "GET",
        "path": "/api/stock/page",
        "query": { "org": "{{org_id}}", "materialCode": "{{keyword}}" }
      },
      "response": { "datynk_envelope": true, "records_path": "data.records" }
    }
  ]
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ERP_QA_REPORTS_PATH", str(p))
    invalidate_report_definitions_cache()
    ans, tools, raw = answer_with_erp_tools("英科1厂", "即时库存 M1 多少", MockErpClient())
    assert any("fetch_config_read" in t for t in tools)
    assert "唯一库存表" in ans
    assert raw.get("_report_blocks")
    assert erp_qa_reports_health_payload()["erp_qa_report_definitions_count"] == 1

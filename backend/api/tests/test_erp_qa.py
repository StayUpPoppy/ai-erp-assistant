from pathlib import Path
import sys

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import ErpClientError, MockErpClient
from app.erp_qa import answer_with_erp_tools
from app.routes import chat_erp_qa_route
from app.schemas import ChatErpQaRequest


def test_answer_calls_vendor_search():
    answer, tools, raw = answer_with_erp_tools("org-1", "查一下供应商 vendor", MockErpClient())
    assert any("search_vendors" in t for t in tools)
    assert "V001" in answer or "Mock Vendor" in answer
    assert "org-1" in answer
    assert raw.get("vendors")


def test_answer_calls_material_search():
    answer, tools, raw = answer_with_erp_tools("org-1", "物料 material 有哪些", MockErpClient())
    assert any("search_materials" in t for t in tools)
    assert "M001" in answer or "Mock Material" in answer


def test_fallback_triggers_sale_orders_when_no_intent(monkeypatch):
    monkeypatch.delenv("ERP_QA_FALLBACK_WHEN_NO_INTENT", raising=False)
    monkeypatch.setenv("ERP_QA_BROAD_MASTER_SEARCH", "false")
    answer, tools, raw = answer_with_erp_tools("org-1", "深圳科技有限公司", MockErpClient())
    assert any("fallback_no_intent" in t for t in tools)
    assert raw.get("erp_qa_fallback") == "no_intent_sale_orders"
    assert raw.get("sale_orders")
    assert "销售订单" in answer


def test_fallback_respects_env_off(monkeypatch):
    monkeypatch.setenv("ERP_QA_FALLBACK_WHEN_NO_INTENT", "false")
    monkeypatch.setenv("ERP_QA_BROAD_MASTER_SEARCH", "false")
    answer, tools, raw = answer_with_erp_tools("org-1", "深圳科技有限公司", MockErpClient())
    assert any("no_tool" in t for t in tools)
    assert "供应商" in answer


def test_fallback_skipped_for_inventory_question(monkeypatch):
    monkeypatch.delenv("ERP_QA_FALLBACK_WHEN_NO_INTENT", raising=False)
    answer, tools, raw = answer_with_erp_tools("org-1", "库存还剩多少", MockErpClient())
    assert any("no_tool" in t for t in tools)
    assert raw.get("sale_orders") is None


def test_broad_master_search_runs_parallel_tools(monkeypatch):
    monkeypatch.delenv("ERP_QA_BROAD_MASTER_SEARCH", raising=False)
    answer, tools, raw = answer_with_erp_tools("org-1", "深圳科技有限公司", MockErpClient())
    assert any("search_vendors" in t for t in tools)
    assert any("search_materials" in t for t in tools)
    assert any("search_warehouses" in t for t in tools)
    assert any("search_tax_codes" in t for t in tools)
    assert any("search_customers" in t for t in tools)
    assert raw.get("vendors") is not None
    assert "供应商" in answer


def test_answer_calls_customer_search():
    answer, tools, raw = answer_with_erp_tools("org-1", "查客户 深圳科技", MockErpClient())
    assert any("search_customers" in t for t in tools)
    assert "MOCK-CUST" in answer or "Mock 往来客户" in answer
    assert raw.get("customers")


def test_answer_calls_sale_order_search():
    answer, tools, raw = answer_with_erp_tools("org-1", "查销售订单 测试客户", MockErpClient())
    assert any("search_sale_orders" in t for t in tools)
    assert "MOCK-SO-001" in answer or "Mock 客户" in answer
    assert raw.get("sale_orders")


def test_sale_order_intent_strips_type_prefix():
    joined = " ".join(
        answer_with_erp_tools("org-1", "销售订单深圳科技有限公司", MockErpClient())[1],
    )
    assert "search_sale_orders" in joined
    assert "深圳科技" in joined
    assert "customerName='销售订单深圳" not in joined


def test_month_order_phrase_triggers_sale_orders_not_broad(monkeypatch):
    monkeypatch.setenv("ERP_QA_BROAD_MASTER_SEARCH", "true")
    monkeypatch.delenv("ERP_QA_FALLBACK_WHEN_NO_INTENT", raising=False)
    tools = answer_with_erp_tools("org-1", "查询本月订单", MockErpClient())[1]
    joined = " ".join(tools)
    assert "search_sale_orders" in joined
    assert "search_vendors" not in joined


def test_sale_order_month_intent_filters_rows(monkeypatch):
    from datetime import date

    import app.erp_qa as erp_qa_mod

    monkeypatch.setattr(erp_qa_mod, "_today_erp_qa", lambda: date(2026, 5, 15))

    class _SOClient(MockErpClient):
        def search_sale_orders(
            self,
            org_id,
            keyword="",
            page_num=1,
            page_size=20,
            *,
            order_date_begin="",
            order_date_end="",
        ):
            _ = org_id, keyword, page_num, page_size, order_date_begin, order_date_end
            return [
                {"orderNo": "F01SO26051201", "customerName": "C1", "orderDate": "2026-05-12 08:00:00"},
                {"orderNo": "F01SO26042701", "customerName": "C2", "orderDate": "2026-04-27 08:00:00"},
            ]

    answer, tools, raw = answer_with_erp_tools("org-1", "查询本月销售订单", _SOClient())
    assert any("search_sale_orders" in t for t in tools)
    assert "F01SO26051201" in answer
    assert "F01SO26042701" not in answer
    assert raw.get("sale_orders_date_filter", {}).get("rows_after_filter") == 1


def test_master_empty_troubleshoot_shown_in_real_mode(monkeypatch):
    import app.erp_qa as erp_qa_mod
    from app.erp_client import MockErpClient

    class EmptyTax(MockErpClient):
        def search_tax_codes(self, org_id, keyword):
            return []

    monkeypatch.setattr(
        erp_qa_mod,
        "erp_adapter_health_payload",
        lambda: {
            "erp_client_mode": "real",
            "erp_data_base_netloc": "erp.example.com",
            "erp_soft_fail_master_search": True,
        },
    )
    ans, _, _ = erp_qa_mod.answer_with_erp_tools("o1", "查税码 J1", EmptyTax())
    assert "主数据无结果" in ans


def test_pick_keyword_strips_leading_cha(monkeypatch):
    monkeypatch.setenv("ERP_QA_BROAD_MASTER_SEARCH", "false")
    joined = " ".join(answer_with_erp_tools("org-1", "查深圳科技", MockErpClient())[1])
    assert "fallback_no_intent" in joined
    assert "深圳科技" in joined


def test_answer_customer_write_help():
    answer, tools, raw = answer_with_erp_tools("org-1", "如何新建客户并保存到 ERP？", MockErpClient())
    assert any("customer_save_api_docs" in t for t in tools)
    assert "/integrations/erp/customer" in answer
    assert "ERP_CUSTOMER_SAVE_ENABLED" in answer
    assert "GET /" in answer
    assert raw == {}


def test_chat_erp_qa_route_without_testclient():
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/erp-qa",
        "raw_path": b"/chat/erp-qa",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-chat-test"
    res = chat_erp_qa_route(
        ChatErpQaRequest(message="查供应商 vendor", org_id="org-demo", user_id="u-1"),
        request,
    )
    assert res.erp_tools_used
    assert "V001" in res.answer or "Mock Vendor" in res.answer


def test_chat_erp_qa_route_upstream_error_returns_message(monkeypatch):
    import app.routes as routes_module

    def boom(*_a, **_k):
        raise ErpClientError("ERP_UPSTREAM_ERROR", "unauthorized", 401, {})

    monkeypatch.setattr(routes_module, "answer_with_erp_tools", boom)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/erp-qa",
        "raw_path": b"/chat/erp-qa",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-chat-err"
    res = chat_erp_qa_route(
        ChatErpQaRequest(message="销售订单 x", org_id="org-demo", user_id="u-1"),
        request,
    )
    assert "401" in res.answer
    assert "ERP_API_TOKEN" in res.answer or "cookie_session" in res.answer
    assert any("upstream_error" in t for t in res.erp_tools_used)

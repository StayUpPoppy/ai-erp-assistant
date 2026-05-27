import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.erp_client import ErpClientError
from app.routes import save_customer_route
from app.schemas import ErrorCode, SaveCustomerRequest


def _req() -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/integrations/erp/customer",
        "raw_path": b"/integrations/erp/customer",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.request_id = "req-test"
    return request


def test_save_customer_route_success(monkeypatch):
    def _fake(payload):
        assert payload["org"] == "org-x"
        assert payload["name"] == "N1"
        return ("CN-99", "https://erp/c/CN-99")

    monkeypatch.setattr("app.routes.erp_client.save_customer", _fake)
    out = save_customer_route(
        SaveCustomerRequest(org_id="org-x", fields={"name": "N1"}),
        _req(),
    )
    assert out.customer_no == "CN-99"
    assert out.customer_url == "https://erp/c/CN-99"


def test_save_customer_route_keeps_explicit_org_in_fields(monkeypatch):
    captured = {}

    def _fake(payload):
        captured["payload"] = dict(payload)
        return ("1", "")

    monkeypatch.setattr("app.routes.erp_client.save_customer", _fake)
    save_customer_route(
        SaveCustomerRequest(org_id="org-default", fields={"org": "explicit-org", "name": "a"}),
        _req(),
    )
    assert captured["payload"]["org"] == "explicit-org"


def test_save_customer_route_disabled(monkeypatch):
    def _raise(_payload):
        raise ErpClientError(
            code="ERP_CUSTOMER_SAVE_DISABLED",
            message="disabled",
            status_code=0,
        )

    monkeypatch.setattr("app.routes.erp_client.save_customer", _raise)
    with pytest.raises(HTTPException) as excinfo:
        save_customer_route(SaveCustomerRequest(org_id="o", fields={"name": "x"}), _req())
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == ErrorCode.ERP_CUSTOMER_SAVE_DISABLED.value


def test_save_customer_route_upstream_timeout(monkeypatch):
    def _raise(_payload):
        raise ErpClientError(code="ERP_UPSTREAM_TIMEOUT", message="t", status_code=0)

    monkeypatch.setattr("app.routes.erp_client.save_customer", _raise)
    with pytest.raises(HTTPException) as excinfo:
        save_customer_route(SaveCustomerRequest(org_id="o", fields={"name": "x"}), _req())
    assert excinfo.value.status_code == 504
    assert excinfo.value.detail == ErrorCode.ERP_UPSTREAM_TIMEOUT.value

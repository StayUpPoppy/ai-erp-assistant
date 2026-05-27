import importlib
import io
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_erp_client_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("ERP_CLIENT_MODE", raising=False)
    monkeypatch.delenv("ERP_BASE_URL", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    assert isinstance(erp_client_module.erp_client, erp_client_module.MockErpClient)


def test_mock_erp_validate_draft_po_requires_material_and_qty(monkeypatch):
    monkeypatch.delenv("ERP_CLIENT_MODE", raising=False)
    monkeypatch.delenv("ERP_BASE_URL", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.MockErpClient()
    ok, miss = client.validate_draft(
        "PO",
        {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": "CNY"},
    )
    assert ok is False
    assert "material_code" in miss
    assert "line_qty" in miss
    ok2, miss2 = client.validate_draft(
        "PO",
        {
            "vendor_code": "V001",
            "doc_date": "2026-04-30",
            "currency": "CNY",
            "material_code": "M001",
            "line_qty": "10",
        },
    )
    assert ok2 is True
    assert miss2 == []


def test_erp_client_real_mode_without_base_url_falls_back(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.delenv("ERP_BASE_URL", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    assert isinstance(erp_client_module.erp_client, erp_client_module.MockErpClient)


def test_erp_client_real_mode_selected(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_TIMEOUT_SECONDS", "12")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    assert isinstance(erp_client_module.erp_client, erp_client_module.RealErpClient)
    assert erp_client_module.erp_client.base_url == "https://erp.example.com"
    assert erp_client_module.erp_client.timeout_seconds == 12


def test_real_erp_skip_upstream_validate_only_local_keys(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_SKIP_UPSTREAM_VALIDATE", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    calls: list[str] = []

    def _no_http(*args, **kwargs):
        calls.append("should_not_run")
        raise AssertionError("_request_json should not be called when skip_upstream_validate")

    monkeypatch.setattr(client, "_request_json", _no_http)
    ok, miss = client.validate_draft(
        "PO",
        {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": "CNY"},
        required_keys=["vendor_code", "currency"],
    )
    assert ok is True
    assert miss == []
    assert calls == []

    ok2, miss2 = client.validate_draft(
        "PO",
        {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": ""},
        required_keys=["vendor_code", "currency"],
    )
    assert ok2 is False
    assert "currency" in miss2


def test_real_erp_create_draft_custom_path_and_empty_url_allowed(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_DRAFT_PATH", "/api/v1/erp/orders")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_DRAFT_URL", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    captured: dict[str, object] = {}

    def _fake_request_json(method, path, payload=None):
        captured["path"] = path
        if path == "/api/v1/erp/orders":
            return {"draft_no": "ORD-001"}
        return {}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    draft_no, draft_url = client.create_draft("PO", {"vendor_code": "V1"}, "idem-1")
    assert draft_no == "ORD-001"
    assert draft_url == ""
    assert captured["path"] == "/api/v1/erp/orders"


def test_real_erp_client_supports_configurable_fields(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_ITEMS_FIELD", "data")
    monkeypatch.setenv("ERP_VALIDATE_VALID_FIELD", "ok")
    monkeypatch.setenv("ERP_VALIDATE_MISSING_FIELDS_FIELD", "missing")
    monkeypatch.setenv("ERP_DRAFT_NO_FIELD", "draftNumber")
    monkeypatch.setenv("ERP_DRAFT_URL_FIELD", "draftLink")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    def _fake_request_json(method, path, payload=None):
        if path.startswith("/erp/vendors/search"):
            return {"data": [{"vendor_code": "V100", "vendor_name": "Vendor 100"}]}
        if path == "/erp/drafts/validate":
            return {"ok": False, "missing": ["vendor_code"]}
        if path == "/erp/drafts":
            return {"draftNumber": "PO-DRAFT-0099", "draftLink": "https://erp/drafts/PO-DRAFT-0099"}
        return {}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    vendors = client.search_vendors("org-1", "v")
    valid, missing = client.validate_draft("PO", {"vendor_code": ""})
    draft_no, draft_url = client.create_draft("PO", {"vendor_code": "V100"}, "ik-1")

    assert vendors == [{"vendor_code": "V100", "vendor_name": "Vendor 100"}]
    assert valid is False
    assert missing == ["vendor_code"]
    assert draft_no == "PO-DRAFT-0099"
    assert draft_url == "https://erp/drafts/PO-DRAFT-0099"


def test_real_erp_client_adds_api_key_and_signature_headers(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_AUTH_MODE", "api_key")
    monkeypatch.setenv("ERP_API_KEY_HEADER", "X-ERP-Key")
    monkeypatch.setenv("ERP_API_KEY", "key-123")
    monkeypatch.setenv("ERP_SIGN_ENABLED", "true")
    monkeypatch.setenv("ERP_SIGN_HEADER", "X-ERP-Sign")
    monkeypatch.setenv("ERP_SIGN_TS_HEADER", "X-ERP-TS")
    monkeypatch.setenv("ERP_SIGN_SECRET", "secret-xyz")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    captured = {"headers": None}

    class _DummyResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"items":[]}'

    def _fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return _DummyResp()

    monkeypatch.setattr(erp_client_module.request, "urlopen", _fake_urlopen)
    client.search_vendors("org-test", "abc")

    headers = {str(k).lower(): str(v) for k, v in (captured["headers"] or {}).items()}
    assert headers.get("x-erp-key") == "key-123"
    assert headers.get("x-erp-sign")
    assert headers.get("x-erp-ts")


def test_real_erp_client_supports_request_payload_mapping(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_REQ_TYPE_FIELD", "docType")
    monkeypatch.setenv("ERP_REQ_PAYLOAD_FIELD", "content")
    monkeypatch.setenv("ERP_REQ_IDEMPOTENCY_FIELD", "idemKey")
    monkeypatch.setenv("ERP_REQ_PAYLOAD_FIELD_MAP", '{"vendor_code":"vendorCode","doc_date":"documentDate"}')
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    captured = {"validate_payload": None, "create_payload": None}

    def _fake_request_json(method, path, payload=None):
        if path == "/erp/drafts/validate":
            captured["validate_payload"] = payload
            return {"valid": True, "missing_fields": []}
        if path == "/erp/drafts":
            captured["create_payload"] = payload
            return {"draft_no": "PO-DRAFT-1001", "draft_url": "https://erp/drafts/PO-DRAFT-1001"}
        return {}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    valid, missing = client.validate_draft("PO", {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": "CNY"})
    draft_no, draft_url = client.create_draft(
        "PO",
        {"vendor_code": "V001", "doc_date": "2026-04-30", "currency": "CNY"},
        "ik-1001",
    )

    assert valid is True
    assert missing == []
    assert captured["validate_payload"] == {
        "docType": "PO",
        "content": {"vendorCode": "V001", "documentDate": "2026-04-30", "currency": "CNY"},
    }
    assert captured["create_payload"] == {
        "docType": "PO",
        "content": {"vendorCode": "V001", "documentDate": "2026-04-30", "currency": "CNY"},
        "idemKey": "ik-1001",
    }
    assert draft_no == "PO-DRAFT-1001"
    assert draft_url == "https://erp/drafts/PO-DRAFT-1001"


def test_real_erp_client_refreshes_token_on_401(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_AUTH_MODE", "bearer")
    monkeypatch.setenv("ERP_API_TOKEN", "expired-token")
    monkeypatch.setenv("ERP_AUTH_REFRESH_ENABLED", "true")
    monkeypatch.setenv("ERP_AUTH_REFRESH_URL", "https://erp.example.com/auth/refresh")
    monkeypatch.setenv("ERP_AUTH_REFRESH_ACCESS_TOKEN_FIELD", "access_token")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    calls = {"resource": 0, "refresh": 0}

    class _Resp:
        def __init__(self, body: str):
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(req, timeout):
        url = req.full_url
        if url.endswith("/auth/refresh"):
            calls["refresh"] += 1
            return _Resp('{"access_token":"new-token-001"}')
        calls["resource"] += 1
        auth = dict(req.header_items()).get("Authorization", "")
        if calls["resource"] == 1 and "expired-token" in auth:
            raise urllib.error.HTTPError(
                url=url,
                code=401,
                msg="unauthorized",
                hdrs=None,
                fp=io.BytesIO(json.dumps({"error_code": "UNAUTHORIZED", "message": "token expired"}).encode("utf-8")),
            )
        return _Resp('{"items":[{"vendor_code":"V001"}]}')

    monkeypatch.setattr(erp_client_module.request, "urlopen", _fake_urlopen)
    vendors = client.search_vendors("org-test", "v")
    assert vendors == [{"vendor_code": "V001"}]
    assert calls["refresh"] == 1
    assert calls["resource"] == 2
    assert client.token == "new-token-001"


def test_resolve_real_bases_data_only_fills_write(monkeypatch):
    monkeypatch.delenv("ERP_BASE_URL", raising=False)
    monkeypatch.delenv("ERP_WRITE_BASE_URL", raising=False)
    monkeypatch.delenv("ERP_TRANS_BASE_URL", raising=False)
    monkeypatch.setenv("ERP_DATA_BASE_URL", "https://only-master.example.com")
    import app.erp_client as erp_client_module

    d, w = erp_client_module._resolve_real_bases()
    assert d == w == "https://only-master.example.com"


def test_composite_dual_erp_client_routes_master_and_transactional():
    from unittest.mock import MagicMock

    from app.erp_client import CompositeDualErpClient

    master = MagicMock()
    trans = MagicMock()
    master.search_vendors.return_value = [{"vendor_code": "V9", "vendor_name": "Nine"}]
    master.search_materials.return_value = [{"material_code": "M9", "material_name": "NineMat"}]
    master.search_sale_orders.return_value = [{"orderNo": "SO-1", "customerName": "C1"}]
    master.search_customers.return_value = [{"customerNumber": "CU-1", "customerName": "Acme"}]
    trans.validate_draft.return_value = (True, [])
    trans.create_draft.return_value = ("DN-1", "https://draft/1")
    trans.save_customer.return_value = ("CN-001", "https://erp/c/CN-001")

    c = CompositeDualErpClient(master, trans)
    assert c.search_vendors("o1", "k1") == [{"vendor_code": "V9", "vendor_name": "Nine"}]
    assert c.search_materials("o1", "m1") == [{"material_code": "M9", "material_name": "NineMat"}]
    assert c.search_sale_orders("o1", "kw", 1, 5) == [{"orderNo": "SO-1", "customerName": "C1"}]
    assert c.search_customers("o1", "kw", 1, 5) == [{"customerNumber": "CU-1", "customerName": "Acme"}]
    ok, miss = c.validate_draft("PO", {"vendor_code": "V9"})
    assert ok and miss == []
    assert c.create_draft("PO", {}, "ik") == ("DN-1", "https://draft/1")
    assert c.save_customer({"name": "Acme"}) == ("CN-001", "https://erp/c/CN-001")
    master.validate_draft.assert_not_called()
    master.create_draft.assert_not_called()
    trans.search_vendors.assert_not_called()
    trans.search_materials.assert_not_called()
    trans.search_customers.assert_not_called()
    master.save_customer.assert_not_called()


def test_build_erp_client_dual_hosts_wraps_composite(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_DATA_BASE_URL", "https://md.example.com")
    monkeypatch.setenv("ERP_WRITE_BASE_URL", "https://tx.example.com")
    monkeypatch.setenv("ERP_API_TOKEN", "shared")
    monkeypatch.delenv("ERP_BASE_URL", raising=False)

    import app.erp_client as erp_client_module

    built: list[str] = []

    class _StubReal:
        def __init__(self, base_url="", token="", timeout_seconds=10):
            built.append(str(base_url))

        def search_vendors(self, org_id, keyword):
            return []

        def search_materials(self, org_id, keyword):
            return []

        def search_warehouses(self, org_id, keyword):
            return []

        def search_tax_codes(self, org_id, keyword):
            return []

        def search_sale_orders(self, org_id, keyword="", page_num=1, page_size=20, **kwargs):
            return []

        def search_customers(self, org_id, keyword="", page_num=1, page_size=20):
            return []

        def validate_draft(self, doc_type, payload):
            return True, []

        def create_draft(self, doc_type, payload, idempotency_key):
            return "D", "http://d"

        def save_customer(self, payload):
            return "C-MOCK", ""

    monkeypatch.setattr(erp_client_module, "RealErpClient", _StubReal)
    try:
        client = erp_client_module._build_erp_client()
        assert isinstance(client, erp_client_module.CompositeDualErpClient)
        assert built == ["https://md.example.com", "https://tx.example.com"]
    finally:
        monkeypatch.setenv("ERP_CLIENT_MODE", "mock")
        monkeypatch.delenv("ERP_DATA_BASE_URL", raising=False)
        monkeypatch.delenv("ERP_WRITE_BASE_URL", raising=False)
        importlib.reload(erp_client_module)
        assert isinstance(erp_client_module.erp_client, erp_client_module.MockErpClient)


def test_cookie_session_missing_login_credentials_raises(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://x.com")
    monkeypatch.setenv("ERP_AUTH_MODE", "cookie_session")
    monkeypatch.delenv("ERP_LOGIN_USERNAME", raising=False)
    monkeypatch.delenv("ERP_LOGIN_PASSWORD", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    c = erp_client_module.RealErpClient("https://x.com", "", 5)
    try:
        c._ensure_cookie_session()
    except erp_client_module.RealErpClient.ErpClientError as exc:
        assert exc.code == "ERP_COOKIE_LOGIN_CONFIGURE"
        assert "ERP_LOGIN_USERNAME" in exc.message
    else:
        raise AssertionError("expected ErpClientError")


def test_cookie_session_do_login_success(monkeypatch):
    monkeypatch.setenv("ERP_AUTH_MODE", "cookie_session")
    monkeypatch.setenv("ERP_LOGIN_USERNAME", "u")
    monkeypatch.setenv("ERP_LOGIN_PASSWORD", "p")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    c = erp_client_module.RealErpClient("https://erp.example.com", "", 5)

    class _Resp:
        def read(self):
            return b'{"code":200,"message":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    c._cookie_opener.open = lambda req, timeout=None: _Resp()  # type: ignore[method-assign]
    c._do_cookie_login()
    assert c._cookie_logged_in is True


def test_datynk_sale_order_create_draft_success(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_CREATE_DRAFT_PATH", "/api/sale-order/save-with-details")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_DRAFT_URL", "true")
    monkeypatch.delenv("ERP_DATYNK_DEFAULT_ORG", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)
    captured: dict[str, object] = {}

    def _fake_request_json(method, path, payload=None):
        captured["path"] = path
        captured["body"] = payload
        return {"code": 200, "message": "success", "data": "F01SO99"}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    draft_no, draft_url = client.create_draft(
        "PO",
        {
            "org": "英科1厂",
            "customerName": "北京某公司",
            "material_code": "S01",
            "line_qty": "2",
            "doc_date": "2026-05-13",
            "currency": "CNY",
            "rate": "1",
            "deliveryDate": "2026-05-20",
        },
        "ik-1",
    )
    assert draft_no == "F01SO99"
    assert draft_url == ""
    assert captured["path"] == "/api/sale-order/save-with-details"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["order"]["org"] == "英科1厂"
    assert body["order"]["customerName"] == "北京某公司"
    assert body["order"]["rate"] == 1.0
    assert body["order"]["deliveryDate"] == "2026-05-20"
    assert "jhq" not in body["order"]
    assert len(body["details"]) == 1
    assert body["details"][0]["materialCode"] == "S01"
    assert body["details"][0]["qty"] == 2.0


def test_datynk_sale_order_uses_default_org(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_DRAFT_URL", "true")
    monkeypatch.setenv("ERP_DATYNK_DEFAULT_ORG", "默认厂")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    captured: dict[str, object] = {}

    def _fake_request_json(method, path, payload=None):
        captured["body"] = payload
        return {"code": 200, "message": "success", "data": "X1"}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    client.create_draft(
        "PO",
        {
            "customerName": "客户甲",
            "material_code": "M1",
            "line_qty": "1",
            "doc_date": "2026-05-01",
        },
        "k",
    )
    assert captured["body"]["order"]["org"] == "默认厂"


def test_datynk_sale_order_rejects_non_po(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    try:
        client.create_draft("GR", {"org": "o"}, "k")
    except erp_client_module.RealErpClient.ErpClientError as exc:
        assert exc.code == "ERP_DATYNK_UNSUPPORTED_DOC_TYPE"
    else:
        raise AssertionError("expected ErpClientError")


def test_datynk_sale_order_upstream_business_error(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_DRAFT_URL", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client

    def _fake_request_json(method, path, payload=None):
        return {"code": 400, "message": "bad request"}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    try:
        client.create_draft(
            "PO",
            {
                "org": "o",
                "customerName": "c",
                "material_code": "m",
                "line_qty": "1",
                "doc_date": "2026-05-01",
            },
            "k",
        )
    except erp_client_module.RealErpClient.ErpClientError as exc:
        assert exc.status_code == 200
        assert "400" in exc.code or exc.message
    else:
        raise AssertionError("expected ErpClientError")


def test_real_erp_search_sale_orders_parses_datynk_page(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_SALE_ORDER_PAGE_ENABLED", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    sample = {
        "code": 200,
        "data": {
            "total": 2,
            "records": [
                {"orderNo": "A1", "customerName": "C1", "orderStatus": "pending"},
                {"orderNo": "A2", "customerName": "C2", "orderStatus": "completed"},
            ],
        },
    }

    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        assert method == "GET"
        return sample

    monkeypatch.setattr(client, "_request_json", _fake)
    rows = client.search_sale_orders("英科1厂", "测", 1, 10)
    assert len(rows) == 2
    assert rows[0]["orderNo"] == "A1"
    assert "pageNum=1" in captured["path"] and "org=" in captured["path"] and "customerName=" in captured["path"]


def test_real_erp_search_sale_orders_adds_date_query_when_env_and_args(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_SALE_ORDER_PAGE_ENABLED", "true")
    monkeypatch.setenv("ERP_SALE_ORDER_PAGE_DATE_BEGIN_PARAM", "beginDate")
    monkeypatch.setenv("ERP_SALE_ORDER_PAGE_DATE_END_PARAM", "endDate")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        return {"code": 200, "data": {"records": []}}

    monkeypatch.setattr(client, "_request_json", _fake)
    client.search_sale_orders("O1", "k", 1, 10, order_date_begin="2026-05-01", order_date_end="2026-05-31")
    assert "beginDate=2026-05-01" in captured["path"] and "endDate=2026-05-31" in captured["path"]


def test_real_erp_search_customers_parses_datynk_page(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_CUSTOMER_PAGE_ENABLED", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)

    sample = {
        "code": 200,
        "data": {
            "total": 1,
            "records": [
                {"customerNumber": "K-1", "customerName": "测试客户"},
            ],
        },
    }
    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        assert method == "GET"
        return sample

    monkeypatch.setattr(client, "_request_json", _fake)
    rows = client.search_customers("英科1厂", "测", 1, 10)
    assert len(rows) == 1
    assert rows[0]["customerNumber"] == "K-1"
    assert "/api/customer/page" in captured["path"]
    assert "pageNum=1" in captured["path"] and "org=" in captured["path"]


def test_save_customer_disabled_raises(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CUSTOMER_SAVE_ENABLED", "false")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    try:
        client.save_customer({"name": "x"})
    except erp_client_module.RealErpClient.ErpClientError as exc:
        assert exc.code == "ERP_CUSTOMER_SAVE_DISABLED"
    else:
        raise AssertionError("expected ErpClientError")


def test_save_customer_datynk_string_data(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CUSTOMER_SAVE_ENABLED", "true")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_CUSTOMER_URL", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    captured: dict[str, object] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        captured["body"] = payload
        return {"code": 200, "message": "success", "data": "CUST-999"}

    monkeypatch.setattr(client, "_request_json", _fake)
    k, url = client.save_customer({"org": "O1", "name": "N1", "customerNumber": "CN1"})
    assert k == "CUST-999"
    assert url == ""
    assert captured["path"] == "/api/customer/save"
    assert captured["body"]["customer"]["name"] == "N1"


def test_save_customer_datynk_dict_data(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CUSTOMER_SAVE_ENABLED", "true")
    monkeypatch.setenv("ERP_ALLOW_EMPTY_CUSTOMER_URL", "true")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client

    def _fake(method, path, payload=None):
        return {"code": 200, "data": {"customerNumber": "X-1", "customerId": 12}}

    monkeypatch.setattr(client, "_request_json", _fake)
    k, _ = client.save_customer({})
    assert k == "X-1"


def test_master_search_soft_fail_datynk_404_returns_empty(monkeypatch):
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    c = erp_client_module.RealErpClient("https://erp.example.com", "", 5)
    assert c.soft_fail_master_search is True

    def boom(*_a, **_k):
        raise erp_client_module.RealErpClient.ErpClientError("ERP_UPSTREAM_ERROR", "nf", 404, {})

    monkeypatch.setattr(c, "_request_json", boom)
    assert c.search_vendors("o", "kw") == []
    assert c.search_materials("o", "kw") == []
    assert c.search_warehouses("o", "kw") == []
    assert c.search_tax_codes("o", "kw") == []


def test_master_search_datynk_envelope_records_parsed(monkeypatch):
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    c = erp_client_module.RealErpClient("https://erp.example.com", "", 5)

    def _fake(method, path, payload=None):
        if path.startswith("/api/supplier/page"):
            return {"code": 200, "data": {"records": [{"supplierCode": "S1", "supplierName": "One"}]}}
        if path.startswith("/api/material/page"):
            return {"code": 200, "data": {"records": [{"material_code": "M1", "material_name": "Mat"}]}}
        if path.startswith("/api/warehouse/page"):
            return {"code": 200, "data": {"records": [{"warehouse_code": "W1", "warehouse_name": "Wh"}]}}
        if path.startswith("/api/tax/page"):
            return {"code": 200, "data": {"records": [{"tax_code": "T1", "tax_name": "Tax"}]}}
        return {}

    monkeypatch.setattr(c, "_request_json", _fake)
    assert c.search_vendors("org-x", "kw") == [{"supplierCode": "S1", "supplierName": "One", "vendor_code": "S1", "vendor_name": "One"}]
    assert c.search_materials("org-x", "kw") == [{"material_code": "M1", "material_name": "Mat"}]
    assert c.search_warehouses("org-x", "kw") == [{"warehouse_code": "W1", "warehouse_name": "Wh"}]
    assert c.search_tax_codes("org-x", "kw") == [{"tax_code": "T1", "tax_name": "Tax"}]


def test_datynk_supplier_page_default_uses_supplier_name_query(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.delenv("ERP_VENDORS_SEARCH_QUERY_KEY", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)
    assert getattr(client, "vendors_search_keyword_param", "") == "supplierName"

    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        return {"code": 200, "data": {"records": []}}

    monkeypatch.setattr(client, "_request_json", _fake)
    client.search_vendors("O1", "abc")
    assert "supplierName=abc" in captured["path"]


def test_datynk_material_page_default_uses_material_name_query(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.delenv("ERP_MATERIALS_SEARCH_QUERY_KEY", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert isinstance(client, erp_client_module.RealErpClient)
    assert getattr(client, "materials_search_keyword_param", "") == "materialName"

    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        return {"code": 200, "data": {"records": []}}

    monkeypatch.setattr(client, "_request_json", _fake)
    client.search_materials("O1", "abc")
    assert "materialName=abc" in captured["path"]


def test_datynk_warehouse_page_default_uses_warehouse_name_query(monkeypatch):
    monkeypatch.setenv("ERP_CLIENT_MODE", "real")
    monkeypatch.setenv("ERP_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.delenv("ERP_WAREHOUSES_SEARCH_QUERY_KEY", raising=False)
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    client = erp_client_module.erp_client
    assert getattr(client, "warehouses_search_keyword_param", "") == "warehouseName"

    captured: dict[str, str] = {}

    def _fake(method, path, payload=None):
        captured["path"] = path
        return {"code": 200, "data": {"records": []}}

    monkeypatch.setattr(client, "_request_json", _fake)
    client.search_warehouses("O1", "abc")
    assert "warehouseName=abc" in captured["path"]
    monkeypatch.setenv("ERP_CREATE_BODY_STYLE", "datynk_sale_order")
    monkeypatch.setenv("ERP_SOFT_FAIL_MASTER_SEARCH", "false")
    import app.erp_client as erp_client_module

    importlib.reload(erp_client_module)
    c = erp_client_module.RealErpClient("https://erp.example.com", "", 5)
    assert c.soft_fail_master_search is False

    def boom(*_a, **_k):
        raise erp_client_module.RealErpClient.ErpClientError("ERP_UPSTREAM_ERROR", "nf", 404, {})

    monkeypatch.setattr(c, "_request_json", boom)
    try:
        c.search_vendors("o", "kw")
    except erp_client_module.RealErpClient.ErpClientError:
        pass
    else:
        raise AssertionError("expected ErpClientError")

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib import error, parse, request
from uuid import uuid4

from app.structured_extract import required_field_keys

logger = logging.getLogger("ai_erp_api")

# 主数据分页返回字段名因系统而异：为问答展示补齐统一键（原键仍保留）。
_MASTER_DISPLAY_ALIASES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "vendor": {
        "vendor_code": ("supplierCode", "supplierNo", "supplier_code", "vendorCode", "code"),
        "vendor_name": ("supplierName", "supplier_name", "vendorName", "name"),
    },
    "material": {
        "material_code": ("materialCode", "material_code", "code"),
        "material_name": ("materialName", "material_name", "name"),
    },
    "warehouse": {
        "warehouse_code": ("warehouseCode", "warehouse_code", "code"),
        "warehouse_name": ("warehouseName", "warehouse_name", "name"),
    },
    "tax": {
        "tax_code": ("taxCode", "tax_code", "code"),
        "tax_name": ("taxName", "tax_name", "name"),
    },
}

# 记录最近一次 RealErpClient urllib 响应元数据，供 store 写入 erp_call_log；每次新 HTTP 请求前清空。
_last_upstream_meta: ContextVar[Optional[Dict[str, Any]]] = ContextVar("erp_last_upstream_meta", default=None)


@dataclass(frozen=True)
class ErpSourceAttachment:
    """Original source file attached transiently to an ERP create request."""

    file_name: str
    file_type: str
    content: bytes


def clear_last_upstream_meta() -> None:
    """丢弃当前上下文中的上游元数据（避免与无关请求串单）。"""
    _last_upstream_meta.set(None)


def consume_last_upstream_meta() -> Dict[str, Any]:
    """取出并清空最近一次 HTTP 调用的元数据（http_status / upstream_request_id / erp_path）。"""
    try:
        v = _last_upstream_meta.get()
    except LookupError:
        v = None
    _last_upstream_meta.set(None)
    return dict(v) if isinstance(v, dict) else {}


class ErpClientProtocol(Protocol):
    # ERP 适配层协议：后续接入真实 ERP 时只需实现同名方法。
    def search_vendors(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        ...

    def search_materials(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        ...

    def get_customer_material_details_by_customer(self, customer_name: str) -> List[Dict[str, str]]:
        ...

    def search_warehouses(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        ...

    def search_tax_codes(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        ...

    def validate_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        required_keys: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        ...

    def create_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        idempotency_key: str,
        source_attachment: Optional[ErpSourceAttachment] = None,
    ) -> Tuple[str, str]:
        ...

    def search_sale_orders(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
        *,
        order_date_begin: str = "",
        order_date_end: str = "",
    ) -> List[Dict[str, str]]:
        ...

    def search_customers(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
    ) -> List[Dict[str, str]]:
        """GET 分页查客户主数据（Datynk 风格：code=200 且 data.records）。"""
        ...

    def save_customer(self, payload: Dict[str, str]) -> Tuple[str, str]:
        """POST 保存客户；返回 (业务主键如 customerNumber, detail_url)。"""
        ...

    def fetch_config_read(
        self,
        path: str,
        query: Dict[str, str],
        *,
        expect_datynk_envelope: bool,
        records_path: str,
    ) -> List[Dict[str, str]]:
        """白名单内的自定义 GET（如报表分页），由 `backend/config/erp_qa_reports.json` 驱动。"""
        ...


class CompositeDualErpClient:
    """
    双 ERP 宿主：主数据查询与写单（校验/建草稿）分属两套系统时使用。

    - search_vendors / search_materials / search_warehouses / search_tax_codes / search_sale_orders / search_customers → master（如 MDG / 主数据网关）
    - validate_draft / create_draft / save_customer → transactional（如 S/4 或业务中台）
    """

    def __init__(self, master: ErpClientProtocol, transactional: ErpClientProtocol):
        self._master = master
        self._transactional = transactional

    def search_vendors(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        return self._master.search_vendors(org_id, keyword)

    def search_materials(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        return self._master.search_materials(org_id, keyword)

    def get_customer_material_details_by_customer(self, customer_name: str) -> List[Dict[str, str]]:
        return self._master.get_customer_material_details_by_customer(customer_name)

    def search_warehouses(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        return self._master.search_warehouses(org_id, keyword)

    def search_tax_codes(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        return self._master.search_tax_codes(org_id, keyword)

    def search_sale_orders(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
        *,
        order_date_begin: str = "",
        order_date_end: str = "",
    ) -> List[Dict[str, str]]:
        return self._master.search_sale_orders(
            org_id,
            keyword,
            page_num,
            page_size,
            order_date_begin=order_date_begin,
            order_date_end=order_date_end,
        )

    def search_customers(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
    ) -> List[Dict[str, str]]:
        return self._master.search_customers(org_id, keyword, page_num, page_size)

    def validate_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        required_keys: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        return self._transactional.validate_draft(doc_type, payload, required_keys=required_keys)

    def create_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        idempotency_key: str,
        source_attachment: Optional[ErpSourceAttachment] = None,
    ) -> Tuple[str, str]:
        return self._transactional.create_draft(
            doc_type,
            payload,
            idempotency_key,
            source_attachment=source_attachment,
        )

    def save_customer(self, payload: Dict[str, str]) -> Tuple[str, str]:
        return self._transactional.save_customer(payload)

    def fetch_config_read(
        self,
        path: str,
        query: Dict[str, str],
        *,
        expect_datynk_envelope: bool,
        records_path: str,
    ) -> List[Dict[str, str]]:
        return self._master.fetch_config_read(
            path,
            query,
            expect_datynk_envelope=expect_datynk_envelope,
            records_path=records_path,
        )


class MockErpClient:
    # Mock 适配器：用于当前阶段端到端打通，返回结构与未来真实 ERP 保持一致。
    customer_page_enabled = True
    def search_vendors(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        if not keyword:
            return []
        return [
            {"vendor_code": "V001", "vendor_name": "Mock Vendor One"},
            {"vendor_code": "V002", "vendor_name": "Mock Vendor Two"},
        ]

    def search_materials(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        if not keyword:
            return []
        return [
            {"material_code": "M001", "material_name": "Mock Material A"},
            {"material_code": "M002", "material_name": "Mock Material B"},
        ]

    def get_customer_material_details_by_customer(self, customer_name: str) -> List[Dict[str, str]]:
        if not customer_name:
            return []
        return [
            {
                "detailId": "1",
                "mapId": "1",
                "custMaterialCode": "N100",
                "custMaterialName": "碟簧",
                "custMaterialModel": "D100101",
                "materialNumber": "S01P019433",
                "materialName": "压缩弹簧",
                "materialModel": "左旋8*64.5*196*9.5",
                "ph": "55CrSiA",
                "status": "enabled",
            },
            {
                "detailId": "2",
                "mapId": "1",
                "custMaterialCode": "N200",
                "custMaterialName": "碟簧",
                "custMaterialModel": "D100102",
                "materialNumber": "S01P019427",
                "materialName": "压缩弹簧",
                "materialModel": "左18*252*597*10.5",
                "ph": "60Si2Mn",
                "status": "enabled",
            },
        ]

    def search_warehouses(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        if not keyword:
            return []
        return [
            {"warehouse_code": "WH01", "warehouse_name": "Mock Main Warehouse"},
            {"warehouse_code": "WH02", "warehouse_name": "Mock Transit Warehouse"},
        ]

    def search_tax_codes(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        if not keyword:
            return []
        return [
            {"tax_code": "J1", "tax_name": "Mock Output VAT 13%"},
            {"tax_code": "J0", "tax_name": "Mock Zero-rated"},
        ]

    def search_sale_orders(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
        *,
        order_date_begin: str = "",
        order_date_end: str = "",
    ) -> List[Dict[str, str]]:
        _ = order_date_begin, order_date_end
        return [
            {
                "orderNo": "MOCK-SO-001",
                "customerName": "Mock 客户（示例）",
                "orderStatus": "pending",
                "orderDate": "2026-05-01",
                "totalAllAmount": "100.00",
                "org": org_id or "MOCK-ORG",
            },
        ]

    def search_customers(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
    ) -> List[Dict[str, str]]:
        _ = page_num, page_size
        return [
            {
                "customerNumber": "MOCK-CUST-001",
                "customerName": "Mock 往来客户（示例）",
                "org": org_id or "MOCK-ORG",
            },
        ]

    def validate_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        required_keys: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        required = required_keys if required_keys is not None else required_field_keys(doc_type)
        missing = [k for k in required if not (payload.get(k) or "").strip()]
        return (len(missing) == 0, missing)

    def create_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        idempotency_key: str,
        source_attachment: Optional[ErpSourceAttachment] = None,
    ) -> Tuple[str, str]:
        _ = source_attachment
        draft_no = f"{doc_type}-DRAFT-{uuid4().hex[:8].upper()}"
        draft_url = f"https://mock-erp.local/drafts/{draft_no}"
        return draft_no, draft_url

    def save_customer(self, payload: Dict[str, str]) -> Tuple[str, str]:
        cn = (payload.get("customerNumber") or payload.get("customer_number") or "MOCK-CUST-NO").strip()
        return cn, f"https://mock-erp.local/customers/{cn}"

    def fetch_config_read(
        self,
        path: str,
        query: Dict[str, str],
        *,
        expect_datynk_envelope: bool,
        records_path: str,
    ) -> List[Dict[str, str]]:
        _ = expect_datynk_envelope, records_path
        p = (path or "").strip("/").replace("/", "_")
        return [
            {
                "_mock": "fetch_config_read",
                "path": p,
                "org": query.get("org", ""),
                "keyword": query.get("materialCode", query.get("keyword", "")),
            },
        ]


class RealErpClient:
    """
    真实 ERP HTTP 适配器（MVP 版本）。

    约定：
    - 通过环境变量读取 ERP base URL 与鉴权 token；
    - ERP 返回字段若缺失，将回退到安全默认值，避免编排层崩溃；
    - 接口契约保持与 MockErpClient 一致，便于无缝切换。
    """

    def __init__(self, base_url: str, token: str = "", timeout_seconds: int = 10):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        try:
            self.create_timeout_seconds = max(
                1,
                int(os.getenv("ERP_CREATE_TIMEOUT_SECONDS", "120").strip()),
            )
        except ValueError:
            self.create_timeout_seconds = 120
        self.auth_mode = os.getenv("ERP_AUTH_MODE", "bearer").strip().lower()
        self.api_key_header = os.getenv("ERP_API_KEY_HEADER", "X-API-Key").strip() or "X-API-Key"
        self.api_key = os.getenv("ERP_API_KEY", "").strip()
        self.auth_refresh_enabled = os.getenv("ERP_AUTH_REFRESH_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.auth_refresh_url = os.getenv("ERP_AUTH_REFRESH_URL", "").strip()
        self.auth_refresh_token = os.getenv("ERP_AUTH_REFRESH_TOKEN", "").strip()
        self.auth_refresh_field = os.getenv("ERP_AUTH_REFRESH_ACCESS_TOKEN_FIELD", "access_token").strip() or "access_token"
        self.sign_enabled = os.getenv("ERP_SIGN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.sign_header = os.getenv("ERP_SIGN_HEADER", "X-Signature").strip() or "X-Signature"
        self.sign_ts_header = os.getenv("ERP_SIGN_TS_HEADER", "X-Timestamp").strip() or "X-Timestamp"
        self.sign_secret = os.getenv("ERP_SIGN_SECRET", "").strip()
        # 允许通过环境变量适配 ERP 字段命名差异，减少改代码频率。
        self.items_field = os.getenv("ERP_ITEMS_FIELD", "items").strip() or "items"
        self.valid_field = os.getenv("ERP_VALIDATE_VALID_FIELD", "valid").strip() or "valid"
        self.missing_fields_field = os.getenv("ERP_VALIDATE_MISSING_FIELDS_FIELD", "missing_fields").strip() or "missing_fields"
        self.draft_no_field = os.getenv("ERP_DRAFT_NO_FIELD", "draft_no").strip() or "draft_no"
        self.draft_url_field = os.getenv("ERP_DRAFT_URL_FIELD", "draft_url").strip() or "draft_url"
        self.error_code_field = os.getenv("ERP_ERROR_CODE_FIELD", "error_code").strip() or "error_code"
        self.error_message_field = os.getenv("ERP_ERROR_MESSAGE_FIELD", "message").strip() or "message"
        # 请求体字段映射配置：
        # - 外层字段名映射（type/payload/idempotency_key）
        # - payload 内字段重命名映射（JSON 字符串）
        self.req_type_field = os.getenv("ERP_REQ_TYPE_FIELD", "type").strip() or "type"
        self.req_payload_field = os.getenv("ERP_REQ_PAYLOAD_FIELD", "payload").strip() or "payload"
        self.req_idempotency_field = os.getenv("ERP_REQ_IDEMPOTENCY_FIELD", "idempotency_key").strip() or "idempotency_key"
        self.req_payload_map = self._load_payload_map("ERP_REQ_PAYLOAD_FIELD_MAP")
        # 主数据查询路径（可按真实 ERP OpenAPI 覆盖；列表字段名仍由 ERP_ITEMS_FIELD 决定）
        self.vendors_search_path = os.getenv("ERP_VENDORS_SEARCH_PATH", "/erp/vendors/search").strip() or "/erp/vendors/search"
        self.materials_search_path = (
            os.getenv("ERP_MATERIALS_SEARCH_PATH", "/erp/materials/search").strip() or "/erp/materials/search"
        )
        self.warehouses_search_path = (
            os.getenv("ERP_WAREHOUSES_SEARCH_PATH", "/erp/warehouses/search").strip() or "/erp/warehouses/search"
        )
        self.tax_codes_search_path = (
            os.getenv("ERP_TAX_CODES_SEARCH_PATH", "/erp/tax-codes/search").strip() or "/erp/tax-codes/search"
        )
        self.validate_path = os.getenv("ERP_VALIDATE_PATH", "/erp/drafts/validate").strip() or "/erp/drafts/validate"
        self.create_draft_path = os.getenv("ERP_CREATE_DRAFT_PATH", "/erp/drafts").strip() or "/erp/drafts"
        self.skip_upstream_validate = os.getenv("ERP_SKIP_UPSTREAM_VALIDATE", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.allow_empty_draft_url = os.getenv("ERP_ALLOW_EMPTY_DRAFT_URL", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        # legacy: {type,payload,idempotency_key}；datynk_sale_order：POST {order,details}，响应 {code,message,data}
        self.create_body_style = os.getenv("ERP_CREATE_BODY_STYLE", "legacy").strip().lower() or "legacy"
        self.datynk_default_org = os.getenv("ERP_DATYNK_DEFAULT_ORG", "").strip()
        self.datynk_default_sales_user = os.getenv("ERP_DATYNK_DEFAULT_SALES_USER", "").strip()
        # Cookie 会话（如 Datynk POST /api/auth/login 后带 Cookie 调业务接口）
        self._cookie_opener: Optional[Any] = None
        self._cookie_logged_in = False
        self._cookie_login_path = ""
        self._cookie_username = ""
        self._cookie_password = ""
        if self.auth_mode == "cookie_session":
            jar = http.cookiejar.CookieJar()
            self._cookie_opener = request.build_opener(request.HTTPCookieProcessor(jar))
            self._cookie_login_path = os.getenv("ERP_COOKIE_LOGIN_PATH", "/api/auth/login").strip() or "/api/auth/login"
            self._cookie_username = os.getenv("ERP_LOGIN_USERNAME", "").strip()
            self._cookie_password = os.getenv("ERP_LOGIN_PASSWORD", "").strip()
        _so_raw = os.getenv("ERP_SALE_ORDER_PAGE_ENABLED", "").strip().lower()
        style_datynk = self.create_body_style == "datynk_sale_order"
        if _so_raw in ("1", "true", "yes", "on"):
            self.sale_order_page_enabled = True
        elif _so_raw in ("0", "false", "no", "off"):
            self.sale_order_page_enabled = False
        else:
            self.sale_order_page_enabled = style_datynk
        self.sale_order_page_path = os.getenv("ERP_SALE_ORDER_PAGE_PATH", "/api/sale-order/page").strip() or "/api/sale-order/page"
        self.sale_order_page_date_begin_param = os.getenv("ERP_SALE_ORDER_PAGE_DATE_BEGIN_PARAM", "").strip()
        self.sale_order_page_date_end_param = os.getenv("ERP_SALE_ORDER_PAGE_DATE_END_PARAM", "").strip()
        _cp_raw = os.getenv("ERP_CUSTOMER_PAGE_ENABLED", "").strip().lower()
        if _cp_raw in ("1", "true", "yes", "on"):
            self.customer_page_enabled = True
        elif _cp_raw in ("0", "false", "no", "off"):
            self.customer_page_enabled = False
        else:
            self.customer_page_enabled = style_datynk
        self.customer_page_path = os.getenv("ERP_CUSTOMER_PAGE_PATH", "/api/customer/page").strip() or "/api/customer/page"
        self.customer_page_keyword_param = (
            os.getenv("ERP_CUSTOMER_PAGE_KEYWORD_PARAM", "customerName").strip() or "customerName"
        )
        self.customer_material_details_by_customer_path = (
            os.getenv("ERP_CUSTOMER_MATERIAL_DETAILS_BY_CUSTOMER_PATH", "/api/customer-material/details-by-customer").strip()
            or "/api/customer-material/details-by-customer"
        )
        _sf = os.getenv("ERP_SOFT_FAIL_MASTER_SEARCH", "").strip().lower()
        if _sf in ("1", "true", "yes", "on"):
            self.soft_fail_master_search = True
        elif _sf in ("0", "false", "no", "off"):
            self.soft_fail_master_search = False
        else:
            # Datynk 等未提供 /erp/vendors/search 类路径时，避免问答里「查供应商」直接把整段聊天打成 502。
            self.soft_fail_master_search = style_datynk
        self.customer_save_enabled = os.getenv("ERP_CUSTOMER_SAVE_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.customer_save_path = os.getenv("ERP_CUSTOMER_SAVE_PATH", "/api/customer/save").strip() or "/api/customer/save"
        self.customer_wrap_field = os.getenv("ERP_CUSTOMER_REQUEST_WRAP_FIELD", "customer").strip() or "customer"
        self.customer_payload_map = self._load_payload_map("ERP_CUSTOMER_PAYLOAD_FIELD_MAP")
        _cust_empty_url = os.getenv("ERP_ALLOW_EMPTY_CUSTOMER_URL", "").strip().lower()
        if _cust_empty_url in ("1", "true", "yes", "on"):
            self.allow_empty_customer_detail_url = True
        elif _cust_empty_url in ("0", "false", "no", "off"):
            self.allow_empty_customer_detail_url = False
        else:
            self.allow_empty_customer_detail_url = self.allow_empty_draft_url
        self.custom_read_path_prefix = os.getenv("ERP_CUSTOM_READ_PATH_PREFIX", "/api/").strip() or "/api/"
        # 主数据 GET：泛 ERP「/erp/.../search + {items:[]}」vs Datynk「/api/.../page?org&pageNum&pageSize&keyword + {code,data.records}」
        _mde_raw = os.getenv("ERP_MASTER_SEARCH_DATYNK_ENVELOPE", "").strip().lower()
        if _mde_raw in ("1", "true", "yes", "on"):
            self.master_search_datynk_envelope = True
        elif _mde_raw in ("0", "false", "no", "off"):
            self.master_search_datynk_envelope = False
        else:
            self.master_search_datynk_envelope = style_datynk
        _mqs = os.getenv("ERP_MASTER_SEARCH_QUERY_STYLE", "").strip().lower()
        if _mqs == "legacy":
            self.master_search_query_style = "legacy"
        elif _mqs == "datynk_page":
            self.master_search_query_style = "datynk_page"
        else:
            self.master_search_query_style = "datynk_page" if style_datynk else "legacy"
        try:
            self.master_search_page_size = max(1, min(100, int(os.getenv("ERP_MASTER_SEARCH_PAGE_SIZE", "20").strip())))
        except ValueError:
            self.master_search_page_size = 20
        _dmp = os.getenv("ERP_DATYNK_DEFAULT_MASTER_PATHS", "").strip().lower()
        if _dmp in ("1", "true", "yes", "on"):
            apply_datynk_paths = True
        elif _dmp in ("0", "false", "no", "off"):
            apply_datynk_paths = False
        else:
            apply_datynk_paths = style_datynk
        if apply_datynk_paths:
            if self.vendors_search_path == "/erp/vendors/search":
                # Datynk 实际为 supplier 分页，非 vendor（见浏览器 Network：/api/supplier/page）
                self.vendors_search_path = "/api/supplier/page"
            if self.materials_search_path == "/erp/materials/search":
                self.materials_search_path = "/api/material/page"
            if self.warehouses_search_path == "/erp/warehouses/search":
                self.warehouses_search_path = "/api/warehouse/page"
            if self.tax_codes_search_path == "/erp/tax-codes/search":
                self.tax_codes_search_path = "/api/tax/page"
        # 主数据模糊参数字段名：Datynk /api/supplier/page 常见为 supplierName，非 keyword
        _vkq_e = os.getenv("ERP_VENDORS_SEARCH_QUERY_KEY", "").strip()
        if _vkq_e:
            self.vendors_search_keyword_param = _vkq_e
        else:
            _vp = (self.vendors_search_path or "").replace("\\", "/").rstrip("/")
            self.vendors_search_keyword_param = "supplierName" if _vp.endswith("/supplier/page") else "keyword"
        # Datynk /api/material/page、/api/warehouse/page 与 supplier 分页类似，常用 *Name 而非 keyword
        _mkq_e = os.getenv("ERP_MATERIALS_SEARCH_QUERY_KEY", "").strip()
        if _mkq_e:
            self.materials_search_keyword_param = _mkq_e
        else:
            _mp = (self.materials_search_path or "").replace("\\", "/").rstrip("/")
            self.materials_search_keyword_param = "materialName" if _mp.endswith("/material/page") else "keyword"
        _wkq_e = os.getenv("ERP_WAREHOUSES_SEARCH_QUERY_KEY", "").strip()
        if _wkq_e:
            self.warehouses_search_keyword_param = _wkq_e
        else:
            _wp = (self.warehouses_search_path or "").replace("\\", "/").rstrip("/")
            self.warehouses_search_keyword_param = "warehouseName" if _wp.endswith("/warehouse/page") else "keyword"
        self.tax_codes_search_keyword_param = (
            os.getenv("ERP_TAX_CODES_SEARCH_QUERY_KEY", "keyword").strip() or "keyword"
        )

    class ErpClientError(Exception):
        def __init__(self, code: str, message: str, status_code: int = 0, details: Optional[Dict] = None):
            super().__init__(message)
            self.code = code
            self.message = message
            self.status_code = status_code
            self.details = details or {}

    @staticmethod
    def _load_payload_map(env_key: str) -> Dict[str, str]:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            mapped: Dict[str, str] = {}
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    mapped[k.strip()] = v.strip()
            return mapped
        except Exception:
            logger.warning("erp_payload_map_parse_failed env_key=%s", env_key)
            return {}

    def _headers(self, method: str, path: str, body_bytes: Optional[bytes]) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if body_bytes:
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif method.upper() != "GET":
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.auth_mode == "cookie_session":
            pass
        elif self.auth_mode == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.auth_mode == "api_key" and self.api_key:
            headers[self.api_key_header] = self.api_key
        if self.sign_enabled and self.sign_secret:
            ts = str(int(time.time()))
            payload = body_bytes.decode("utf-8") if body_bytes else ""
            sign_raw = f"{method.upper()}|{path}|{payload}|{ts}"
            signature = hmac.new(self.sign_secret.encode("utf-8"), sign_raw.encode("utf-8"), hashlib.sha256).hexdigest()
            headers[self.sign_ts_header] = ts
            headers[self.sign_header] = signature
        return headers

    def _ensure_cookie_session(self) -> None:
        if self.auth_mode != "cookie_session" or self._cookie_opener is None:
            return
        if self._cookie_logged_in:
            return
        if not self._cookie_username or not self._cookie_password:
            raise RealErpClient.ErpClientError(
                code="ERP_COOKIE_LOGIN_CONFIGURE",
                message="ERP_AUTH_MODE=cookie_session 时需配置 ERP_LOGIN_USERNAME 与 ERP_LOGIN_PASSWORD",
                status_code=0,
                details={},
            )
        self._do_cookie_login()

    def _do_cookie_login(self) -> None:
        if self._cookie_opener is None:
            return
        url = f"{self.base_url}{self._cookie_login_path}"
        body = json.dumps({"username": self._cookie_username, "password": self._cookie_password}).encode("utf-8")
        req = request.Request(url=url, method="POST", headers={"Content-Type": "application/json"}, data=body)
        try:
            with self._cookie_opener.open(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            raise RealErpClient.ErpClientError(
                code="ERP_UPSTREAM_ERROR",
                message=f"cookie_login_http status={exc.code}",
                status_code=exc.code,
                details={},
            ) from exc
        except error.URLError as exc:
            raise RealErpClient.ErpClientError(
                code="UPSTREAM_TIMEOUT",
                message=str(exc),
                status_code=0,
                details={"reason": str(exc)},
            ) from exc
        try:
            parsed: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RealErpClient.ErpClientError(
                code="ERP_COOKIE_LOGIN_FAILED",
                message="login response not json",
                status_code=0,
                details={"raw": raw[:200]},
            ) from exc
        if not RealErpClient._datynk_http_ok(parsed.get("code")):
            msg = str(parsed.get(self.error_message_field) or parsed.get("message") or "login_failed")
            raise RealErpClient.ErpClientError(
                code="ERP_COOKIE_LOGIN_FAILED",
                message=msg,
                status_code=200,
                details={"body": parsed},
            )
        self._cookie_logged_in = True
        logger.info("erp_cookie_login_ok path=%s", self._cookie_login_path)

    def _open_request(self, req: request.Request, *, timeout_seconds: Optional[int] = None):
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        if getattr(self, "_cookie_opener", None) is not None:
            self._ensure_cookie_session()
            return self._cookie_opener.open(req, timeout=timeout)
        return request.urlopen(req, timeout=timeout)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict] = None,
        *,
        timeout_seconds: Optional[int] = None,
    ) -> Dict:
        return self._request_json_internal(
            method,
            path,
            payload,
            allow_refresh=True,
            timeout_seconds=timeout_seconds,
        )

    def _request_json_internal(
        self,
        method: str,
        path: str,
        payload: Optional[Dict],
        allow_refresh: bool,
        timeout_seconds: Optional[int] = None,
    ) -> Dict:
        _last_upstream_meta.set(None)
        url = f"{self.base_url}{path}"
        data: Optional[bytes] = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url=url, method=method, headers=self._headers(method, path, data), data=data)
        try:
            with self._open_request(req, timeout_seconds=timeout_seconds) as resp:
                if callable(getattr(resp, "getcode", None)):
                    code = int(resp.getcode())
                else:
                    code = int(getattr(resp, "status", 200))
                rid = ""
                hdrs = getattr(resp, "headers", None)
                if hdrs:
                    rid = (hdrs.get("X-Request-Id") or hdrs.get("x-request-id") or "").strip()
                raw = resp.read().decode("utf-8")
                if not raw:
                    _last_upstream_meta.set(
                        {"http_status": code, "erp_path": path, "upstream_request_id": rid},
                    )
                    return {}
                parsed: Any = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("request_id"):
                    rid = str(parsed.get("request_id")).strip() or rid
                _last_upstream_meta.set(
                    {"http_status": code, "erp_path": path, "upstream_request_id": rid},
                )
                if isinstance(parsed, dict):
                    return parsed
                return {}
        except error.HTTPError as exc:
            rid_err = ""
            if exc.headers:
                rid_err = (exc.headers.get("X-Request-Id") or exc.headers.get("x-request-id") or "").strip()
            _last_upstream_meta.set(
                {"http_status": exc.code, "erp_path": path, "upstream_request_id": rid_err},
            )
            raw = exc.read().decode("utf-8", errors="ignore")
            code = "ERP_UPSTREAM_ERROR"
            message = f"http_error status={exc.code}"
            details: Dict = {}
            if raw:
                try:
                    body = json.loads(raw)
                    code = str(body.get(self.error_code_field) or code)
                    message = str(body.get(self.error_message_field) or message)
                    if isinstance(body.get("details"), dict):
                        details = body.get("details", {})
                    if isinstance(body.get("violations"), list):
                        details["violations"] = body.get("violations")
                    if body.get("request_id"):
                        details["request_id"] = str(body.get("request_id"))
                except Exception:
                    message = raw[:200]
            if exc.code == 401 and allow_refresh and self.auth_mode == "cookie_session" and self._cookie_opener is not None:
                self._cookie_logged_in = False
                self._ensure_cookie_session()
                return self._request_json_internal(
                    method,
                    path,
                    payload,
                    allow_refresh=False,
                    timeout_seconds=timeout_seconds,
                )
            if exc.code == 401 and allow_refresh and self._try_refresh_access_token():
                return self._request_json_internal(
                    method,
                    path,
                    payload,
                    allow_refresh=False,
                    timeout_seconds=timeout_seconds,
                )
            raise RealErpClient.ErpClientError(code=code, message=message, status_code=exc.code, details=details) from exc
        except error.URLError as exc:
            _last_upstream_meta.set({"http_status": 0, "erp_path": path, "upstream_request_id": ""})
            raise RealErpClient.ErpClientError(
                code="UPSTREAM_TIMEOUT",
                message=str(exc),
                status_code=0,
                details={"reason": str(exc)},
            ) from exc

    def _try_refresh_access_token(self) -> bool:
        if self.auth_mode != "bearer":
            return False
        if not self.auth_refresh_enabled or not self.auth_refresh_url:
            return False
        payload = {}
        if self.auth_refresh_token:
            payload = {"refresh_token": self.auth_refresh_token}
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=self.auth_refresh_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=data,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return False
                body = json.loads(raw)
                new_token = str(body.get(self.auth_refresh_field, "")).strip()
                if not new_token:
                    return False
                self.token = new_token
                logger.info("erp_access_token_refreshed")
                return True
        except Exception:
            logger.warning("erp_access_token_refresh_failed")
            return False

    def _map_payload_fields(self, payload: Dict[str, str]) -> Dict[str, str]:
        if not self.req_payload_map:
            return payload
        mapped: Dict[str, str] = {}
        for k, v in payload.items():
            to_key = self.req_payload_map.get(k, k)
            mapped[to_key] = v
        return mapped

    @staticmethod
    def _normalize_items(items: object) -> List[Dict[str, str]]:
        if not isinstance(items, list):
            return []
        result: List[Dict[str, str]] = []
        for i in items:
            if not isinstance(i, dict):
                continue
            result.append({str(k): str(v) for k, v in i.items() if v is not None})
        return result

    @staticmethod
    def _apply_master_display_aliases(kind: str, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        spec = _MASTER_DISPLAY_ALIASES.get(kind)
        if not spec:
            return rows
        out: List[Dict[str, str]] = []
        for row in rows:
            merged = dict(row)
            for canonical, aliases in spec.items():
                if (merged.get(canonical) or "").strip():
                    continue
                for ak in aliases:
                    val = row.get(ak)
                    if val is not None and str(val).strip():
                        merged[canonical] = str(val).strip()
                        break
            out.append(merged)
        return out

    def _master_search_http_soft_fail(self, exc: "RealErpClient.ErpClientError") -> bool:
        if not getattr(self, "soft_fail_master_search", False):
            return False
        return int(exc.status_code or 0) in (404, 405)

    def _parse_master_search_items(self, body: Any) -> List[Dict[str, str]]:
        if not isinstance(body, dict):
            return []
        if getattr(self, "master_search_datynk_envelope", False):
            if not RealErpClient._datynk_http_ok(body.get("code")):
                logger.warning(
                    "erp_master_search_non_ok code=%s msg=%s",
                    body.get("code"),
                    body.get("message"),
                )
                return []
            data = body.get("data")
            if isinstance(data, dict):
                rec = data.get("records")
                if isinstance(rec, list):
                    return self._normalize_items(rec)
                for alt in ("items", "list", "rows"):
                    alt_l = data.get(alt)
                    if isinstance(alt_l, list):
                        return self._normalize_items(alt_l)
            return []
        raw_items = body.get(self.items_field, [])
        return self._normalize_items(raw_items)

    def _master_search_query_legacy(self, org_id: str, keyword: str) -> str:
        return parse.urlencode({"org_id": org_id, "keyword": keyword})

    def _master_search_query_datynk_page(self, org_id: str, keyword: str, keyword_param: str) -> str:
        ps = int(getattr(self, "master_search_page_size", 20))
        params: Dict[str, Any] = {"pageNum": 1, "pageSize": ps}
        o = (org_id or "").strip()
        if o:
            params["org"] = o
        kw = (keyword or "").strip()
        if kw:
            params[keyword_param] = kw
        return parse.urlencode(params)

    def _master_search_get(self, path: str, org_id: str, keyword: str, keyword_param: str) -> List[Dict[str, str]]:
        p = path if path.startswith("/") else f"/{path}"
        if getattr(self, "master_search_query_style", "legacy") == "datynk_page":
            qs = self._master_search_query_datynk_page(org_id, keyword, keyword_param)
            full = f"{p}?{qs}"
        else:
            qs = self._master_search_query_legacy(org_id, keyword)
            full = f"{p}?{qs}"
        try:
            body = self._request_json("GET", full)
        except RealErpClient.ErpClientError as exc:
            if self._master_search_http_soft_fail(exc):
                logger.warning("erp_master_search_soft_fail status=%s path=%s", exc.status_code, p)
                return []
            raise
        return self._parse_master_search_items(body)

    def search_vendors(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        raw = self._master_search_get(self.vendors_search_path, org_id, keyword, self.vendors_search_keyword_param)
        return RealErpClient._apply_master_display_aliases("vendor", raw)

    def search_materials(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        raw = self._master_search_get(self.materials_search_path, org_id, keyword, self.materials_search_keyword_param)
        return RealErpClient._apply_master_display_aliases("material", raw)

    def get_customer_material_details_by_customer(self, customer_name: str) -> List[Dict[str, str]]:
        name = (customer_name or "").strip()
        if not name:
            return []
        path = self.customer_material_details_by_customer_path
        p = path if path.startswith("/") else f"/{path}"
        qs = parse.urlencode({"customerName": name})
        body = self._request_json("GET", f"{p}?{qs}")
        code = body.get("code")
        ok = False
        try:
            ok = int(code) in (0, 200)
        except (TypeError, ValueError):
            ok = str(code).strip() in {"0", "200"}
        if not ok:
            logger.warning(
                "erp_customer_material_details_bad_code code=%s message=%s",
                code,
                body.get("message"),
            )
            return []
        return self._normalize_items(body.get("data"))

    def search_warehouses(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        raw = self._master_search_get(self.warehouses_search_path, org_id, keyword, self.warehouses_search_keyword_param)
        return RealErpClient._apply_master_display_aliases("warehouse", raw)

    def search_tax_codes(self, org_id: str, keyword: str) -> List[Dict[str, str]]:
        raw = self._master_search_get(self.tax_codes_search_path, org_id, keyword, self.tax_codes_search_keyword_param)
        return RealErpClient._apply_master_display_aliases("tax", raw)

    def _validate_custom_read_path(self, path: str) -> str:
        p = (path or "").strip()
        if not p.startswith("/") or ".." in p:
            raise RealErpClient.ErpClientError(
                code="ERP_BAD_PATH",
                message="custom read path must start with / and must not contain ..",
                status_code=0,
                details={"path": path},
            )
        prefix = (self.custom_read_path_prefix or "/api/").strip()
        if prefix and not p.startswith(prefix):
            raise RealErpClient.ErpClientError(
                code="ERP_PATH_NOT_ALLOWED",
                message=f"path must start with prefix {prefix!r}",
                status_code=0,
                details={"path": p, "prefix": prefix},
            )
        return p

    @staticmethod
    def _dig_json(obj: Any, dotted: str) -> Any:
        cur = obj
        for part in (dotted or "").split("."):
            part = part.strip()
            if not part:
                continue
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def fetch_config_read(
        self,
        path: str,
        query: Dict[str, str],
        *,
        expect_datynk_envelope: bool,
        records_path: str,
    ) -> List[Dict[str, str]]:
        """配置驱动的只读 GET（问答报表）；路径必须在 ERP_CUSTOM_READ_PATH_PREFIX 之下。"""
        p = self._validate_custom_read_path(path)
        q = {k: str(v) for k, v in (query or {}).items() if str(v).strip() != ""}
        qs = parse.urlencode(q)
        full = f"{p}?{qs}" if qs else p
        try:
            body = self._request_json("GET", full)
        except RealErpClient.ErpClientError as exc:
            if self._master_search_http_soft_fail(exc):
                logger.warning("erp_fetch_config_read_soft_fail status=%s path=%s", exc.status_code, p)
                return []
            raise
        if expect_datynk_envelope:
            if not RealErpClient._datynk_http_ok(body.get("code")):
                logger.warning("erp_fetch_config_read_bad_code path=%s code=%s", p, body.get("code"))
                return []
        rec = self._dig_json(body, records_path or "data.records")
        if isinstance(rec, list):
            return self._normalize_items(rec)
        if isinstance(rec, dict):
            return self._normalize_items([rec])
        return []

    def search_sale_orders(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
        *,
        order_date_begin: str = "",
        order_date_end: str = "",
    ) -> List[Dict[str, str]]:
        if not self.sale_order_page_enabled:
            return []
        try:
            pn = max(1, int(page_num))
        except (TypeError, ValueError):
            pn = 1
        try:
            ps = max(1, min(100, int(page_size)))
        except (TypeError, ValueError):
            ps = 20
        params: Dict[str, Any] = {"pageNum": pn, "pageSize": ps}
        o = (org_id or "").strip()
        if o:
            params["org"] = o
        kw = (keyword or "").strip()
        if kw:
            params["customerName"] = kw
        db = (order_date_begin or "").strip()
        de = (order_date_end or "").strip()
        bp = getattr(self, "sale_order_page_date_begin_param", "") or ""
        ep = getattr(self, "sale_order_page_date_end_param", "") or ""
        if db and de and bp and ep:
            params[bp] = db
            params[ep] = de
        path_base = (
            self.sale_order_page_path if self.sale_order_page_path.startswith("/") else f"/{self.sale_order_page_path}"
        )
        qs = parse.urlencode(params)
        body = self._request_json("GET", f"{path_base}?{qs}")
        if not RealErpClient._datynk_http_ok(body.get("code")):
            logger.warning(
                "erp_search_sale_orders_bad_code code=%s keys=%s",
                body.get("code"),
                list(body.keys()) if isinstance(body, dict) else [],
            )
            return []
        data = body.get("data")
        if not isinstance(data, dict):
            return []
        records = data.get("records")
        return self._normalize_items(records)

    def search_customers(
        self,
        org_id: str,
        keyword: str = "",
        page_num: int = 1,
        page_size: int = 20,
    ) -> List[Dict[str, str]]:
        if not getattr(self, "customer_page_enabled", False):
            return []
        try:
            pn = max(1, int(page_num))
        except (TypeError, ValueError):
            pn = 1
        try:
            ps = max(1, min(100, int(page_size)))
        except (TypeError, ValueError):
            ps = 20
        params: Dict[str, Any] = {"pageNum": pn, "pageSize": ps}
        o = (org_id or "").strip()
        if o:
            params["org"] = o
        kw = (keyword or "").strip()
        if kw:
            params[self.customer_page_keyword_param] = kw
        path_base = (
            self.customer_page_path if self.customer_page_path.startswith("/") else f"/{self.customer_page_path}"
        )
        qs = parse.urlencode(params)
        try:
            body = self._request_json("GET", f"{path_base}?{qs}")
        except RealErpClient.ErpClientError as exc:
            if self._master_search_http_soft_fail(exc):
                logger.warning(
                    "erp_search_customers_soft_fail status=%s path=%s",
                    exc.status_code,
                    self.customer_page_path,
                )
                return []
            raise
        if not RealErpClient._datynk_http_ok(body.get("code")):
            logger.warning(
                "erp_search_customers_bad_code code=%s keys=%s",
                body.get("code"),
                list(body.keys()) if isinstance(body, dict) else [],
            )
            return []
        data = body.get("data")
        if not isinstance(data, dict):
            return []
        records = data.get("records")
        return self._normalize_items(records)

    def validate_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        required_keys: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        local_missing: List[str] = []
        if required_keys:
            local_missing = [k for k in required_keys if not (payload.get(k) or "").strip()]

        if self.skip_upstream_validate:
            # 联调第一阶段：ERP 侧暂未提供 validate 接口时，仅做本地必填键检查（与 Mock 口径对齐）。
            if local_missing:
                return False, local_missing
            return True, []

        body = self._request_json(
            "POST",
            self.validate_path,
            {
                self.req_type_field: doc_type,
                self.req_payload_field: self._map_payload_fields(payload),
            },
        )
        erp_valid = bool(body.get(self.valid_field, False))
        erp_missing = body.get(self.missing_fields_field, [])
        if not isinstance(erp_missing, list):
            erp_missing = []
        erp_missing = [str(x) for x in erp_missing]
        if not erp_valid and not erp_missing:
            # 如果 ERP 未返回缺失字段，至少回填一个稳定占位，避免前端空态无法提示。
            erp_missing = ["erp_validation_failed"]
        ok = erp_valid and not local_missing
        if ok:
            return True, []
        merged = list(dict.fromkeys([*erp_missing, *local_missing]))
        return False, merged

    @staticmethod
    def _datynk_pick(payload: Dict[str, str], *keys: str, default: str = "") -> str:
        for k in keys:
            v = payload.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return default

    @staticmethod
    def _safe_float(raw: Optional[str], fallback: float) -> float:
        if raw is None or not str(raw).strip():
            return fallback
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            return fallback

    @staticmethod
    def _datynk_http_ok(code: object) -> bool:
        if code is None:
            return False
        try:
            return int(code) == 200
        except (TypeError, ValueError):
            return str(code).strip() == "200"

    _DATYNK_REQUIRED_DETAIL_FIELDS: Tuple[Tuple[str, str], ...] = (
        ("materialCode", "物料编码"),
        ("qty", "数量"),
        ("price", "不含税单价"),
        ("taxPrice", "含税单价"),
        ("amount", "不含税金额"),
        ("allAmount", "含税金额"),
        ("tax", "税率"),
    )

    @staticmethod
    def _datynk_detail_value_missing(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return False
        return not str(value).strip()

    @classmethod
    def _validate_datynk_sale_order_details(cls, details: List[Dict[str, Any]]) -> None:
        missing: List[Dict[str, Any]] = []
        for index, detail in enumerate(details):
            for key, label in cls._DATYNK_REQUIRED_DETAIL_FIELDS:
                if cls._datynk_detail_value_missing(detail.get(key)):
                    missing.append({"row": index + 1, "field": key, "label": label})
        if not missing:
            return
        labels = "、".join(f"第 {item['row']} 行{item['label']}" for item in missing[:12])
        if len(missing) > 12:
            labels += f" 等 {len(missing)} 项"
        raise RealErpClient.ErpClientError(
            code="ERP_DATYNK_MISSING_DETAIL_FIELDS",
            message=f"创建 ERP 订单失败：订单明细必填字段为空，请补齐 {labels}。",
            status_code=0,
            details={"missing_fields": missing},
        )

    def _build_datynk_sale_order_body(
        self,
        doc_type: str,
        payload: Dict[str, str],
        source_attachment: Optional[ErpSourceAttachment] = None,
    ) -> Dict[str, Any]:
        """组装 https://.../api/sale-order/save-with-details 所需 JSON（与对端 Postman 示例一致）。"""
        dt = (doc_type or "").strip().upper()
        if dt != "PO":
            raise RealErpClient.ErpClientError(
                code="ERP_DATYNK_UNSUPPORTED_DOC_TYPE",
                message="ERP_CREATE_BODY_STYLE=datynk_sale_order 仅支持 PO（销售订单）建单",
                status_code=0,
                details={"doc_type": doc_type},
            )
        org = self._datynk_pick(payload, "org", "org_name") or self.datynk_default_org
        if not org:
            raise RealErpClient.ErpClientError(
                code="ERP_DATYNK_MISSING_ORG",
                message="缺少 org：请在补全字段中填写 org，或配置环境变量 ERP_DATYNK_DEFAULT_ORG",
                status_code=0,
                details={},
            )
        customer = self._datynk_pick(payload, "customerName", "customer_name")
        if not customer:
            customer = self._datynk_pick(payload, "vendor_code")
        if not customer:
            raise RealErpClient.ErpClientError(
                code="ERP_DATYNK_MISSING_CUSTOMER",
                message="缺少 customerName：请填写 customerName/customer_name，或提供 vendor_code 作为占位",
                status_code=0,
                details={},
            )
        material = self._datynk_pick(payload, "material_code")
        if not material:
            raise RealErpClient.ErpClientError(
                code="ERP_DATYNK_MISSING_MATERIAL",
                message="缺少 material_code",
                status_code=0,
                details={},
            )
        line_qty_raw = self._datynk_pick(payload, "line_qty", "qty")
        qty = self._safe_float(line_qty_raw, 0.0)
        doc_date = self._datynk_pick(payload, "doc_date", "orderDate", "order_date")
        if not doc_date:
            raise RealErpClient.ErpClientError(
                code="ERP_DATYNK_MISSING_ORDER_DATE",
                message="缺少 doc_date（订单日期）",
                status_code=0,
                details={},
            )
        currency = self._datynk_pick(payload, "currency") or "CNY"
        delivery_date = self._datynk_pick(payload, "deliveryDate", "delivery_date", "jhq") or doc_date
        rate = self._safe_float(self._datynk_pick(payload, "rate"), 1.0)
        sales_user = self._datynk_pick(payload, "salesUser", "sales_user") or self.datynk_default_sales_user
        order_status = self._datynk_pick(payload, "orderStatus", "order_status") or "pending"
        delivery_addr = self._datynk_pick(payload, "deliveryAddr", "delivery_addr", "delivery_address")
        customer_po = self._datynk_pick(payload, "customerPoNo", "customer_po_no")

        details_override = self._datynk_pick(payload, "datynk_details_json", "details_json")
        if details_override:
            try:
                parsed = json.loads(details_override)
            except json.JSONDecodeError as exc:
                raise RealErpClient.ErpClientError(
                    code="ERP_DATYNK_INVALID_DETAILS_JSON",
                    message="datynk_details_json 不是合法 JSON",
                    status_code=0,
                    details={"reason": str(exc)},
                ) from exc
            if not isinstance(parsed, list) or not parsed:
                raise RealErpClient.ErpClientError(
                    code="ERP_DATYNK_INVALID_DETAILS_JSON",
                    message="datynk_details_json 须为非空 JSON 数组",
                    status_code=0,
                    details={},
                )
            details: List[Dict[str, Any]] = list(parsed)
        else:
            price_raw = self._datynk_pick(payload, "unit_price", "line_price", "price")
            tax_price_raw = self._datynk_pick(payload, "taxPrice", "tax_price", "unit_price_incl_tax")
            amount_raw = self._datynk_pick(payload, "amount", "line_amount_excl_tax")
            all_amount_raw = self._datynk_pick(payload, "allAmount", "all_amount", "line_amount_incl_tax")
            tax_raw = self._datynk_pick(payload, "tax", "tax_rate")
            price = self._safe_float(price_raw, 0.0)
            tax_price = self._safe_float(tax_price_raw, 0.0)
            amount = self._safe_float(amount_raw, 0.0)
            all_amount = self._safe_float(all_amount_raw, 0.0)
            tax_pct = self._safe_float(tax_raw, 0.0)
            if tax_raw.strip() and abs(tax_pct - int(tax_pct)) < 1e-9:
                tax_field: Any = int(tax_pct)
            elif tax_raw.strip():
                tax_field = tax_pct
            else:
                tax_field = ""
            tax_amount_raw = self._datynk_pick(payload, "taxAmount", "tax_amount")
            if tax_amount_raw.strip():
                tax_amount: Any = self._safe_float(tax_amount_raw, 0.0)
            elif amount_raw.strip() and tax_raw.strip():
                tax_amount = round(amount * (tax_pct / 100.0), 10)
            else:
                tax_amount = ""
            gift_raw = self._datynk_pick(payload, "gift", "line_gift").lower()
            gift = gift_raw in ("1", "true", "yes", "on")
            detail: Dict[str, Any] = {
                "materialCode": material,
                "productName": self._datynk_pick(payload, "productName", "product_name") or material,
                "productSpec": self._datynk_pick(payload, "productSpec", "product_spec"),
                "ph": self._datynk_pick(payload, "ph", "material_ph"),
                "customerMaterialNo": self._datynk_pick(payload, "customerMaterialNo", "customer_material_no"),
                "qty": qty if line_qty_raw.strip() else "",
                "price": price if price_raw.strip() else "",
                "taxPrice": tax_price if tax_price_raw.strip() else "",
                "amount": amount if amount_raw.strip() else "",
                "allAmount": all_amount if all_amount_raw.strip() else "",
                "tax": tax_field,
                "taxAmount": tax_amount,
                "gift": gift,
                "remark": self._datynk_pick(payload, "line_remark", "detail_remark"),
            }
            details = [detail]

        self._validate_datynk_sale_order_details(details)

        order: Dict[str, Any] = {
            "org": org,
            "customerName": customer,
            "customerPoNo": customer_po,
            "salesUser": sales_user,
            "createUser": sales_user,
            "orderDate": doc_date,
            "orderStatus": order_status,
            "deliveryAddr": delivery_addr,
            "rate": rate,
            "currency": currency,
            "deliveryDate": delivery_date,
        }
        body: Dict[str, Any] = {"order": order, "details": details}
        if source_attachment is not None:
            body["files"] = [
                {
                    "fileName": source_attachment.file_name,
                    "fileType": source_attachment.file_type,
                    "base64Content": base64.b64encode(source_attachment.content).decode("ascii"),
                }
            ]
        return body

    def _parse_datynk_create_response(self, body: Dict[str, Any]) -> Tuple[str, str]:
        if not self._datynk_http_ok(body.get("code")):
            msg = str(body.get("message") or body.get(self.error_message_field) or "datynk_error")
            ec = str(body.get(self.error_code_field) or body.get("code") or "ERP_UPSTREAM_ERROR")
            raise RealErpClient.ErpClientError(
                code=ec,
                message=msg,
                status_code=200,
                details={"body": body},
            )
        data = body.get("data")
        draft_no = ""
        draft_url = str(body.get(self.draft_url_field, "") or "").strip()
        if isinstance(data, dict):
            order = data.get("order") if isinstance(data.get("order"), dict) else {}
            draft_no = str(
                data.get("orderNo")
                or data.get("draftNo")
                or data.get(self.draft_no_field)
                or order.get("orderNo")
                or order.get("draftNo")
                or order.get(self.draft_no_field)
                or ""
            ).strip()
            if not draft_url:
                draft_url = str(
                    data.get(self.draft_url_field)
                    or data.get("draftUrl")
                    or order.get(self.draft_url_field)
                    or order.get("draftUrl")
                    or ""
                ).strip()
        elif data is not None:
            draft_no = str(data).strip()
        if self.allow_empty_draft_url and draft_no and not draft_url:
            draft_url = ""
        if not draft_no or (not draft_url and not self.allow_empty_draft_url):
            raise RealErpClient.ErpClientError(
                code="ERP_UPSTREAM_ERROR",
                message="Datynk 返回缺少 data（单号）或 draft_url",
                status_code=200,
                details={"response_fields": list(body.keys()) if isinstance(body, dict) else []},
            )
        return draft_no, draft_url

    def create_draft(
        self,
        doc_type: str,
        payload: Dict[str, str],
        idempotency_key: str,
        source_attachment: Optional[ErpSourceAttachment] = None,
    ) -> Tuple[str, str]:
        if self.create_body_style == "datynk_sale_order":
            _ = idempotency_key  # 对端当前接口未使用；仍由上层 store 幂等
            mapped = self._map_payload_fields(payload)
            sale_body = self._build_datynk_sale_order_body(doc_type, mapped, source_attachment)
            body = self._request_json(
                "POST",
                self.create_draft_path,
                sale_body,
                timeout_seconds=self.create_timeout_seconds,
            )
            return self._parse_datynk_create_response(body)

        if source_attachment is not None:
            raise RealErpClient.ErpClientError(
                code="ERP_SOURCE_FILE_UNSUPPORTED_BODY_STYLE",
                message="ERP 原始文件附件仅支持 datynk_sale_order 建单请求格式",
                status_code=0,
                details={"create_body_style": self.create_body_style},
            )

        body = self._request_json(
            "POST",
            self.create_draft_path,
            {
                self.req_type_field: doc_type,
                self.req_payload_field: self._map_payload_fields(payload),
                self.req_idempotency_field: idempotency_key,
            },
        )
        draft_no = str(body.get(self.draft_no_field, "")).strip()
        draft_url = str(body.get(self.draft_url_field, "")).strip()
        if self.allow_empty_draft_url and draft_no and not draft_url:
            draft_url = ""
        if not draft_no or (not draft_url and not self.allow_empty_draft_url):
            raise RealErpClient.ErpClientError(
                code="ERP_UPSTREAM_ERROR",
                message="ERP create_draft 返回缺少 draft_no/draft_url",
                status_code=200,
                details={"response_fields": list(body.keys()) if isinstance(body, dict) else []},
            )
        return draft_no, draft_url

    def _map_customer_payload(self, payload: Dict[str, str]) -> Dict[str, str]:
        if not self.customer_payload_map:
            return dict(payload)
        out: Dict[str, str] = {}
        for k, v in payload.items():
            to_key = self.customer_payload_map.get(k, k)
            out[to_key] = str(v) if v is not None else ""
        return out

    def _parse_customer_save_response(self, body: Dict[str, Any]) -> Tuple[str, str]:
        if not self._datynk_http_ok(body.get("code")):
            msg = str(body.get("message") or body.get(self.error_message_field) or "customer_save_error")
            ec = str(body.get(self.error_code_field) or body.get("code") or "ERP_UPSTREAM_ERROR")
            raise RealErpClient.ErpClientError(
                code=ec,
                message=msg,
                status_code=200,
                details={"body": body},
            )
        data = body.get("data")
        key = ""
        if isinstance(data, str):
            key = data.strip()
        elif isinstance(data, dict):
            for fk in ("customerNumber", "customerId", "id"):
                v = data.get(fk)
                if v is not None and str(v).strip():
                    key = str(v).strip()
                    break
        detail_url = str(body.get(self.draft_url_field, "") or "").strip()
        if self.allow_empty_customer_detail_url and key and not detail_url:
            detail_url = ""
        if not key:
            raise RealErpClient.ErpClientError(
                code="ERP_UPSTREAM_ERROR",
                message="客户保存返回缺少 data（客户主键）",
                status_code=200,
                details={"response_fields": list(body.keys()) if isinstance(body, dict) else []},
            )
        if not detail_url and not self.allow_empty_customer_detail_url:
            raise RealErpClient.ErpClientError(
                code="ERP_UPSTREAM_ERROR",
                message="客户保存返回缺少 detail_url",
                status_code=200,
                details={"response_fields": list(body.keys()) if isinstance(body, dict) else []},
            )
        return key, detail_url

    def save_customer(self, payload: Dict[str, str]) -> Tuple[str, str]:
        if not self.customer_save_enabled:
            raise RealErpClient.ErpClientError(
                code="ERP_CUSTOMER_SAVE_DISABLED",
                message="未启用客户保存：设置 ERP_CUSTOMER_SAVE_ENABLED=true",
                status_code=0,
                details={},
            )
        mapped = self._map_customer_payload(payload)
        wrap = self.customer_wrap_field
        path = self.customer_save_path if self.customer_save_path.startswith("/") else f"/{self.customer_save_path}"
        body = self._request_json("POST", path, {wrap: mapped})
        return self._parse_customer_save_response(body)


def _resolve_real_bases() -> Tuple[str, str]:
    """
    解析主数据宿主与写单宿主 URL。
    未单独配置时均回退到 ERP_BASE_URL；仅配一侧时另一侧回退到已解析值。
    """
    legacy = os.getenv("ERP_BASE_URL", "").strip()
    data_base = os.getenv("ERP_DATA_BASE_URL", "").strip() or legacy
    write_base = (
        os.getenv("ERP_WRITE_BASE_URL", "").strip()
        or os.getenv("ERP_TRANS_BASE_URL", "").strip()
        or legacy
    )
    if not write_base:
        write_base = data_base
    if not data_base:
        data_base = write_base
    return data_base, write_base


def _build_erp_client() -> ErpClientProtocol:
    mode = os.getenv("ERP_CLIENT_MODE", "mock").strip().lower()
    if mode != "real":
        logger.info("erp_client_mode=mock")
        return MockErpClient()

    data_base, write_base = _resolve_real_bases()
    if not data_base:
        logger.warning("erp_client_mode=real but no ERP_BASE_URL / ERP_DATA_BASE_URL / ERP_WRITE_BASE_URL, fallback=mock")
        return MockErpClient()

    token_default = os.getenv("ERP_API_TOKEN", "").strip()
    data_token = os.getenv("ERP_DATA_API_TOKEN", "").strip() or token_default
    write_token = os.getenv("ERP_WRITE_API_TOKEN", "").strip() or token_default
    timeout_raw = os.getenv("ERP_TIMEOUT_SECONDS", "10").strip()
    try:
        timeout_seconds = max(1, int(timeout_raw))
    except ValueError:
        timeout_seconds = 10

    read_client = RealErpClient(base_url=data_base, token=data_token, timeout_seconds=timeout_seconds)
    write_client = RealErpClient(base_url=write_base, token=write_token, timeout_seconds=timeout_seconds)
    same_host = data_base.rstrip("/") == write_base.rstrip("/") and data_token == write_token
    if same_host:
        logger.info("erp_client_mode=real single_host base_url=%s timeout_seconds=%s", data_base, timeout_seconds)
        return read_client
    logger.info(
        "erp_client_mode=real dual_host data_base=%s write_base=%s timeout_seconds=%s",
        data_base,
        write_base,
        timeout_seconds,
    )
    return CompositeDualErpClient(read_client, write_client)


erp_client: ErpClientProtocol = _build_erp_client()


def erp_adapter_health_payload() -> Dict[str, Any]:
    """供 GET /health：ERP 适配模式摘要（不含密钥）。"""

    mode = os.getenv("ERP_CLIENT_MODE", "mock").strip().lower() or "mock"
    auth = os.getenv("ERP_AUTH_MODE", "bearer").strip().lower() or "bearer"
    out: Dict[str, Any] = {
        "erp_client_mode": mode,
        "erp_sale_order_page_enabled": False,
        "erp_customer_page_enabled": False,
        "erp_create_body_style": "legacy",
        "erp_auth_mode": auth,
        "erp_customer_save_enabled": False,
        "erp_soft_fail_master_search": False,
        "erp_master_search_query_style": "legacy",
        "erp_master_search_datynk_envelope": False,
        "erp_data_base_netloc": "",
        "erp_vendors_search_path": "",
        "erp_materials_search_path": "",
        "erp_warehouses_search_path": "",
        "erp_tax_codes_search_path": "",
        "erp_customer_page_path": "",
        "erp_vendors_search_query_key": "",
        "erp_materials_search_query_key": "",
        "erp_warehouses_search_query_key": "",
        "erp_tax_codes_search_query_key": "",
        "erp_customer_page_keyword_param": "",
    }

    def _attach_master_read_endpoints(master: RealErpClient) -> None:
        """主数据问答实际调用的 GET 路径与查询键（便于与 Datynk 文档/浏览器抓包对照）。"""
        out["erp_data_base_netloc"] = (parse.urlparse(master.base_url).netloc or "").strip()
        out["erp_vendors_search_path"] = str(getattr(master, "vendors_search_path", "") or "")
        out["erp_materials_search_path"] = str(getattr(master, "materials_search_path", "") or "")
        out["erp_warehouses_search_path"] = str(getattr(master, "warehouses_search_path", "") or "")
        out["erp_tax_codes_search_path"] = str(getattr(master, "tax_codes_search_path", "") or "")
        out["erp_customer_page_path"] = str(getattr(master, "customer_page_path", "") or "")
        out["erp_vendors_search_query_key"] = str(getattr(master, "vendors_search_keyword_param", "") or "")
        out["erp_materials_search_query_key"] = str(getattr(master, "materials_search_keyword_param", "") or "")
        out["erp_warehouses_search_query_key"] = str(getattr(master, "warehouses_search_keyword_param", "") or "")
        out["erp_tax_codes_search_query_key"] = str(getattr(master, "tax_codes_search_keyword_param", "") or "")
        out["erp_customer_page_keyword_param"] = str(getattr(master, "customer_page_keyword_param", "") or "")

    c = erp_client
    if isinstance(c, RealErpClient):
        out["erp_sale_order_page_enabled"] = bool(getattr(c, "sale_order_page_enabled", False))
        out["erp_customer_page_enabled"] = bool(getattr(c, "customer_page_enabled", False))
        out["erp_create_body_style"] = str(getattr(c, "create_body_style", "legacy"))
        out["erp_customer_save_enabled"] = bool(getattr(c, "customer_save_enabled", False))
        out["erp_soft_fail_master_search"] = bool(getattr(c, "soft_fail_master_search", False))
        out["erp_master_search_query_style"] = str(getattr(c, "master_search_query_style", "legacy"))
        out["erp_master_search_datynk_envelope"] = bool(getattr(c, "master_search_datynk_envelope", False))
        _attach_master_read_endpoints(c)
    elif isinstance(c, CompositeDualErpClient):
        m = c._master
        w = c._transactional
        so_m = isinstance(m, RealErpClient) and bool(getattr(m, "sale_order_page_enabled", False))
        so_w = isinstance(w, RealErpClient) and bool(getattr(w, "sale_order_page_enabled", False))
        out["erp_sale_order_page_enabled"] = so_m or so_w
        cp_m = isinstance(m, RealErpClient) and bool(getattr(m, "customer_page_enabled", False))
        cp_w = isinstance(w, RealErpClient) and bool(getattr(w, "customer_page_enabled", False))
        out["erp_customer_page_enabled"] = cp_m or cp_w
        ce_m = isinstance(m, RealErpClient) and bool(getattr(m, "customer_save_enabled", False))
        ce_w = isinstance(w, RealErpClient) and bool(getattr(w, "customer_save_enabled", False))
        out["erp_customer_save_enabled"] = ce_m or ce_w
        sf_m = isinstance(m, RealErpClient) and bool(getattr(m, "soft_fail_master_search", False))
        sf_w = isinstance(w, RealErpClient) and bool(getattr(w, "soft_fail_master_search", False))
        out["erp_soft_fail_master_search"] = sf_m or sf_w
        env_m = isinstance(m, RealErpClient) and bool(getattr(m, "master_search_datynk_envelope", False))
        env_w = isinstance(w, RealErpClient) and bool(getattr(w, "master_search_datynk_envelope", False))
        out["erp_master_search_datynk_envelope"] = env_m or env_w
        mq_m = str(getattr(m, "master_search_query_style", "legacy")) if isinstance(m, RealErpClient) else "legacy"
        mq_w = str(getattr(w, "master_search_query_style", "legacy")) if isinstance(w, RealErpClient) else "legacy"
        if mq_m == mq_w:
            out["erp_master_search_query_style"] = mq_m
        else:
            out["erp_master_search_query_style"] = f"data={mq_m};write={mq_w}"
        ms = "legacy"
        ws = "legacy"
        if isinstance(m, RealErpClient):
            ms = str(getattr(m, "create_body_style", "legacy"))
        if isinstance(w, RealErpClient):
            ws = str(getattr(w, "create_body_style", "legacy"))
        if ms == ws:
            out["erp_create_body_style"] = ms
        else:
            out["erp_create_body_style"] = f"data={ms};write={ws}"
        if isinstance(m, RealErpClient):
            _attach_master_read_endpoints(m)
    else:
        out["erp_create_body_style"] = "mock"
        out["erp_customer_page_enabled"] = True
    return out


# 暴露统一异常类型给 store 层使用（避免直接引用内部类路径）。
ErpClientError = RealErpClient.ErpClientError

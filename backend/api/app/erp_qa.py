"""
ERP 事实问答（MVP）：仅通过 ERP 适配层查询接口拼装回答，禁止编造实时主数据。

后续可在此处接入 LangChain/LangGraph，但须保留「先工具、后解释」约束。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.erp_client import ErpClientProtocol, clear_last_upstream_meta, erp_adapter_health_payload
from app.erp_qa_reports import (
    format_report_disambiguation,
    format_report_section,
    load_report_definitions,
    matched_reports,
    resolve_query_templates,
)

logger = logging.getLogger("ai_erp_api")

_CALENDAR_PHRASES_RE = re.compile(r"(本月|这个月|当月|上月|上个月|今年|本年|去年)(\s*)")


def _erp_qa_timezone() -> str:
    return (os.getenv("ERP_QA_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai")


def _today_erp_qa() -> date:
    tz_name = _erp_qa_timezone()
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        # Python<3.9 无 zoneinfo，或精简环境缺 tzdata：退化为本机日历日。
        return date.today()


def _month_date_range(y: int, mo: int) -> Tuple[date, date]:
    first = date(y, mo, 1)
    if mo == 12:
        last = date(y, 12, 31)
    else:
        last = date(y, mo + 1, 1) - timedelta(days=1)
    return first, last


def _infer_sale_order_date_range(message: str) -> Optional[Tuple[date, date]]:
    """识别自然语言中的日历范围（闭区间），用于销单筛选。"""
    m = message.strip()
    if not m:
        return None
    today = _today_erp_qa()
    y, mo = today.year, today.month
    if any(p in m for p in ("本月", "这个月", "当月")):
        return _month_date_range(y, mo)
    if any(p in m for p in ("上月", "上个月")):
        if mo == 1:
            return _month_date_range(y - 1, 12)
        return _month_date_range(y, mo - 1)
    if any(p in m for p in ("今年", "本年")):
        return date(y, 1, 1), date(y, 12, 31)
    if "去年" in m:
        yy = y - 1
        return date(yy, 1, 1), date(yy, 12, 31)
    return None


def _strip_calendar_phrases(text: str) -> str:
    t = _CALENDAR_PHRASES_RE.sub("", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _parse_order_row_date(row: Dict[str, str]) -> Optional[date]:
    raw = (row.get("orderDate") or row.get("order_date") or "").strip()
    if not raw:
        return None
    head = raw[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def _filter_sale_orders_by_inclusive_range(
    rows: List[Dict[str, str]],
    start: date,
    end: date,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in rows:
        od = _parse_order_row_date(r)
        if od is None:
            continue
        if start <= od <= end:
            out.append(r)
    return out


def _erp_qa_master_data_empty_troubleshoot(tools_used: List[str], raw: Dict[str, Any]) -> str:
    """当 real 模式下主数据检索已调用但结果为空时，给出与配置/软失败相关的说明（非模型幻觉）。"""
    hp = erp_adapter_health_payload()
    if (hp.get("erp_client_mode") or "").strip().lower() != "real":
        return ""

    def ran(sub: str) -> bool:
        return any(sub in t for t in tools_used)

    def empty_key(k: str) -> bool:
        return k in raw and not (raw.get(k) or [])

    labels: List[str] = []
    if ran("search_vendors") and empty_key("vendors"):
        labels.append("供应商")
    if ran("search_materials") and empty_key("materials"):
        labels.append("物料")
    if ran("search_warehouses") and empty_key("warehouses"):
        labels.append("仓库")
    if ran("search_tax_codes") and empty_key("tax_codes"):
        labels.append("税码")
    if ran("search_customers") and empty_key("customers"):
        labels.append("客户分页")
    if not labels:
        return ""
    netloc = (hp.get("erp_data_base_netloc") or "").strip() or "（未解析到主机名，请检查 ERP_BASE_URL / ERP_DATA_BASE_URL）"
    sf = bool(hp.get("erp_soft_fail_master_search"))
    lines = [
        f"> **主数据无结果**（本次涉及：{'、'.join(labels)}）。这不是模型「编造不出来」，而是 **适配层拿到的列表为空**（路径/参数/权限/响应格式与对端不一致时都会出现）。",
        "> - 请在 **`GET /health`** 核对当前生效的 **`erp_vendors_search_path`**、**`erp_tax_codes_search_path`** 等是否与 Datynk 后台真实分页 URL 一致；模糊字段见 **`erp_*_search_query_key`**。",
        f"> - 主数据请求发往 **`{netloc}`**；需与浏览器能打开的后台同源，且 Token/Cookie 对该路径有读权限。",
    ]
    if sf:
        lines.append(
            "> - 若 **`erp_soft_fail_master_search`** 为 **true**：主数据 **404/405** 会被吞掉并显示为空列表，请查 API 日志或对上述 path 直接 `curl`/浏览器验证。",
        )
    lines.append(
        "> - 校准方式：在浏览器已登录后台的情况下，打开一条 **能出列表** 的供应商/税码分页请求，把完整 URL 与查询参数名抄到环境变量 `ERP_VENDORS_SEARCH_PATH`、`ERP_TAX_CODES_SEARCH_PATH`、`ERP_*_SEARCH_QUERY_KEY`（详见仓库 `backend/docs/runbook.md`）。",
    )
    return "\n".join(lines)


def _pick_search_keyword(message: str) -> str:
    """从用户问题中取用于主数据模糊查询的关键词。"""
    m = message.strip()
    if not m:
        return ""
    # 去掉常见问句外壳
    m = re.sub(r"^(请|帮我|麻烦)?(查一下|查一查|查询|搜索|找|查)\s*", "", m, flags=re.IGNORECASE)
    m = m.strip("？? \t")
    # 优先提取引号内片段
    quoted = re.search(r"[「\"']([^\"'」]{2,32})[\"'」]", message)
    if quoted:
        return quoted.group(1).strip()
    # 英文/数字连续段
    token = re.search(r"[\w\-]{2,40}", m)
    if token:
        return token.group(0).strip()
    return m[:40]


def _sale_order_query_keyword(message: str, keyword: str) -> str:
    """销售订单意图下，去掉句首类型词，把剩余部分当作 `customerName` 检索关键字。"""
    if not _wants_sale_orders(message):
        return keyword
    k = (keyword or "").strip()
    if not k:
        return keyword
    stripped = k
    for prefix in (
        "销售订单分页",
        "销售订单列表",
        "销售订单查询",
        "销售订单",
        "本月订单",
        "上月订单",
        "当月订单",
        "这个月订单",
        "上个月订单",
        "客户订单",
        "订单列表",
        "订单分页",
        "订单查询",
        "查一下订单",
        "查订单",
        "销单",
        "订单",
    ):
        while stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].lstrip(" ：:，,、")
    out = stripped.strip()
    return out if out else keyword


def _wants_vendors(message: str) -> bool:
    low = message.lower()
    return any(
        k in message
        for k in ("供应商", "厂商", "卖方", "vendor")
    ) or "vendor" in low


def _wants_materials(message: str) -> bool:
    low = message.lower()
    return any(k in message for k in ("物料", "材料", "商品", "料号", "品名")) or "material" in low


def _wants_warehouses(message: str) -> bool:
    low = message.lower()
    return any(k in message for k in ("仓库", "库房", "收货仓", "仓储")) or "warehouse" in low


def _wants_tax_codes(message: str) -> bool:
    low = message.lower()
    return any(k in message for k in ("税码", "税代码", "税率代码", "税种")) or "tax code" in low or "tax_code" in low


def _wants_sale_orders(message: str) -> bool:
    low = message.lower()
    keys = (
        "销售订单",
        "订单列表",
        "订单分页",
        "SO单",
        "销单",
        "查订单",
        "订单查询",
        "查一下订单",
        "客户订单",
        "销货",
        "开单",
        "出货单",
        "送货单",
        "发货单",
        "在途订单",
        "订单情况",
        "销售情况",
        "本月订单",
        "上月订单",
        "当月订单",
        "这个月订单",
        "上个月订单",
    )
    if any(k in message for k in keys) or ("sale order" in low) or ("sales order" in low) or ("order list" in low):
        return True
    # 「本月…订单」等未写「销售」二字：避免误走宽泛主数据；排除明显采购语境
    if "采购订单" not in message and "询价单" not in message:
        if re.search(r"(本月|当月|上月|这周|上周).{0,10}订单", message):
            return True
    return False


def _wants_customers(message: str) -> bool:
    """客户主数据分页（与「销售订单」区分：后者优先）。"""
    if _wants_sale_orders(message):
        return False
    low = message.lower()
    if any(
        k in message
        for k in (
            "往来单位",
            "客户资料",
            "客户列表",
            "客户分页",
            "客户信息",
            "客户主数据",
            "查客户",
        )
    ):
        return True
    if "customer" in low and any(x in low for x in ("list", "page", "search", "query")):
        return True
    if "客户" in message and any(k in message for k in ("查询", "搜索", "查找", "检索", "查一下")):
        return True
    return False


def _customer_query_keyword(message: str, keyword: str) -> str:
    """客户主数据意图下，去掉句首类型词，把剩余部分当作分页关键字（默认 customerName）。"""
    if not _wants_customers(message):
        return keyword
    k = (keyword or "").strip()
    if not k:
        return keyword
    stripped = k
    for prefix in (
        "客户列表",
        "客户分页",
        "客户查询",
        "客户信息",
        "客户主数据",
        "往来单位",
        "查客户",
        "客户",
    ):
        while stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].lstrip(" ：:，,、")
    out = stripped.strip()
    return out if out else keyword


def _skip_no_intent_sale_order_fallback(message: str) -> bool:
    """明显不是销单/客户维度的问题，避免误把库存、报销等泛问当成客户名去查。"""
    block = _erp_qa_non_master_question_blocklist()
    return any(b in message for b in block)


def _erp_qa_non_master_question_blocklist() -> Tuple[str, ...]:
    """库存/财报等：不做销单兜底，也不做「宽泛四类主数据」并行检索，减少误触。"""
    return (
        "库存",
        "入库",
        "报销",
        "考勤",
        "工资",
        "预算",
        "固定资产",
        "财报",
        "利润表",
        "资产负债表",
        "现金流",
        "采购申请",
        "询价",
        "招标",
    )


def _skip_broad_master_search(message: str) -> bool:
    """与销单兜底共用排除表：泛业务问题不并行扫主数据。"""
    return any(b in message for b in _erp_qa_non_master_question_blocklist())


def _wants_customer_write_help(message: str) -> bool:
    """是否询问「如何把客户写入 ERP」——仅返回 API/配置说明，不调用 save_customer（避免误创建）。"""
    m = message.strip()
    if not m:
        return False
    low = m.lower()
    if "integrations/erp/customer" in low:
        return True
    if any(k in m for k in ("新建客户", "创建客户", "保存客户", "客户保存", "录入客户", "客户写入")):
        return True
    if "客户" in m and any(k in m for k in ("怎么", "如何", "怎样", "接口", "API", "调用")):
        return True
    if "customer" in low and any(k in low for k in ("create", "save", "new ", "add ", "integrat")):
        return True
    return False


def _customer_write_help_markdown(org_id: str) -> str:
    return (
        f"（org_id=`{org_id}`）**新建/保存客户**不在本聊天里自动代写 ERP，请调用你们已部署的 API：\n\n"
        "- **`POST /integrations/erp/customer`**，JSON 体：`{{ \"org_id\": \"…\", \"fields\": {{ … }} }}`"
        "（`fields` 为扁平键值，会按 `ERP_CUSTOMER_PAYLOAD_FIELD_MAP` 映射后写入对方 `customer` 对象；未传 `org` 时服务端会把 `org_id` 填入 `fields.org`）。\n"
        "- 环境：`ERP_CLIENT_MODE=real`、`ERP_CUSTOMER_SAVE_ENABLED=true`，写域 `ERP_WRITE_BASE_URL` 等与销售订单写侧一致；可用 **`GET /health`** 看 `erp_customer_save_enabled`。\n"
        "- 本地验证：`python verify_async_flow.py --api-base … --integrations-save-customer @backend/scripts/integrations-save-customer.sample.json`（仓库根执行；解释器可用 `backend/api/.venv/Scripts/python.exe`）。\n"
        "- 打开 **`GET /`** 可查看常用相对链接（含客户保存路径）。\n"
        "- 直连对方烟测（不经本服务）：`backend/scripts/smoke-datynk-erp.ps1 -ProbeCustomerSave`（见 `backend/docs/runbook.md`）。\n"
    )


def answer_with_erp_tools(org_id: str, message: str, erp: ErpClientProtocol) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    返回 (answer_markdown, tools_used, raw_results_for_audit)。
    """
    clear_last_upstream_meta()
    tools_used: List[str] = []
    raw: Dict[str, Any] = {}
    keyword = _pick_search_keyword(message)

    vendors: List[Dict[str, str]] = []
    materials: List[Dict[str, str]] = []
    warehouses: List[Dict[str, str]] = []
    tax_codes: List[Dict[str, str]] = []

    want_v = _wants_vendors(message)
    want_m = _wants_materials(message)
    want_w = _wants_warehouses(message)
    want_t = _wants_tax_codes(message)
    want_so = _wants_sale_orders(message)
    want_c = _wants_customers(message)
    # 无明确类型词时并行查四类主数据；默认开启以提高命中率。可用 ERP_QA_BROAD_MASTER_SEARCH=false 关闭。
    _broad_raw = os.getenv("ERP_QA_BROAD_MASTER_SEARCH", "true").strip().lower()
    broad_on = _broad_raw not in ("0", "false", "no", "off")
    cust_page_on = bool(getattr(erp, "customer_page_enabled", False))
    broad = (
        broad_on
        and bool(keyword)
        and not (want_v or want_m or want_w or want_t or want_so or want_c)
        and not _skip_broad_master_search(message)
    )

    if _wants_customer_write_help(message) and not (want_v or want_m or want_w or want_t or want_so or want_c):
        tools_used = ["(no_tool: customer_save_api_docs)"]
        return (
            _customer_write_help_markdown(org_id),
            tools_used,
            {},
        )

    rep_defs = load_report_definitions()
    matched_rep = matched_reports(rep_defs, message)
    if len(matched_rep) > 1:
        labels = [str(x.get("label") or x.get("id")) for x in matched_rep]
        tools_used = ["(no_tool: report_definitions_ambiguous)"]
        return (format_report_disambiguation(labels), tools_used, {})

    sale_orders: List[Dict[str, str]] = []
    if want_so:
        so_rng = _infer_sale_order_date_range(message)
        so_kw = _sale_order_query_keyword(message, keyword.strip())
        so_kw = _strip_calendar_phrases(so_kw)
        try:
            so_ps = int(os.getenv("ERP_SALE_ORDER_PAGE_SIZE", "20").strip())
        except ValueError:
            so_ps = 20
        so_ps = max(1, min(100, so_ps))
        if so_rng:
            try:
                cap = int(os.getenv("ERP_SALE_ORDER_MONTH_QUERY_PAGE_CAP", "100").strip())
            except ValueError:
                cap = 100
            cap = max(so_ps, min(100, cap))
            so_ps = cap
        ob, oe = "", ""
        if so_rng:
            ob, oe = so_rng[0].isoformat(), so_rng[1].isoformat()
        sale_orders = erp.search_sale_orders(
            org_id,
            so_kw,
            1,
            so_ps,
            order_date_begin=ob,
            order_date_end=oe,
        )
        pre_filter = len(sale_orders)
        if so_rng:
            sale_orders = _filter_sale_orders_by_inclusive_range(sale_orders, so_rng[0], so_rng[1])
            raw["sale_orders_date_filter"] = {
                "start": ob,
                "end": oe,
                "rows_before_filter": pre_filter,
                "rows_after_filter": len(sale_orders),
            }
        tool_bits = [
            f"search_sale_orders(org={org_id!r}, customerName={so_kw!r}, pageSize={so_ps})",
        ]
        if ob and oe:
            tool_bits.append(f"orderDateRange={ob!r}..{oe!r}")
            if pre_filter != len(sale_orders):
                tool_bits.append(f"client_date_filter={pre_filter}->{len(sale_orders)}")
        tools_used.append(", ".join(tool_bits))
        raw["sale_orders"] = sale_orders
        logger.info(
            "erp_qa_search_sale_orders org_id=%s keyword=%s count=%s date_range=%s",
            org_id,
            so_kw,
            len(sale_orders),
            (ob, oe) if ob else None,
        )

    customers: List[Dict[str, str]] = []
    if want_c or (broad and cust_page_on):
        c_kw = _customer_query_keyword(message, keyword.strip()) if want_c else keyword.strip()
        try:
            c_ps = int(os.getenv("ERP_CUSTOMER_PAGE_SIZE", os.getenv("ERP_SALE_ORDER_PAGE_SIZE", "20")).strip())
        except ValueError:
            c_ps = 20
        c_ps = max(1, min(100, c_ps))
        customers = erp.search_customers(org_id, c_kw, 1, c_ps)
        tools_used.append(f"search_customers(org={org_id!r}, keyword={c_kw!r}, pageSize={c_ps})")
        raw["customers"] = customers
        logger.info("erp_qa_search_customers org_id=%s keyword=%s count=%s", org_id, c_kw, len(customers))

    if want_v or broad:
        q = keyword or "vendor"
        vendors = erp.search_vendors(org_id, q)
        tools_used.append(f"search_vendors(keyword={q!r})")
        raw["vendors"] = vendors
        logger.info("erp_qa_search_vendors org_id=%s keyword=%s count=%s", org_id, q, len(vendors))

    if want_m or broad:
        q = keyword or "material"
        materials = erp.search_materials(org_id, q)
        tools_used.append(f"search_materials(keyword={q!r})")
        raw["materials"] = materials
        logger.info("erp_qa_search_materials org_id=%s keyword=%s count=%s", org_id, q, len(materials))

    if want_w or broad:
        q = keyword or "warehouse"
        warehouses = erp.search_warehouses(org_id, q)
        tools_used.append(f"search_warehouses(keyword={q!r})")
        raw["warehouses"] = warehouses
        logger.info("erp_qa_search_warehouses org_id=%s keyword=%s count=%s", org_id, q, len(warehouses))

    if want_t or broad:
        q = keyword or "tax"
        tax_codes = erp.search_tax_codes(org_id, q)
        tools_used.append(f"search_tax_codes(keyword={q!r})")
        raw["tax_codes"] = tax_codes
        logger.info("erp_qa_search_tax_codes org_id=%s keyword=%s count=%s", org_id, q, len(tax_codes))

    if len(matched_rep) == 1:
        r0 = matched_rep[0]
        http = r0.get("http") or {}
        path = str(http.get("path") or "").strip()
        qtpl = http.get("query") if isinstance(http.get("query"), dict) else {}
        resp = r0.get("response") or {}
        if isinstance(resp, dict):
            env_on = bool(resp.get("datynk_envelope", True))
            rp = str(resp.get("records_path") or "data.records").strip() or "data.records"
        else:
            env_on, rp = True, "data.records"
        rid = str(r0.get("id") or "report")
        qflat = resolve_query_templates(qtpl, org_id, keyword.strip())
        rows = erp.fetch_config_read(path, qflat, expect_datynk_envelope=env_on, records_path=rp)
        tools_used.append(f"fetch_config_read(report={rid!r}, path={path!r})")
        raw.setdefault("_report_blocks", []).append({"id": rid, "label": str(r0.get("label") or rid), "rows": rows})
        logger.info("erp_qa_fetch_config_read report_id=%s path=%s count=%s", rid, path, len(rows))

    # 未命中任何意图词时，可选：按「客户名称关键字」走销售订单分页（Datynk 常见）；可用 ERP_QA_FALLBACK_WHEN_NO_INTENT=false 关闭。
    _fb = os.getenv("ERP_QA_FALLBACK_WHEN_NO_INTENT", "true").strip().lower() in ("1", "true", "yes", "on")
    if _fb and not tools_used and keyword and len(keyword.strip()) >= 2 and not _skip_no_intent_sale_order_fallback(message):
        kw0 = keyword.strip()
        if not re.match(r"^(什么|怎么|如何|为什么|能否|是否|可以|有没有|哪个|你们|系统|这个|那个)", kw0):
            try:
                so_ps = int(os.getenv("ERP_SALE_ORDER_PAGE_SIZE", "20").strip())
            except ValueError:
                so_ps = 20
            so_ps = max(1, min(100, so_ps))
            sale_orders = erp.search_sale_orders(org_id, kw0, 1, so_ps)
            tools_used.append(
                f"search_sale_orders(fallback_no_intent, org={org_id!r}, customerName={kw0!r}, pageSize={so_ps})"
            )
            raw["sale_orders"] = sale_orders
            raw["erp_qa_fallback"] = "no_intent_sale_orders"
            logger.info("erp_qa_search_sale_orders_fallback org_id=%s keyword=%s count=%s", org_id, kw0, len(sale_orders))

    if not tools_used:
        tools_used.append("(no_tool: question_empty)")
        return (
            "请先描述要查的对象（例如：「查供应商 vendor」「物料 M001」「仓库 WH01」「税码 J1」「查客户 某某」「销售订单 某某客户」）。\n"
            "**自定义报表**：管理员可复制 `backend/config/erp_qa_reports.example.json` 为 `backend/config/erp_qa_reports.json`，按关键词绑定只读 GET；详见 `GET /health` 的 `erp_qa_report_definitions_count`。\n"
            "如需**把新客户写入 ERP**，请调 `POST /integrations/erp/customer`（本聊天不自动建客户）；配置见 `GET /health` 的 `erp_customer_save_enabled`。\n"
            "说明：本助手回答主数据问题时只会展示 **ERP 查询接口** 返回的结果；"
            "未写类型词时默认会并行检索供应商/物料/仓库/税码，并在启用客户分页时一并检索客户（可用 `ERP_QA_BROAD_MASTER_SEARCH=false` 关闭宽泛检索）。",
            tools_used,
            raw,
        )

    lines: List[str] = [
        f"（org_id=`{org_id}`）以下为 **ERP 实时查询** 返回的数据摘要（未命中则列表为空）：",
        "",
    ]
    if raw.get("erp_qa_fallback") == "no_intent_sale_orders":
        lines.append(
            "> **提示**：未在问题中识别到「供应商 / 物料 / 仓库 / 税码 / 客户 / 销售订单」等类型词，"
            "已按 **销售订单分页**（`customerName` 关键字）尝试检索；若无结果请补充类型词，或让管理员配置真实 ERP / 关闭兜底（`ERP_QA_FALLBACK_WHEN_NO_INTENT=false`）。"
        )
        lines.append("")
    sdf = raw.get("sale_orders_date_filter")
    if isinstance(sdf, dict) and sdf.get("start") and sdf.get("end"):
        lines.append(
            f"> **销单日期**：已按问题识别为 **`{sdf['start']}`～`{sdf['end']}`**；"
            "若已在环境变量中配置 `ERP_SALE_ORDER_PAGE_DATE_BEGIN_PARAM` / `ERP_SALE_ORDER_PAGE_DATE_END_PARAM`，会一并传给分页接口；"
            "并对返回行按 `orderDate` 做本地过滤（仅保留能解析出日期的行）。"
        )
        lines.append("")
    if "sale_orders" in raw:
        lines.append("**销售订单（分页）**")
        rows = raw.get("sale_orders") or []
        if not rows:
            lines.append("- （无匹配或未启用销售订单查询接口）")
        else:
            for r in rows[:20]:
                ono = r.get("orderNo", r.get("order_no", ""))
                cname = r.get("customerName", r.get("customer_name", ""))
                st = r.get("orderStatus", r.get("order_status", ""))
                od = r.get("orderDate", r.get("order_date", ""))
                amt = r.get("totalAllAmount", r.get("total_all_amount", ""))
                lines.append(f"- `{ono}` {cname} | 状态 {st} | 日期 {od} | 含税合计 {amt}")
        lines.append("")
    if "vendors" in raw:
        lines.append("**供应商**")
        if not vendors:
            lines.append("- （无匹配）")
        else:
            for v in vendors[:20]:
                lines.append(f"- `{v.get('vendor_code', '')}` {v.get('vendor_name', '')}")
        lines.append("")
    if "materials" in raw:
        lines.append("**物料**")
        if not materials:
            lines.append("- （无匹配）")
        else:
            for m in materials[:20]:
                lines.append(f"- `{m.get('material_code', '')}` {m.get('material_name', '')}")
        lines.append("")
    if "warehouses" in raw:
        lines.append("**仓库**")
        if not warehouses:
            lines.append("- （无匹配）")
        else:
            for w in warehouses[:20]:
                lines.append(f"- `{w.get('warehouse_code', '')}` {w.get('warehouse_name', '')}")
        lines.append("")
    if "tax_codes" in raw:
        lines.append("**税码**")
        if not tax_codes:
            lines.append("- （无匹配）")
        else:
            for t in tax_codes[:20]:
                lines.append(f"- `{t.get('tax_code', '')}` {t.get('tax_name', '')}")
        lines.append("")
    if "customers" in raw:
        lines.append("**客户（分页）**")
        rows = raw.get("customers") or []
        if not rows:
            lines.append("- （无匹配或未启用客户分页接口，见 `GET /health` 的 `erp_customer_page_enabled`）")
        else:
            for r in rows[:20]:
                cno = r.get("customerNumber", r.get("customer_no", r.get("customer_number", "")))
                cname = r.get("customerName", r.get("customer_name", ""))
                lines.append(f"- `{cno}` {cname}")
        lines.append("")
    for blk in raw.get("_report_blocks") or []:
        if not isinstance(blk, dict):
            continue
        label = str(blk.get("label") or blk.get("id") or "报表")
        rows_obj = blk.get("rows")
        rows = rows_obj if isinstance(rows_obj, list) else []
        lines.extend(format_report_section(label, rows))
    ts = _erp_qa_master_data_empty_troubleshoot(tools_used, raw)
    if ts:
        lines.append("")
        lines.append(ts)
    lines.append("— 以上条目均来自 ERP 适配层查询结果，未使用外部知识库作为事实源。")

    return "\n".join(lines).strip(), tools_used, raw

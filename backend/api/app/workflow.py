"""
ingestion 处理工作流骨架。

设计目的：
- 把状态推进逻辑从 store 层抽离，避免业务逻辑分散；
- 先提供可运行的线性流程，后续可以平滑替换为 LangGraph 图式编排；
- 保持当前 API 契约不变，降低迭代风险。
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import perf_counter, sleep
from typing import Callable, Dict, List, Literal, Tuple, TypedDict

try:
    from langgraph.graph import END, StateGraph
except Exception:  # pragma: no cover
    END = None
    StateGraph = None

from app.document_extract import (
    classify_doc_type_from_name,
    classify_doc_type_from_text,
    extract_text_from_bytes,
    heuristic_fill_fields,
    heuristic_vendor_code,
    mapping_search_snippet,
    resolved_upload_file_name,
    truncate_for_api,
)
from app.erp_audit_log import append_erp_call_log_with_upstream
from app.erp_client import ErpClientError, ErpClientProtocol, clear_last_upstream_meta
from app.schemas import DocType, ErrorCode, IngestionResponse, IngestionStatus
from app.extraction_profile import apply_field_aliases, get_profile, refresh_ingestion_required_keys
from app.llm_extract import try_apply_llm_preview
from app.order_preview import apply_customer_material_mapping, apply_preview_to_ingestion, build_order_preview_data
from app.structured_extract import (
    extract_po_cn_layout_entities,
    extract_structured_fields,
)
from app.storage_client import get_object_bytes

logger = logging.getLogger("ai_erp_api")

# 由调用方注入“写状态+记审计”的实现，保证存储层语义统一。
AppendEventFn = Callable[[IngestionResponse, IngestionStatus, str], None]


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _should_force_datynk_sale_order_doc_type() -> bool:
    return (
        os.getenv("ERP_CREATE_BODY_STYLE", "").strip().lower() == "datynk_sale_order"
        and _env_truthy("ERP_DATYNK_SALE_ORDER_FORCE_DOC_TYPE", True)
    )


def _should_require_purchase_order_evidence() -> bool:
    raw = os.getenv("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE")
    if raw is not None:
        return _env_truthy("WORKFLOW_REQUIRE_PURCHASE_ORDER_EVIDENCE", True)
    return _should_force_datynk_sale_order_doc_type()


def _force_datynk_sale_order_doc_type(ing: IngestionResponse) -> bool:
    if not _should_force_datynk_sale_order_doc_type():
        return False
    if ing.doc_type_hint == DocType.PO:
        return False
    ing.doc_type_hint = DocType.PO
    return True


class WorkflowState(TypedDict):
    ingestion: IngestionResponse
    erp: ErpClientProtocol
    append_event: AppendEventFn
    mapping_metrics: Dict[str, int]
    # 解析后的全文（仅内存传递，不落库；预览见 ingestion.extract_preview）
    document_text: str


class NodeExecutionError(Exception):
    """节点执行异常，携带节点名用于精确审计。"""

    def __init__(self, node_name: str, reason: str, failure_type: Literal["node", "retry_exhausted", "timeout"] = "node"):
        super().__init__(f"{node_name}: {reason}")
        self.node_name = node_name
        self.reason = reason
        self.failure_type = failure_type


def _map_erp_error_for_workflow(err_code: str) -> str:
    """与 store._map_erp_error_code 对齐，避免 workflow 在自动校验失败时无法落稳定错误码。"""
    code = (err_code or "").upper()
    if code in {"MASTER_DATA_NOT_FOUND", "ERP_MASTER_DATA_NOT_FOUND"}:
        return ErrorCode.ERP_MASTER_DATA_NOT_FOUND.value
    if code in {"PERMISSION_DENIED", "ERP_PERMISSION_DENIED", "FORBIDDEN"}:
        return ErrorCode.ERP_PERMISSION_DENIED.value
    if code in {"UPSTREAM_TIMEOUT", "ERP_UPSTREAM_TIMEOUT", "TIMEOUT"}:
        return ErrorCode.ERP_UPSTREAM_TIMEOUT.value
    return ErrorCode.ERP_UPSTREAM_ERROR.value


def _resolve_workflow_error_code(exc: NodeExecutionError) -> str:
    # 细粒度错误码映射：优先返回“节点 + 场景”级别，便于前端与告警分流。
    if exc.reason.startswith("unsupported_document"):
        return ErrorCode.UNSUPPORTED_DOCUMENT.value
    if exc.failure_type == "timeout":
        timeout_map = {
            "parse": ErrorCode.WORKFLOW_PARSE_RETRY_TIMEOUT.value,
            "extract": ErrorCode.WORKFLOW_EXTRACT_RETRY_TIMEOUT.value,
            "map": ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value,
        }
        return timeout_map.get(exc.node_name, ErrorCode.WORKFLOW_RETRY_TIMEOUT.value)
    if exc.failure_type == "retry_exhausted":
        exhausted_map = {
            "parse": ErrorCode.WORKFLOW_PARSE_RETRY_EXHAUSTED.value,
            "extract": ErrorCode.WORKFLOW_EXTRACT_RETRY_EXHAUSTED.value,
            "map": ErrorCode.WORKFLOW_MAP_RETRY_EXHAUSTED.value,
        }
        return exhausted_map.get(exc.node_name, ErrorCode.WORKFLOW_RETRY_EXHAUSTED.value)
    return ErrorCode.WORKFLOW_NODE_FAILED.value


def _purchase_order_evidence(ing: IngestionResponse, text: str) -> Tuple[bool, str]:
    if not _should_require_purchase_order_evidence():
        return True, "disabled"
    name = resolved_upload_file_name(ing.source_file_object_key, ing.source_file_name)
    name_guess = classify_doc_type_from_name(name)
    text_guess = classify_doc_type_from_text(text)
    if name_guess in {"INV", "GR"}:
        return False, f"name_classified_as_{name_guess}"
    if text_guess in {"INV", "GR"}:
        return False, f"text_classified_as_{text_guess}"
    if name_guess == "PO":
        return True, "name_classified_as_PO"
    if text_guess == "PO":
        return True, "text_classified_as_PO"

    corpus = f"{name}\n{text}".lower()
    strong_phrases = (
        "purchase order",
        "purchase order no",
        "purchase order number",
        "po number",
        "supplier po",
        "standard purchase order",
        "采购订单",
        "标准采购订单",
        "客户采购订单",
        "采购订单号",
    )
    if any(phrase in corpus for phrase in strong_phrases):
        return True, "strong_purchase_order_phrase"

    weak_signals = (
        "srm",
        "sap",
        "supplier",
        "vendor",
        "material",
        "material code",
        "quantity",
        "delivery date",
        "plant",
        "采购",
        "供应商",
        "物料",
        "数量",
        "交货",
        "工厂",
        "采购组织",
        "订单日期",
    )
    weak_count = sum(1 for signal in weak_signals if signal in corpus)
    has_line_signal = any(signal in corpus for signal in ("material", "material code", "物料"))
    has_quantity_signal = any(signal in corpus for signal in ("quantity", "qty", "数量"))
    if weak_count >= 4 and has_line_signal and has_quantity_signal:
        return True, f"weak_purchase_order_signals={weak_count}"
    return False, f"insufficient_purchase_order_evidence name_guess={name_guess or 'none'} text_guess={text_guess or 'none'} weak={weak_count}"


def _node_retry_config(node_name: str, default_max_retries: int = 0, default_backoff_ms: int = 0) -> Dict[str, int]:
    """
    获取节点重试配置，优先读取节点级环境变量，其次读取通用默认值。

    示例（node_name=map）：
    - WORKFLOW_MAP_MAX_RETRIES
    - WORKFLOW_MAP_RETRY_BACKOFF_MS
    - WORKFLOW_MAP_MAX_ELAPSED_MS
    """
    prefix = f"WORKFLOW_{node_name.upper()}"
    max_retries_raw = os.getenv(f"{prefix}_MAX_RETRIES", str(default_max_retries)).strip()
    backoff_ms_raw = os.getenv(f"{prefix}_RETRY_BACKOFF_MS", str(default_backoff_ms)).strip()
    max_elapsed_ms_raw = os.getenv(f"{prefix}_MAX_ELAPSED_MS", "0").strip()
    try:
        max_retries = max(0, int(max_retries_raw))
    except ValueError:
        max_retries = default_max_retries
    try:
        backoff_ms = max(0, int(backoff_ms_raw))
    except ValueError:
        backoff_ms = default_backoff_ms
    try:
        max_elapsed_ms = max(0, int(max_elapsed_ms_raw))
    except ValueError:
        max_elapsed_ms = 0
    return {"max_retries": max_retries, "backoff_ms": backoff_ms, "max_elapsed_ms": max_elapsed_ms}


def _run_node(
    ingestion: IngestionResponse,
    node_name: str,
    fn: Callable[[], Dict[str, int]],
) -> Dict[str, int]:
    """
    执行单个节点并记录统一日志。

    约定：
    - 节点函数返回轻量指标字典（如候选数），用于结构化日志；
    - 日志默认包含 ingestion_id、node、耗时、状态。
    """
    start = perf_counter()
    logger.info("workflow_node_start ingestion_id=%s node=%s", ingestion.ingestion_id, node_name)
    try:
        metrics = fn()
    except NodeExecutionError:
        raise
    except Exception as exc:
        elapsed_ms = int((perf_counter() - start) * 1000)
        logger.exception(
            "workflow_node_failed ingestion_id=%s node=%s elapsed_ms=%s err=%s",
            ingestion.ingestion_id,
            node_name,
            elapsed_ms,
            str(exc),
        )
        raise NodeExecutionError(node_name=node_name, reason=str(exc), failure_type="node") from exc
    elapsed_ms = int((perf_counter() - start) * 1000)
    logger.info(
        "workflow_node_end ingestion_id=%s node=%s elapsed_ms=%s status=%s metrics=%s",
        ingestion.ingestion_id,
        node_name,
        elapsed_ms,
        ingestion.status,
        metrics,
    )
    return metrics


def _node_classify(state: WorkflowState) -> WorkflowState:
    def _classify_impl() -> Dict[str, int]:
        ing = state["ingestion"]
        name = resolved_upload_file_name(ing.source_file_object_key, ing.source_file_name)
        guessed = classify_doc_type_from_name(name)
        if guessed and ing.doc_type_hint is None:
            ing.doc_type_hint = DocType(guessed)
        forced = _force_datynk_sale_order_doc_type(ing)
        msg = "document classified to business type"
        if ing.doc_type_hint:
            msg = f"document classified to business type hint={ing.doc_type_hint.value} file={name!r}"
        if forced:
            msg += " forced=datynk_sale_order"
        state["append_event"](ing, IngestionStatus.CLASSIFIED, msg)
        return {"doc_type_hint": 1 if ing.doc_type_hint else 0, "forced_sale_order": int(forced)}

    _run_node(state["ingestion"], "classify", _classify_impl)
    return state


def _node_parse(state: WorkflowState) -> WorkflowState:
    def _parse_impl() -> Dict[str, int]:
        started = perf_counter()
        ing = state["ingestion"]
        name = resolved_upload_file_name(ing.source_file_object_key, ing.source_file_name)
        raw = get_object_bytes(ing.source_file_object_key)
        if not raw:
            state["document_text"] = ""
            ing.parsed_char_count = 0
            ing.extract_preview = None
            ing.parse_format_label = "parse_skipped_no_bytes"
            state["append_event"](
                ing,
                IngestionStatus.PARSED,
                "parse outcome=skipped_no_bytes format=parse_skipped_no_bytes chars=0 "
                "(no object bytes: configure MinIO, LOCAL_OBJECT_STORAGE_DIR fallback, or re-upload)",
            )
            return {"chars": 0, "skipped": 1}

        text, fmt = extract_text_from_bytes(raw, name)
        elapsed_ms = int((perf_counter() - started) * 1000)
        state["document_text"] = text
        ing.parsed_char_count = len(text)
        ing.extract_preview = truncate_for_api(text) if text else None
        ing.parse_format_label = fmt
        head = (text[:160] if text else "").replace("\n", " ")
        if len(text) > 0:
            outcome = "ok"
        elif fmt.startswith("unsupported"):
            outcome = "unsupported"
        elif fmt in ("empty",):
            outcome = "empty_input"
        else:
            outcome = "no_text"
        state["append_event"](
            ing,
            IngestionStatus.PARSED,
            f"parse outcome={outcome} format={fmt} chars={len(text)} elapsed_ms={elapsed_ms} head={head!r}",
        )
        return {"chars": len(text), "format": fmt, "elapsed_ms": elapsed_ms}

    _run_node_with_retry(state=state, node_name="parse", fn=_parse_impl, default_max_retries=1, default_backoff_ms=100)
    return state


def _node_extract(state: WorkflowState) -> WorkflowState:
    def _extract_impl() -> Dict[str, int]:
        ing = state["ingestion"]
        text = state["document_text"] or ""
        ok, evidence_reason = _purchase_order_evidence(ing, text)
        if not ok:
            ing.error_details = {
                "category": "unsupported_document",
                "reason": evidence_reason,
            }
            raise NodeExecutionError(
                node_name="extract",
                reason=f"unsupported_document {evidence_reason}",
                failure_type="node",
            )
        if ing.doc_type_hint is None:
            gt = classify_doc_type_from_text(text)
            if gt:
                ing.doc_type_hint = DocType(gt)
        _force_datynk_sale_order_doc_type(ing)
        dt_val = ing.doc_type_hint.value if ing.doc_type_hint else None
        prof = get_profile(ing.extraction_profile_id)
        hints = heuristic_fill_fields(text)
        hints.update(heuristic_vendor_code(text))
        hints.update(extract_structured_fields(text, dt_val, prof))
        if dt_val == "PO":
            hints.update(extract_po_cn_layout_entities(text))
        apply_field_aliases(hints, prof)
        for k, v in hints.items():
            if v and not (ing.resolved_fields.get(k) or "").strip():
                ing.resolved_fields[k] = v
        lj = (ing.resolved_fields.get("line_items_json") or "").strip()
        if lj and dt_val == "PO":
            try:
                items = json.loads(lj)
                if isinstance(items, list) and items:
                    first = items[0]
                    ic = (first.get("inventory_code") or "").strip()
                    q = (first.get("quantity") or "").strip()
                    if ic and not (ing.resolved_fields.get("material_code") or "").strip():
                        ing.resolved_fields["material_code"] = ic
                    if q and not (ing.resolved_fields.get("line_qty") or "").strip():
                        ing.resolved_fields["line_qty"] = q
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        refresh_ingestion_required_keys(ing)
        required = ing.required_resolve_keys
        ing.missing_fields = [k for k in required if not (ing.resolved_fields.get(k) or "").strip()]
        llm_applied = try_apply_llm_preview(ing, text)
        if llm_applied:
            refresh_ingestion_required_keys(ing)
        state["append_event"](
            ing,
            IngestionStatus.EXTRACTED,
            f"structured fields extracted missing_count={len(ing.missing_fields)} doc_type_hint="
            f"{ing.doc_type_hint.value if ing.doc_type_hint else 'none'} llm_preview={int(llm_applied)}",
        )
        return {"missing": len(ing.missing_fields), "llm_preview": int(llm_applied)}

    _run_node_with_retry(
        state=state,
        node_name="extract",
        fn=_extract_impl,
        default_max_retries=1,
        default_backoff_ms=100,
    )
    return state


def _run_node_with_retry(
    state: WorkflowState,
    node_name: str,
    fn: Callable[[], Dict[str, int]],
    default_max_retries: int = 0,
    default_backoff_ms: int = 0,
) -> Dict[str, int]:
    cfg = _node_retry_config(
        node_name=node_name,
        default_max_retries=default_max_retries,
        default_backoff_ms=default_backoff_ms,
    )
    max_retries = int(cfg["max_retries"])
    backoff_ms = int(cfg["backoff_ms"])
    max_elapsed_ms = int(cfg["max_elapsed_ms"])
    attempt = 0
    started = perf_counter()
    while True:
        attempt += 1
        try:
            metrics = _run_node(state["ingestion"], node_name, fn)
            metrics["attempt"] = attempt
            return metrics
        except NodeExecutionError as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            if "unsupported_document" in exc.reason:
                raise exc
            if max_elapsed_ms > 0 and elapsed_ms >= max_elapsed_ms:
                logger.error(
                    "workflow_node_timeout ingestion_id=%s node=%s elapsed_ms=%s max_elapsed_ms=%s",
                    state["ingestion"].ingestion_id,
                    node_name,
                    elapsed_ms,
                    max_elapsed_ms,
                )
                raise NodeExecutionError(
                    node_name=node_name,
                    reason=f"retry timeout exceeded elapsed_ms={elapsed_ms} max_elapsed_ms={max_elapsed_ms}",
                    failure_type="timeout",
                ) from exc
            if attempt > max_retries:
                raise NodeExecutionError(
                    node_name=node_name,
                    reason=f"retry exhausted attempts={attempt} max_retries={max_retries}",
                    failure_type="retry_exhausted",
                ) from exc
            state["append_event"](
                state["ingestion"],
                state["ingestion"].status,
                f"{node_name} node retry scheduled attempt={attempt} max_retries={max_retries} elapsed_ms={elapsed_ms} reason={exc.reason}",
            )
            logger.warning(
                "workflow_node_retry ingestion_id=%s node=%s attempt=%s max_retries=%s backoff_ms=%s elapsed_ms=%s reason=%s",
                state["ingestion"].ingestion_id,
                node_name,
                attempt,
                max_retries,
                backoff_ms,
                elapsed_ms,
                exc.reason,
            )
            if backoff_ms > 0:
                sleep(backoff_ms / 1000)


def _node_map(state: WorkflowState) -> WorkflowState:
    def _map_impl() -> Dict[str, int]:
        ing = state["ingestion"]
        snippet = mapping_search_snippet(state["document_text"])
        v_kw = snippet if snippet else "vendor"
        m_kw = snippet if snippet else "material"
        w_kw = snippet if snippet else "warehouse"
        t_kw = snippet if snippet else "tax"
        erp = state["erp"]

        def _safe_list(name: str, fn, *args: object) -> Tuple[str, List[Dict[str, str]]]:
            try:
                out = fn(*args)
                return name, list(out) if out else []
            except Exception as exc:
                # 与 ErpClientError 鸭子类型兼容（避免测试 importlib.reload 后出现类身份不一致）
                if getattr(exc, "code", None) is None:
                    raise
                logger.warning(
                    "workflow_map_erp_search_failed ingestion_id=%s fn=%s code=%s status=%s",
                    ing.ingestion_id,
                    getattr(fn, "__name__", "search"),
                    getattr(exc, "code", ""),
                    getattr(exc, "status_code", 0),
                )
                return name, []

        calls = {
            "vendor": (erp.search_vendors, (ing.org_id, v_kw)),
            "material": (erp.search_materials, (ing.org_id, m_kw)),
            "warehouse": (erp.search_warehouses, (ing.org_id, w_kw)),
            "tax_code": (erp.search_tax_codes, (ing.org_id, t_kw)),
        }
        results: Dict[str, List[Dict[str, str]]] = {name: [] for name in calls}
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="erp-map") as executor:
            future_to_name = {
                executor.submit(_safe_list, name, fn, *args): name
                for name, (fn, args) in calls.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                result_name, rows = future.result()
                results[result_name or name] = rows[:50]

        vendor_candidates = results["vendor"]
        material_candidates = results["material"]
        warehouse_candidates = results["warehouse"]
        tax_code_candidates = results["tax_code"]
        ing.vendor_candidates = [dict(x) for x in vendor_candidates]
        ing.material_candidates = [dict(x) for x in material_candidates]
        ing.warehouse_candidates = [dict(x) for x in warehouse_candidates]
        ing.tax_code_candidates = [dict(x) for x in tax_code_candidates]
        state["append_event"](ing, IngestionStatus.MAPPED, "ERP master data mapping completed")
        state["append_event"](
            ing,
            IngestionStatus.MAPPED,
            f"mapping_candidates vendor={len(vendor_candidates)} material={len(material_candidates)} "
            f"warehouse={len(warehouse_candidates)} tax_code={len(tax_code_candidates)} keyword={v_kw!r}",
        )
        return {
            "vendor_candidates": len(vendor_candidates),
            "material_candidates": len(material_candidates),
            "warehouse_candidates": len(warehouse_candidates),
            "tax_code_candidates": len(tax_code_candidates),
        }

    state["mapping_metrics"] = _run_node_with_retry(
        state=state,
        node_name="map",
        fn=_map_impl,
        default_max_retries=2,
        default_backoff_ms=200,
    )
    return state


def _node_build_preview(state: WorkflowState) -> WorkflowState:
    def _preview_impl() -> Dict[str, int]:
        ing = state["ingestion"]
        existing_preview = ing.preview_data
        ing.preview_data = None
        preview = build_order_preview_data(ing)
        ing.preview_data = existing_preview
        if preview is None:
            apply_preview_to_ingestion(ing, None)
            return {"preview": 0}
        customer_material_metrics: Dict[str, int] = {}
        customer_name = (preview.order.customerName or "").strip()
        if customer_name:
            try:
                rows = state["erp"].get_customer_material_details_by_customer(customer_name)
            except Exception as exc:
                if getattr(exc, "code", None) is None:
                    raise
                rows = []
                logger.warning(
                    "customer_material_mapping_fetch_failed ingestion_id=%s code=%s status=%s",
                    ing.ingestion_id,
                    getattr(exc, "code", ""),
                    getattr(exc, "status_code", 0),
                )
            if rows:
                preview, customer_material_metrics, mapping_issues = apply_customer_material_mapping(preview, rows)
            else:
                customer_material_metrics = {"mapping_rows": 0, "matched": 0, "exact": 0, "normalized": 0, "unmatched": 0}
                mapping_issues = []
        else:
            customer_material_metrics = {"mapping_rows": 0, "matched": 0, "exact": 0, "normalized": 0, "unmatched": 0}
            mapping_issues = []
        apply_preview_to_ingestion(ing, preview)
        if mapping_issues:
            ing.issues.extend(mapping_issues)
        state["append_event"](
            ing,
            IngestionStatus.MAPPED,
            f"order preview prepared details={len(preview.details)} editable={len(ing.editable_fields)} issues={len(ing.issues)} "
            f"customer_material matched={customer_material_metrics.get('matched', 0)} "
            f"exact={customer_material_metrics.get('exact', 0)} normalized={customer_material_metrics.get('normalized', 0)} "
            f"unmatched={customer_material_metrics.get('unmatched', 0)} rows={customer_material_metrics.get('mapping_rows', 0)}",
        )
        return {
            "preview": 1,
            "details": len(preview.details),
            "editable_fields": len(ing.editable_fields),
            "issues": len(ing.issues),
            "customer_material_matched": customer_material_metrics.get("matched", 0),
            "customer_material_exact": customer_material_metrics.get("exact", 0),
            "customer_material_normalized": customer_material_metrics.get("normalized", 0),
            "customer_material_unmatched": customer_material_metrics.get("unmatched", 0),
        }

    _run_node(state["ingestion"], "build_preview", _preview_impl)
    return state


def _node_request_user_input(state: WorkflowState) -> WorkflowState:
    def _request_impl() -> Dict[str, int]:
        ing = state["ingestion"]
        erp = state["erp"]
        if ing.missing_fields:
            state["append_event"](
                ing,
                IngestionStatus.NEED_USER_INPUT,
                "required fields missing, waiting user resolve",
            )
            return {"missing_fields": len(ing.missing_fields), "auto_validated": 0}

        doc_type = ing.doc_type_hint.value if ing.doc_type_hint else "PO"
        clear_last_upstream_meta()
        refresh_ingestion_required_keys(ing)
        try:
            valid, missing = erp.validate_draft(
                doc_type,
                dict(ing.resolved_fields),
                required_keys=ing.required_resolve_keys or None,
            )
        except ErpClientError as exc:
            append_erp_call_log_with_upstream(
                ing,
                {
                    "at": datetime.utcnow().isoformat() + "Z",
                    "operation": "validate_draft",
                    "doc_type": doc_type,
                    "ok": False,
                    "erp_error_code": exc.code,
                },
                exc=exc,
            )
            ing.error_code = _map_erp_error_for_workflow(exc.code or "")
            ing.error_details = {
                "category": "upstream_error",
                "erp_error_code": exc.code,
                "erp_message": exc.message,
            }
            state["append_event"](
                ing,
                IngestionStatus.FAILED,
                f"erp_validate_failed(post_extract) code={exc.code} message={exc.message}",
            )
            return {"auto_validated": 0, "erp_error": 1}

        ing.missing_fields = list(missing)
        append_erp_call_log_with_upstream(
            ing,
            {
                "at": datetime.utcnow().isoformat() + "Z",
                "operation": "validate_draft",
                "doc_type": doc_type,
                "ok": valid,
                "missing_fields": list(missing),
            },
        )
        if valid:
            ing.error_code = None
            ing.error_details = {}
            state["append_event"](
                ing,
                IngestionStatus.VALIDATED,
                "all required fields present after extract; ERP validate passed (auto)",
            )
            return {"auto_validated": 1, "missing_fields": 0}

        state["append_event"](
            ing,
            IngestionStatus.NEED_USER_INPUT,
            "ERP validate reported missing fields after extract",
        )
        return {"missing_fields": len(ing.missing_fields), "auto_validated": 0}

    _run_node(state["ingestion"], "request_user_input", _request_impl)
    return state


def _run_with_langgraph(state: WorkflowState) -> WorkflowState:
    # 最小可运行图：先串行，后续可按条件边扩展分支与重试策略。
    graph = StateGraph(WorkflowState)
    graph.add_node("classify", _node_classify)
    graph.add_node("parse", _node_parse)
    graph.add_node("extract", _node_extract)
    graph.add_node("map", _node_map)
    graph.add_node("build_preview", _node_build_preview)
    graph.add_node("request_user_input", _node_request_user_input)
    graph.set_entry_point("classify")
    graph.add_edge("classify", "parse")
    graph.add_edge("parse", "extract")
    graph.add_edge("extract", "map")
    graph.add_edge("map", "build_preview")
    graph.add_edge("build_preview", "request_user_input")
    graph.add_edge("request_user_input", END)
    compiled = graph.compile()
    return compiled.invoke(state)


def _run_linearly(state: WorkflowState) -> WorkflowState:
    # LangGraph 不可用时的兜底执行路径，保证本地可继续联调。
    state = _node_classify(state)
    state = _node_parse(state)
    state = _node_extract(state)
    state = _node_map(state)
    state = _node_build_preview(state)
    state = _node_request_user_input(state)
    return state


def run_ingestion_processing_workflow(
    ingestion: IngestionResponse,
    erp: ErpClientProtocol,
    append_event: AppendEventFn,
) -> IngestionResponse:
    """
    执行 ingestion 处理工作流（当前为线性 MVP 版本）。

    约束：
    - 仅处理 `UPLOADED` 状态，其他状态由上层决定是否跳过；
    - 所有状态变更都通过 `append_event` 写入，确保审计轨迹完整。
    """
    logger.info("workflow_started ingestion_id=%s status=%s", ingestion.ingestion_id, ingestion.status)
    ingestion.error_code = None

    node_names: List[str] = ["classify", "parse", "extract", "map", "build_preview", "request_user_input"]
    logger.info("workflow_nodes_planned ingestion_id=%s nodes=%s", ingestion.ingestion_id, node_names)
    state: WorkflowState = {
        "ingestion": ingestion,
        "erp": erp,
        "append_event": append_event,
        "mapping_metrics": {},
        "document_text": "",
    }
    try:
        if StateGraph is not None and END is not None:
            logger.info("workflow_executor_selected ingestion_id=%s executor=langgraph", ingestion.ingestion_id)
            state = _run_with_langgraph(state)
        else:
            logger.warning(
                "workflow_executor_fallback ingestion_id=%s executor=linear reason=langgraph_unavailable",
                ingestion.ingestion_id,
            )
            state = _run_linearly(state)
    except NodeExecutionError as exc:
        ingestion.error_code = _resolve_workflow_error_code(exc)
        append_event(
            ingestion,
            IngestionStatus.FAILED,
            f"workflow node failed node={exc.node_name} failure_type={exc.failure_type} reason={exc.reason}",
        )
        logger.error(
            "workflow_failed ingestion_id=%s error_code=%s node=%s failure_type=%s reason=%s",
            ingestion.ingestion_id,
            ingestion.error_code,
            exc.node_name,
            exc.failure_type,
            exc.reason,
        )
        return ingestion
    except Exception as exc:
        ingestion.error_code = ErrorCode.WORKFLOW_UNEXPECTED_ERROR.value
        append_event(
            ingestion,
            IngestionStatus.FAILED,
            "workflow unexpected error",
        )
        logger.exception(
            "workflow_failed ingestion_id=%s error_code=%s err=%s",
            ingestion.ingestion_id,
            ingestion.error_code,
            str(exc),
        )
        return ingestion
    logger.info(
        "workflow_completed ingestion_id=%s final_status=%s vendor_candidates=%s material_candidates=%s "
        "warehouse_candidates=%s tax_code_candidates=%s",
        ingestion.ingestion_id,
        ingestion.status,
        state["mapping_metrics"].get("vendor_candidates", 0),
        state["mapping_metrics"].get("material_candidates", 0),
        state["mapping_metrics"].get("warehouse_candidates", 0),
        state["mapping_metrics"].get("tax_code_candidates", 0),
    )
    return ingestion


"""英文采购订单的通用语义路由与内部候选数据。

该模块只使用订单中的通用英文表头，不依赖客户名称或固定版式。候选数据仅供
后端客户判定和客户物料映射使用，不会被序列化到 ERP 建单 payload。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


ENGLISH_PO_ENHANCED_ENV = "ENGLISH_PO_ENHANCED_EXTRACTION_ENABLED"
ENGLISH_PO_ROUTE_VERSION = "en-po-semantic-v1"
_TRUTHY = {"1", "true", "yes", "on"}

ENGLISH_ORDER_EXTRACTION_RULES = """
英文订单增强规则：
- 这是英文或英文表头为主的采购订单。purchaser_name、supplier_name、delivery_address 可以返回原始英文；不得因没有中文而置空。
- organization_candidates 返回文档中出现的公司候选，每项为 {"name":"","role":"buyer|purchaser|supplier|vendor|bill_to|ship_to|ordering_company|other","source_label":"","page":0,"confidence":0}。只记录有明确标签或位置证据的公司。
- purchaser_name 应为向己方采购的外部客户，supplier_name 应为销售方/供方；Buyer、Purchaser、Ordering Company 优先代表客户，Supplier、Vendor 优先代表己方供方。Bill To、Ship To 仅作为候选，不能凭它们猜测客户。
- 每个 items[].material_code_candidates 返回客户物料编码候选，每项为 {"value":"","kind":"material_code|material_code_with_revision|specification","source_label":"","page":0,"confidence":0}。
- Part Number、Part No.、Material No.、Customer Part No.、Material 列的值作为 material_code 候选；若 Revision/Rev 独立出现，额外返回“编码+原始版本”的 material_code_with_revision 候选。
- Specification、Spec、Size、Dimensions 的原文可作为 specification 候选；不得用销售订单号、Line/Item/PO Line、HSN/SAC、供应商编号、图号、页码、合计替代客户物料编码。
- 所有候选的 value 必须逐字保留原图内容：不改大小写、空格、换行、乘号、标点或版本号。规范化只由后端查询时完成。
- 处理跨行单元格和续表时，必须按同一明细行拼接，不得将不同明细行合并。
- 输出 purchase_order 顶层可增加 organization_candidates；每个 items 元素可增加 material_code_candidates。它们仅是内部候选，仍须正常填写主字段 material_code、specification 等。
""".strip()


@dataclass(frozen=True)
class EnglishOrderRoute:
    route: str
    primary_headers: Tuple[str, ...] = ()
    supporting_headers: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_enhanced(self) -> bool:
        return self.route == "en_enhanced"

    @property
    def audit_signals(self) -> str:
        return ",".join((*self.primary_headers, *self.supporting_headers)) or "none"


def english_po_enhanced_enabled() -> bool:
    raw = (os.getenv(ENGLISH_PO_ENHANCED_ENV) or "true").strip().lower()
    return raw in _TRUTHY


def english_order_health_payload() -> Dict[str, Any]:
    return {
        "english_po_enhanced_extraction_enabled": english_po_enhanced_enabled(),
        "english_po_enhanced_route_version": ENGLISH_PO_ROUTE_VERSION,
    }


_PRIMARY_HEADER_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("part_number", re.compile(r"\bpart\s*(?:number|no\.?|#)\b", re.IGNORECASE)),
    ("material_no", re.compile(r"\bmaterial\s*(?:number|no\.?)\b", re.IGNORECASE)),
    ("customer_part_no", re.compile(r"\bcustomer\s*(?:part|material)\s*(?:number|no\.?|#)?\b", re.IGNORECASE)),
    # GEA 等订单将客户编码列直接称为 Material；要求同时满足两个辅助表头，避免正文误判。
    ("material", re.compile(r"\bmaterial\b", re.IGNORECASE)),
)
_SUPPORTING_HEADER_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("description", re.compile(r"\b(?:product\s+)?description\b", re.IGNORECASE)),
    ("quantity", re.compile(r"\b(?:quantity|qty)\b", re.IGNORECASE)),
    ("unit_price", re.compile(r"\b(?:unit|net)\s*price\b", re.IGNORECASE)),
    ("delivery", re.compile(r"\b(?:delivery|need[-\s]?by|required\s+in\s+house|shipment)\b", re.IGNORECASE)),
    ("unit", re.compile(r"\b(?:uom|unit)\b", re.IGNORECASE)),
)


def detect_english_order_route(document_text: str) -> EnglishOrderRoute:
    """通过英文订单明细表头决定是否启用英文增强，不按客户名称分流。"""
    if not english_po_enhanced_enabled():
        return EnglishOrderRoute(route="zh_current", reason="disabled")
    text = str(document_text or "")
    if not text.strip():
        return EnglishOrderRoute(route="zh_current", reason="insufficient_text")
    primary = tuple(name for name, pattern in _PRIMARY_HEADER_PATTERNS if pattern.search(text))
    supporting = tuple(name for name, pattern in _SUPPORTING_HEADER_PATTERNS if pattern.search(text))
    if primary and len(supporting) >= 2:
        return EnglishOrderRoute(
            route="en_enhanced",
            primary_headers=primary,
            supporting_headers=supporting,
            reason="english_table_headers",
        )
    return EnglishOrderRoute(
        route="zh_current",
        primary_headers=primary,
        supporting_headers=supporting,
        reason="insufficient_english_table_headers",
    )


def _clean_candidate(value: Any) -> str:
    return str(value or "").strip()


def _candidate_dict(value: Any, *, kind: str, source_label: str = "", page: Any = 0, confidence: Any = 0.0) -> Dict[str, Any] | None:
    text = _clean_candidate(value)
    if not text:
        return None
    try:
        parsed_page = max(0, int(page or 0))
    except (TypeError, ValueError):
        parsed_page = 0
    try:
        parsed_confidence = max(0.0, min(1.0, float(confidence or 0.0)))
    except (TypeError, ValueError):
        parsed_confidence = 0.0
    return {
        "value": text,
        "kind": str(kind or "material_code").strip() or "material_code",
        "source_label": _clean_candidate(source_label),
        "page": parsed_page,
        "confidence": parsed_confidence,
    }


def _deduplicate_material_candidates(values: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        candidate = _candidate_dict(
            item.get("value"),
            kind=str(item.get("kind") or "material_code"),
            source_label=str(item.get("source_label") or ""),
            page=item.get("page"),
            confidence=item.get("confidence"),
        )
        if candidate is None:
            continue
        if _is_excluded_material_candidate(candidate):
            continue
        key = (candidate["kind"], candidate["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _is_excluded_material_candidate(candidate: Dict[str, Any]) -> bool:
    """拒绝模型误放入候选的行号、采购单号、图号、税则号和汇总列。"""
    kind = str(candidate.get("kind") or "material_code")
    if kind == "specification":
        return False
    label = re.sub(r"[^a-z0-9]+", "", str(candidate.get("source_label") or "").casefold())
    excluded_labels = (
        "line",
        "item",
        "poline",
        "hsn",
        "sac",
        "supplierno",
        "vendorno",
        "drawing",
        "drawingno",
        "drawingnumber",
        "pageno",
        "page",
        "total",
        "amount",
    )
    if label in excluded_labels:
        return True
    value = str(candidate.get("value") or "").strip()
    return bool(re.fullmatch(r"(?:page\s*)?\d{1,3}", value, flags=re.IGNORECASE))


def material_candidate_groups_from_purchase_order(order: Any) -> List[List[Dict[str, Any]]]:
    """将模型返回候选和主字段合并；主字段永远保留作兜底。"""
    groups: List[List[Dict[str, Any]]] = []
    for item in list(getattr(order, "items", None) or []):
        raw_candidates = list(getattr(item, "material_code_candidates", None) or [])
        group: List[Dict[str, Any]] = []
        for raw in raw_candidates:
            if isinstance(raw, dict):
                raw_value = raw
            else:
                raw_value = {
                    "value": getattr(raw, "value", ""),
                    "kind": getattr(raw, "kind", "material_code"),
                    "source_label": getattr(raw, "source_label", ""),
                    "page": getattr(raw, "page", 0),
                    "confidence": getattr(raw, "confidence", 0.0),
                }
            candidate = _candidate_dict(
                raw_value.get("value"),
                kind=str(raw_value.get("kind") or "material_code"),
                source_label=str(raw_value.get("source_label") or ""),
                page=raw_value.get("page"),
                confidence=raw_value.get("confidence"),
            )
            if candidate:
                group.append(candidate)
        primary_code = _candidate_dict(getattr(item, "material_code", ""), kind="material_code", source_label="primary_material_code")
        source_spec = _candidate_dict(getattr(item, "specification", ""), kind="specification", source_label="primary_specification")
        if primary_code:
            group.insert(0, primary_code)
        if source_spec:
            group.append(source_spec)
        groups.append(_deduplicate_material_candidates(group))
    return groups


def organization_candidates_from_purchase_order(order: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    raw_candidates = list(getattr(order, "organization_candidates", None) or [])
    for raw in raw_candidates:
        if isinstance(raw, dict):
            name = _clean_candidate(raw.get("name"))
            role = _clean_candidate(raw.get("role")).lower()
            source_label = _clean_candidate(raw.get("source_label"))
            page = raw.get("page", 0)
            confidence = raw.get("confidence", 0.0)
        else:
            name = _clean_candidate(getattr(raw, "name", ""))
            role = _clean_candidate(getattr(raw, "role", "")).lower()
            source_label = _clean_candidate(getattr(raw, "source_label", ""))
            page = getattr(raw, "page", 0)
            confidence = getattr(raw, "confidence", 0.0)
        if not name:
            continue
        try:
            page_value = max(0, int(page or 0))
        except (TypeError, ValueError):
            page_value = 0
        try:
            confidence_value = max(0.0, min(1.0, float(confidence or 0.0)))
        except (TypeError, ValueError):
            confidence_value = 0.0
        out.append(
            {
                "name": name,
                "role": role or "other",
                "source_label": source_label,
                "page": page_value,
                "confidence": confidence_value,
            }
        )
    # 模型未返回扩展候选时，仍保留当前的两方字段，保证英文流程可降级。
    out.extend(
        candidate
        for candidate in (
            {"name": _clean_candidate(getattr(order, "purchaser_name", "")), "role": "purchaser", "source_label": "purchaser_name", "page": 0, "confidence": 0.0},
            {"name": _clean_candidate(getattr(order, "supplier_name", "")), "role": "supplier", "source_label": "supplier_name", "page": 0, "confidence": 0.0},
        )
        if candidate["name"]
    )
    deduplicated: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in out:
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", candidate["name"].casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


def apply_english_raw_sources_to_preview(preview: Any, order: Any) -> None:
    """将英文候选中的逐字原文回填到隐藏 source 字段，供人工重匹配使用。"""
    groups = material_candidate_groups_from_purchase_order(order)
    for detail, candidates in zip(list(getattr(preview, "details", None) or []), groups):
        raw_code = next((candidate["value"] for candidate in candidates if candidate["kind"] == "material_code"), "")
        raw_spec = next((candidate["value"] for candidate in candidates if candidate["kind"] == "specification"), "")
        if raw_code:
            detail.sourceMaterialCode = raw_code
        if raw_spec:
            detail.sourceProductSpec = raw_spec


def save_english_extraction_candidates(resolved_fields: Dict[str, str], order: Any, route: EnglishOrderRoute) -> None:
    """候选放在内部 resolved_fields，避免进入 preview_data 和 ERP payload。"""
    for key in (
        "english_order_language_route",
        "english_order_header_signals",
        "english_organization_candidates_json",
        "english_material_code_candidates_json",
    ):
        resolved_fields.pop(key, None)
    resolved_fields["english_order_language_route"] = route.route
    resolved_fields["english_order_header_signals"] = route.audit_signals
    if not route.is_enhanced:
        return
    companies = organization_candidates_from_purchase_order(order)
    groups = material_candidate_groups_from_purchase_order(order)
    if companies:
        resolved_fields["english_organization_candidates_json"] = json.dumps(companies, ensure_ascii=False)
    if any(groups):
        resolved_fields["english_material_code_candidates_json"] = json.dumps(groups, ensure_ascii=False)


def load_organization_candidates(resolved_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = str((resolved_fields or {}).get("english_organization_candidates_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def load_material_candidate_groups(resolved_fields: Dict[str, Any], detail_count: int) -> List[List[Dict[str, Any]]]:
    raw = str((resolved_fields or {}).get("english_material_code_candidates_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or len(parsed) != detail_count:
        return []
    groups: List[List[Dict[str, Any]]] = []
    for raw_group in parsed:
        if not isinstance(raw_group, list):
            return []
        groups.append(_deduplicate_material_candidates(item for item in raw_group if isinstance(item, dict)))
    return groups

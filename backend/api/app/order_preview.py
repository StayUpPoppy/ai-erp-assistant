from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Tuple

from app.schemas import (
    IngestionResponse,
    OrderPreviewData,
    OrderPreviewDetail,
    OrderPreviewHeader,
    PreviewEditableField,
    PreviewIssue,
)

ORDER_REQUIRED_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("org", "销售组织"),
    ("customerName", "客户名称"),
    ("orderDate", "订单日期"),
    ("currency", "币别"),
    ("deliveryDate", "交货期"),
)

DETAIL_REQUIRED_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("materialCode", "物料编码"),
    ("qty", "数量"),
)

PREVIEW_REQUIRED_RESOLVE_KEYS: List[str] = [
    "org",
    "customerName",
    "doc_date",
    "currency",
    "delivery_date",
    "material_code",
    "line_qty",
]

DETAIL_MISSING_KEY_BY_FIELD: Dict[str, str] = {
    "materialCode": "material_code",
    "qty": "line_qty",
}


def _pick(fields: Dict[str, str], *keys: str) -> str:
    for key in keys:
        raw = fields.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _float_or_none(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool_from_any(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _line_items_from_resolved_fields(fields: Dict[str, str]) -> List[Dict[str, Any]]:
    raw = (fields.get("datynk_details_json") or fields.get("line_items_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _source_product_specs_from_resolved_fields(fields: Dict[str, str]) -> List[str]:
    raw = (fields.get("source_product_specs_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return ["" if value is None else str(value) for value in parsed]


def _source_material_codes_from_resolved_fields(fields: Dict[str, str]) -> List[str]:
    raw = (fields.get("source_material_codes_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return ["" if value is None else str(value) for value in parsed]


def build_order_preview_data(ingestion: IngestionResponse) -> OrderPreviewData | None:
    if ingestion.preview_data is not None:
        return ingestion.preview_data
    if ingestion.doc_type_hint and ingestion.doc_type_hint.value != "PO":
        return None
    fields = dict(ingestion.resolved_fields or {})
    header = OrderPreviewHeader(
        org=_pick(fields, "org", "org_name") or ingestion.org_id,
        customerName=_pick(fields, "customerName", "customer_name", "supplier_name", "vendor_name"),
        customerPoNo=_pick(fields, "customerPoNo", "customer_po_no", "order_no"),
        salesUser=_pick(fields, "salesUser", "sales_user", "buyer_name"),
        orderDate=_pick(fields, "orderDate", "order_date", "doc_date"),
        orderStatus=_pick(fields, "orderStatus", "order_status") or "pending",
        deliveryAddr=_pick(fields, "deliveryAddr", "delivery_addr", "delivery_address"),
        rate=_float_or_none(fields.get("rate")),
        currency=_pick(fields, "currency"),
        deliveryDate=_pick(fields, "deliveryDate", "delivery_date", "jhq", "doc_date"),
    )

    details: List[OrderPreviewDetail] = []
    raw_items = _line_items_from_resolved_fields(fields)
    source_material_codes = _source_material_codes_from_resolved_fields(fields)
    source_product_specs = _source_product_specs_from_resolved_fields(fields)
    for index, item in enumerate(raw_items):
        source_material_code = item.get("sourceMaterialCode")
        if source_material_code is None and index < len(source_material_codes):
            source_material_code = source_material_codes[index]
        source_product_spec = item.get("sourceProductSpec")
        if source_product_spec is None and index < len(source_product_specs):
            source_product_spec = source_product_specs[index]
        details.append(
            OrderPreviewDetail(
                materialCode=str(item.get("materialCode") or item.get("inventory_code") or item.get("material_code") or "").strip(),
                sourceMaterialCode="" if source_material_code is None else str(source_material_code),
                productName=str(item.get("productName") or item.get("name") or item.get("product_name") or "").strip(),
                productSpec=str(
                    item.get("productSpec") or item.get("product_spec") or item.get("customerMaterialSpec") or ""
                ).strip(),
                sourceProductSpec="" if source_product_spec is None else str(source_product_spec),
                ph=str(item.get("ph") or "").strip(),
                customerMaterialNo=str(item.get("customerMaterialNo") or item.get("customer_material_no") or "").strip(),
                qty=_float_or_none(item.get("qty") or item.get("quantity")),
                price=_float_or_none(item.get("price") or item.get("unit_price_excl_tax")),
                taxPrice=_float_or_none(item.get("taxPrice") or item.get("unit_price_incl_tax")),
                amount=_float_or_none(item.get("amount") or item.get("line_amount_excl_tax")),
                allAmount=_float_or_none(item.get("allAmount") or item.get("line_amount_incl_tax")),
                tax=_float_or_none(item.get("tax") or item.get("tax_rate")),
                taxAmount=_float_or_none(item.get("taxAmount")),
                gift=_bool_from_any(item.get("gift")),
                remark=str(item.get("remark") or "").strip(),
            )
        )
    if not details:
        details.append(
            OrderPreviewDetail(
                materialCode=_pick(fields, "materialCode", "material_code"),
                sourceMaterialCode=source_material_codes[0] if source_material_codes else "",
                productName=_pick(fields, "productName", "product_name"),
                productSpec=_pick(fields, "productSpec", "product_spec", "customerMaterialSpec"),
                sourceProductSpec=source_product_specs[0] if source_product_specs else "",
                ph=_pick(fields, "ph", "material_ph"),
                customerMaterialNo=_pick(fields, "customerMaterialNo", "customer_material_no"),
                qty=_float_or_none(fields.get("qty") or fields.get("line_qty")),
                price=_float_or_none(fields.get("price") or fields.get("unit_price_excl_tax")),
                taxPrice=_float_or_none(fields.get("taxPrice")),
                amount=_float_or_none(fields.get("amount")),
                allAmount=_float_or_none(fields.get("allAmount")),
                tax=_float_or_none(fields.get("tax") or fields.get("tax_rate")),
                taxAmount=_float_or_none(fields.get("taxAmount")),
                gift=_bool_from_any(fields.get("gift")),
                remark=_pick(fields, "remark", "line_remark", "detail_remark"),
            )
        )
    return OrderPreviewData(order=header, details=details)


def preview_issues(preview: OrderPreviewData) -> List[PreviewIssue]:
    def inconsistent(actual: float, expected: float) -> bool:
        return abs(actual - expected) > max(0.05, abs(expected) * 0.005)

    issues: List[PreviewIssue] = []
    if not preview.details:
        issues.append(PreviewIssue(path="details", level="error", message="未识别到订单明细，请至少补充一行明细"))
        return issues
    for idx, detail in enumerate(preview.details):
        if detail.qty is not None and detail.qty <= 0:
            issues.append(PreviewIssue(path=f"details[{idx}].qty", level="warning", message="数量必须大于 0"))
        if detail.amount is not None and detail.price is not None and detail.qty is not None:
            expected = round(detail.price * detail.qty, 6)
            if inconsistent(detail.amount, expected):
                issues.append(
                    PreviewIssue(
                        path=f"details[{idx}].amount",
                        level="warning",
                        message="不含税金额与数量×不含税单价不一致，请人工核对",
                    )
                )
        if detail.allAmount is not None and detail.taxPrice is not None and detail.qty is not None:
            expected = round(detail.taxPrice * detail.qty, 6)
            if inconsistent(detail.allAmount, expected):
                issues.append(
                    PreviewIssue(
                        path=f"details[{idx}].allAmount",
                        level="warning",
                        message="含税金额与数量×含税单价不一致，请人工核对",
                    )
                )
        if detail.amount is not None and detail.taxAmount is not None and detail.allAmount is not None:
            expected = round(detail.amount + detail.taxAmount, 6)
            if inconsistent(detail.allAmount, expected):
                issues.append(
                    PreviewIssue(
                        path=f"details[{idx}].taxAmount",
                        level="warning",
                        message="不含税金额＋税额与含税金额不一致，请人工核对",
                    )
                )
        if detail.price is not None and detail.taxPrice is not None and detail.tax is not None:
            tax_rate = detail.tax / 100 if abs(detail.tax) > 1 else detail.tax
            expected = round(detail.price * (1 + tax_rate), 6)
            if inconsistent(detail.taxPrice, expected):
                issues.append(
                    PreviewIssue(
                        path=f"details[{idx}].taxPrice",
                        level="warning",
                        message="含税单价与不含税单价及税率不一致，请人工核对",
                    )
                )
    return issues


def normalize_customer_material_code(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not text:
        return ""
    return re.sub(r"[\s\-_./\\,，、。]+", "", text)


def preserve_customer_material_numbers_from_sources(preview: OrderPreviewData) -> Tuple[OrderPreviewData, int]:
    """客户未确定时，将 PDF 原始物料编码移入客户物料编码供后续重新匹配。"""
    preserved = 0
    details: List[OrderPreviewDetail] = []
    for detail in preview.details:
        if str(detail.customerMaterialNo or "").strip():
            details.append(detail)
            continue
        raw_value = detail.sourceMaterialCode
        if not str(raw_value or "").strip():
            raw_value = detail.materialCode
        raw_code = str(raw_value or "").strip()
        if not raw_code:
            details.append(detail)
            continue
        preserved += 1
        details.append(
            detail.model_copy(
                update={
                    "customerMaterialNo": raw_code,
                    "materialCode": "",
                }
            )
        )
    return preview.model_copy(update={"details": details}), preserved


def _candidate_match(
    candidates: Iterable[Dict[str, Any]],
    exact_index: Dict[str, Dict[str, str]],
    normalized_index: Dict[str, Dict[str, str]],
    duplicate_normalized: set[str],
) -> Tuple[Dict[str, str] | None, str, str, List[str]]:
    """按英文订单候选的既定优先级找到唯一 ERP 物料。"""
    cleaned: List[Dict[str, str]] = []
    for candidate in candidates:
        value = str(candidate.get("value") or "").strip()
        if not value:
            continue
        kind = str(candidate.get("kind") or "material_code").strip() or "material_code"
        cleaned.append({"value": value, "kind": kind})

    phases = (
        ("exact", {"material_code"}),
        ("exact", {"material_code_with_revision"}),
        ("normalized", {"material_code", "material_code_with_revision"}),
        ("exact", {"specification"}),
        ("normalized", {"specification"}),
    )
    for method, accepted_kinds in phases:
        hits: List[Tuple[str, Dict[str, str]]] = []
        for candidate in cleaned:
            if candidate["kind"] not in accepted_kinds:
                continue
            if method == "exact":
                hit = exact_index.get(candidate["value"])
            else:
                normalized = normalize_customer_material_code(candidate["value"])
                hit = normalized_index.get(normalized) if normalized and normalized not in duplicate_normalized else None
            if hit is not None:
                hits.append((candidate["value"], hit))
        if not hits:
            continue
        material_numbers = {str(hit.get("materialNumber") or "").strip() for _, hit in hits}
        material_numbers.discard("")
        if len(material_numbers) == 1:
            value, hit = hits[0]
            return hit, method, value, []
        return None, "ambiguous", "", [value for value, _ in hits]
    return None, "", "", []


def apply_customer_material_mapping(
    preview: OrderPreviewData,
    mapping_details: Iterable[Dict[str, Any]],
    *,
    use_material_code_fallback: bool = True,
    candidate_groups: List[List[Dict[str, Any]]] | None = None,
) -> Tuple[OrderPreviewData, Dict[str, int], List[PreviewIssue]]:
    exact_index: Dict[str, Dict[str, str]] = {}
    normalized_index: Dict[str, Dict[str, str]] = {}
    duplicate_normalized: set[str] = set()

    for raw_row in mapping_details:
        row = {str(k): str(v).strip() for k, v in dict(raw_row).items() if v is not None}
        cust_code = (row.get("custMaterialCode") or "").strip()
        internal_code = (row.get("materialNumber") or "").strip()
        if not cust_code or not internal_code:
            continue
        if cust_code not in exact_index:
            exact_index[cust_code] = row
        norm = normalize_customer_material_code(cust_code)
        if not norm:
            continue
        if norm in normalized_index:
            duplicate_normalized.add(norm)
        else:
            normalized_index[norm] = row

    matched = 0
    exact = 0
    normalized = 0
    unmatched = 0
    skipped = 0
    ambiguous = 0
    issues: List[PreviewIssue] = []
    next_details: List[OrderPreviewDetail] = []

    for idx, detail in enumerate(preview.details):
        raw_code = (
            detail.customerMaterialNo
            or (detail.materialCode if use_material_code_fallback else "")
            or ""
        ).strip()
        candidates = candidate_groups[idx] if candidate_groups is not None and idx < len(candidate_groups) else []
        candidate_raw_values = [str(candidate.get("value") or "").strip() for candidate in candidates if str(candidate.get("value") or "").strip()]
        if not raw_code and candidate_raw_values:
            raw_code = candidate_raw_values[0]
        if not raw_code:
            skipped += 1
            next_details.append(detail)
            continue

        ambiguous_values: List[str] = []
        if candidates:
            hit, method, matched_code, ambiguous_values = _candidate_match(
                candidates,
                exact_index,
                normalized_index,
                duplicate_normalized,
            )
            if hit is not None:
                raw_code = matched_code
        else:
            hit = exact_index.get(raw_code)
            method = "exact"
            if hit is None:
                norm = normalize_customer_material_code(raw_code)
                if norm and norm not in duplicate_normalized:
                    hit = normalized_index.get(norm)
                    method = "normalized"

        if method == "ambiguous":
            ambiguous += 1
            unmatched += 1
            shown_values = "、".join(dict.fromkeys(ambiguous_values))
            issues.append(
                PreviewIssue(
                    path=f"details[{idx}].materialCode",
                    level="error",
                    message=(
                        f"客户物料编码候选（{shown_values}）匹配到多个 ERP 内部物料，"
                        "请人工选择或修改客户物料编码后重新匹配。"
                    ),
                )
            )
            next_details.append(
                detail.model_copy(
                    update={
                        "customerMaterialNo": raw_code,
                        "materialCode": "",
                        "productName": "",
                        "productSpec": "",
                        "ph": "",
                    }
                )
            )
            continue

        if hit is None:
            unmatched += 1
            issues.append(
                PreviewIssue(
                    path=f"details[{idx}].materialCode",
                    level="error",
                    message=(
                        f"客户物料编码 {raw_code} 未在 ERP 客户物料对应表中找到，"
                        "请先到客户物料对应表创建客户物料与内部物料的对应关系，"
                        "然后方可显示内部物料编码、物料名称、物料规格以及物料牌号等关系。"
                    ),
                )
            )
            next_details.append(
                detail.model_copy(
                    update={
                        "customerMaterialNo": raw_code,
                        "materialCode": "",
                        "productName": "",
                        "productSpec": "",
                        "ph": "",
                    }
                )
            )
            continue

        matched += 1
        if method == "exact":
            exact += 1
        else:
            normalized += 1
        next_details.append(
            detail.model_copy(
                update={
                    "customerMaterialNo": raw_code,
                    "materialCode": hit.get("materialNumber") or "",
                    "productName": hit.get("materialName") or "",
                    "productSpec": hit.get("materialModel") or "",
                    "ph": hit.get("ph") or "",
                }
            )
        )

    metrics = {
        "mapping_rows": len(exact_index),
        "matched": matched,
        "exact": exact,
        "normalized": normalized,
        "unmatched": unmatched,
    }
    if candidate_groups is not None:
        metrics["ambiguous"] = ambiguous
    if not use_material_code_fallback:
        metrics["skipped"] = skipped
    return preview.model_copy(update={"details": next_details}), metrics, issues


def preview_editable_fields(preview: OrderPreviewData) -> List[PreviewEditableField]:
    out: List[PreviewEditableField] = []
    order = preview.order
    for key, label in ORDER_REQUIRED_FIELDS:
        raw = getattr(order, key, "")
        text = "" if raw is None else str(raw).strip()
        if not text:
            out.append(
                PreviewEditableField(
                    path=f"order.{key}",
                    label=label,
                    current_value="",
                    required=True,
                    reason="订单头必填字段缺失",
                    confidence=0.0,
                )
            )
    rate = order.rate
    if str(order.currency or "").strip() and (
        rate is None or not math.isfinite(float(rate)) or float(rate) <= 0
    ):
        out.append(
            PreviewEditableField(
                path="order.rate",
                label="汇率",
                current_value="" if rate is None else str(rate),
                required=True,
                reason="当前币别需要填写大于 0 的有效汇率",
                confidence=0.0,
            )
        )
    for idx, detail in enumerate(preview.details):
        for key, label in DETAIL_REQUIRED_FIELDS:
            raw = getattr(detail, key, None)
            text = "" if raw is None else str(raw).strip()
            if not text:
                out.append(
                    PreviewEditableField(
                        path=f"details[{idx}].{key}",
                        label=f"第 {idx + 1} 行{label}",
                        current_value="",
                        required=True,
                        reason="订单明细必填字段缺失",
                        confidence=0.0,
                    )
                )
    return out


def _missing_key_for_editable_path(path: str) -> str:
    if path == "order.customerName":
        return "customerName"
    if path == "order.org":
        return "org"
    if path == "order.orderDate":
        return "doc_date"
    if path == "order.currency":
        return "currency"
    if path == "order.rate":
        return "rate"
    if path == "order.deliveryDate":
        return "delivery_date"

    match = re.fullmatch(r"details\[(\d+)\]\.([A-Za-z0-9_]+)", path)
    if match:
        index = int(match.group(1))
        field = match.group(2)
        mapped = DETAIL_MISSING_KEY_BY_FIELD.get(field)
        if mapped and index == 0:
            return mapped
    return path


def apply_preview_to_ingestion(ingestion: IngestionResponse, preview: OrderPreviewData | None) -> IngestionResponse:
    ingestion.preview_data = preview
    if preview is None:
        ingestion.editable_fields = []
        ingestion.issues = []
        return ingestion
    ingestion.editable_fields = preview_editable_fields(preview)
    ingestion.issues = preview_issues(preview)
    ingestion.required_resolve_keys = list(PREVIEW_REQUIRED_RESOLVE_KEYS)
    ingestion.missing_fields = list(
        dict.fromkeys(_missing_key_for_editable_path(field.path) for field in ingestion.editable_fields)
    )
    return ingestion


def preview_to_resolved_fields(preview: OrderPreviewData) -> Dict[str, str]:
    header = preview.order
    details = preview.details or [OrderPreviewDetail()]
    first = details[0]
    details_payload: List[Dict[str, Any]] = []
    for item in details:
        details_payload.append(
            {
                "materialCode": item.materialCode,
                "productName": item.productName,
                "productSpec": item.productSpec,
                "ph": item.ph,
                "customerMaterialNo": item.customerMaterialNo,
                "qty": item.qty,
                "price": item.price,
                "taxPrice": item.taxPrice,
                "amount": item.amount,
                "allAmount": item.allAmount,
                "tax": item.tax,
                "taxAmount": item.taxAmount,
                "gift": item.gift,
                "remark": item.remark,
            }
        )
    source_material_codes_json = (
        json.dumps([item.sourceMaterialCode for item in details], ensure_ascii=False)
        if any(item.sourceMaterialCode for item in details)
        else ""
    )
    source_product_specs_json = (
        json.dumps([item.sourceProductSpec for item in details], ensure_ascii=False)
        if any(item.sourceProductSpec for item in details)
        else ""
    )
    return {
        "org": header.org,
        "customerName": header.customerName,
        "customer_name": header.customerName,
        "customerPoNo": header.customerPoNo,
        "salesUser": header.salesUser,
        "orderDate": header.orderDate,
        "orderStatus": header.orderStatus,
        "deliveryAddr": header.deliveryAddr,
        "currency": header.currency,
        "deliveryDate": header.deliveryDate,
        "delivery_date": header.deliveryDate,
        "doc_date": header.orderDate,
        "rate": "" if header.rate is None else str(header.rate),
        "materialCode": first.materialCode,
        "material_code": first.materialCode,
        "productName": first.productName,
        "productSpec": first.productSpec,
        "ph": first.ph,
        "customerMaterialNo": first.customerMaterialNo,
        "qty": "" if first.qty is None else str(first.qty),
        "line_qty": "" if first.qty is None else str(first.qty),
        "price": "" if first.price is None else str(first.price),
        "taxPrice": "" if first.taxPrice is None else str(first.taxPrice),
        "amount": "" if first.amount is None else str(first.amount),
        "allAmount": "" if first.allAmount is None else str(first.allAmount),
        "tax": "" if first.tax is None else str(first.tax),
        "taxAmount": "" if first.taxAmount is None else str(first.taxAmount),
        "gift": "true" if first.gift else "false",
        "remark": first.remark,
        "source_material_codes_json": source_material_codes_json,
        "source_product_specs_json": source_product_specs_json,
        "datynk_details_json": json.dumps(details_payload, ensure_ascii=False),
        "line_items_json": json.dumps(details_payload, ensure_ascii=False),
    }


def preview_missing_keys(preview: OrderPreviewData) -> List[str]:
    return list(dict.fromkeys(_missing_key_for_editable_path(field.path) for field in preview_editable_fields(preview)))


def merge_non_empty(base: Dict[str, str], patch: Dict[str, str]) -> Dict[str, str]:
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
        elif key in out:
            out[key] = ""
    return out

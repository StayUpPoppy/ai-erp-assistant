from __future__ import annotations

import json
import logging
import os
import re
from time import perf_counter
from typing import Any, Dict, Optional

from app.llm_client import chat_completion_json, llm_available, llm_model_name, llm_prompt_version
from app.order_preview import apply_preview_to_ingestion, preview_to_resolved_fields
from app.schemas import IngestionResponse, OrderPreviewData, OrderPreviewDetail, OrderPreviewHeader, PreviewIssue, PurchaseOrder

logger = logging.getLogger("ai_erp_api")

SYSTEM_PROMPT = """你是制造业采购订单抽取引擎，只能依据用户提供的订单原文抽取字段，不能猜测、补全或编造。

任务目标：
1. 从采购订单/销售订单文本中抽取订单头、物料明细、金额和交期。
2. 输出严格 JSON，不要 Markdown，不要解释文字。
3. 优先返回新结构 {"purchase_order":{...}}；如果字段无法确定，字符串填 ""，数字填 0，并把字段名加入 uncertain_fields。

字段语义：
- order_number：客户/外部采购订单号，如 PO No、采购单号、订单号、合同编号。不要填本系统内部流水号、页码、发票号。
- purchaser_name：买方/需方/甲方/采购商/客户名称。
- supplier_name：卖方/供方/乙方/供应商名称。
- order_date：订单签订或下单日期，统一 YYYY-MM-DD；无法确定则 ""。
- payment_terms：付款方式或结算条款。
- tax_rate：税率百分数,无法确定输出 0。
- delivery_address：收货/送货地址，只保留地址正文。
- total_order_amount：订单总金额；不要把保证金、违约金、页脚金额当总金额。
- items：逐行物料明细，不要合并不同物料行，不要把表头、合计行、备注行当物料行。
- items[].material_code：物料编码/存货编码/产品编码/料号。不要填规格、图号或客户订单号。
- items[].material_name：物料名称/品名/产品名称。
- items[].specification：规格型号/型号/尺寸。
- items[].material_texture：材质/牌号。
- items[].quantity：采购数量，只填数字。
- items[].unit：计量单位，如 件、PCS、KG、套。
- items[].unit_price_without_tax：不含税单价；只有原文明确为不含税/未税/除税/净价时填写。
- items[].unit_price_with_tax：含税单价；只有原文明确为含税/价税合计单价时填写。不要把不含税单价填到这里。
- items[].total_amount_without_tax：单行不含税金额；只有原文明确给出或列名为不含税/未税金额时填写。
- items[].total_amount_with_tax：单行含税金额；只有原文明确给出或列名为含税金额/价税合计时填写。
- items[].total_amount：兼容字段，仅在原文明确是不含税金额时填写；不要把含税金额填到 total_amount。
- items[].delivery_date：该行交货日期，统一 YYYY-MM-DD。
- items[].drawing_number：图号/生产单号/版本号。

证据与置信度：
- purchase_order.evidence 按字段名返回证据，例如 {"order_number":{"source_text":"订单号：PO-001","page":1,"confidence":0.95}}。
- items[].evidence 同样按字段名返回证据。
- confidence 范围 0-1；低于 0.75 或来源不清时，把字段名加入 uncertain_fields。
- 若某字段在原文中没有清晰证据，必须留空/0，并加入 uncertain_fields。

一致性规则：
- 不要根据税率在含税/不含税金额之间互相推算；原文没有对应字段就输出 0。
- 如果 quantity * unit_price_without_tax 与 total_amount_without_tax 明显不一致，不要自行修正；保留原文字段并在 extraction_notes 写明。
- 自动过滤合同法律条款、违约条款、保密条款、廉政条款、页码、水印、公章、签字盖章区。

输出 JSON 结构：
{"purchase_order":{"order_number":"","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"","material_name":"","specification":"","material_texture":"","quantity":0,"unit":"","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}""".strip()


def _user_prompt(document_text: str) -> str:
    return (
        "请解析以下订单文本。严格按 system schema 输出 JSON；没有原文证据的字段不要猜。\n"
        "<document_text>\n"
        f"{document_text[:8000]}\n"
        "</document_text>"
    )

LLM_CONTEXT_KEYWORDS = (
    "purchase order",
    "po no",
    "po number",
    "order no",
    "order number",
    "customer",
    "supplier",
    "vendor",
    "buyer",
    "seller",
    "material",
    "part no",
    "item",
    "qty",
    "quantity",
    "unit price",
    "amount",
    "total",
    "tax",
    "delivery",
    "address",
    "采购",
    "订单",
    "客户",
    "供应商",
    "物料",
    "料号",
    "品名",
    "规格",
    "数量",
    "单价",
    "金额",
    "合计",
    "税率",
    "交货",
    "地址",
)


def _llm_context_max_chars() -> int:
    raw = (os.getenv("LLM_EXTRACT_CONTEXT_MAX_CHARS") or "6000").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 6000
    return max(1200, min(value, 20000))


def _clip_llm_document_context(document_text: str, resolved_fields: Optional[Dict[str, str]] = None) -> str:
    text = (document_text or "").strip()
    if not text:
        return ""
    max_chars = _llm_context_max_chars()
    if len(text) <= max_chars:
        return text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected: list[str] = []
    seen: set[int] = set()

    def add_line(index: int) -> None:
        if 0 <= index < len(lines) and index not in seen:
            seen.add(index)
            selected.append(lines[index])

    keywords = [keyword.lower() for keyword in LLM_CONTEXT_KEYWORDS]
    for idx, line in enumerate(lines):
        lower = line.lower()
        if any(keyword in lower for keyword in keywords):
            for j in range(idx, idx + 3):
                add_line(j)

    for i in range(min(8, len(lines))):
        add_line(i)

    for value in (resolved_fields or {}).values():
        needle = str(value or "").strip()
        if len(needle) < 3:
            continue
        low_needle = needle.lower()
        for idx, line in enumerate(lines):
            if low_needle in line.lower():
                for j in range(idx - 1, idx + 2):
                    add_line(j)
                break

    if len(lines) > 20:
        for i in range(max(0, len(lines) - 10), len(lines)):
            add_line(i)

    clipped = "\n".join(selected).strip()
    if not clipped:
        clipped = text[:max_chars]
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 3].rstrip() + "..."
    return clipped


def _build_user_prompt(document_text: str, resolved_fields: Optional[Dict[str, str]] = None) -> str:
    clipped_text = _clip_llm_document_context(document_text, resolved_fields)
    known_fields = {k: v for k, v in (resolved_fields or {}).items() if str(v).strip()}
    return (
        _user_prompt(clipped_text)
        + "\n<known_fields>\n"
        + json.dumps(known_fields, ensure_ascii=False)
        + "\n</known_fields>"
    )


def _strip_code_fence(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw.startswith("```"):
        return raw
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S)
    return (match.group(1) if match else raw.strip("`")).strip()


def _json_object_candidate(raw: str) -> str:
    text = _strip_code_fence(raw)
    start = text.find("{")
    if start < 0:
        return text

    depth = 0
    in_string = False
    escaped = False
    for idx, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return text[start:]


def _escape_control_chars_inside_strings(raw: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
        else:
            if ch == '"':
                in_string = True
        out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> Dict[str, Any]:
    candidate = _json_object_candidate((text or "").strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = json.loads(_escape_control_chars_inside_strings(candidate))
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON root must be an object")
    return parsed


JSON_REPAIR_SYSTEM_PROMPT = """You repair malformed JSON only.
Return one valid JSON object and nothing else.
Do not add facts, do not infer missing values, do not explain.
Keep the original field names and values whenever possible.
If a value cannot be repaired, use an empty string for text, 0 for numbers, [] for lists, or {} for objects."""


def _repair_llm_json(raw_content: str, parse_error: Exception) -> str:
    repair_prompt = (
        "Repair this malformed JSON-like response so it matches the purchase_order JSON object schema. "
        "Only repair syntax/structure. Do not add or infer business data.\n"
        f"Parse error: {parse_error}\n"
        "<malformed_response>\n"
        f"{(raw_content or '')[:12000]}\n"
        "</malformed_response>"
    )
    return chat_completion_json(
        [
            {"role": "system", "content": JSON_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": repair_prompt},
        ]
    )


def _extract_json_with_repair(text: str) -> tuple[Dict[str, Any], bool]:
    try:
        return _extract_json(text), False
    except Exception as exc:
        repaired = _repair_llm_json(text, exc)
        return _extract_json(repaired), True


def _zero_to_none(value: float) -> float | None:
    return None if value == 0 else value


def _first_delivery_date(order: PurchaseOrder) -> str:
    for item in order.items:
        if item.delivery_date.strip():
            return item.delivery_date.strip()
    return ""


def _line_amount_without_tax(item_qty: float, price_without_tax: float, total_amount: float) -> float | None:
    if total_amount:
        return total_amount
    return None


def _line_amount_with_tax(item_qty: float, price_with_tax: float, amount_without_tax: float | None, tax_rate: float) -> float | None:
    return None


def _field_evidence_confidence(evidence: Dict[str, Any], field: str) -> Optional[float]:
    raw = evidence.get(field)
    if not isinstance(raw, dict):
        return None
    value = raw.get("confidence")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _append_llm_quality_issues(ingestion: IngestionResponse, order: PurchaseOrder) -> None:
    seen: set[str] = set()

    def add(path: str, message: str) -> None:
        if path in seen:
            return
        seen.add(path)
        ingestion.issues.append(PreviewIssue(path=path, level="warning", message=message))

    for field in order.uncertain_fields:
        add(f"order.{field}", f"LLM 对字段 {field} 置信度不足，建议人工核对。")

    for field, raw in order.evidence.items():
        conf = _field_evidence_confidence(order.evidence, field)
        if conf is not None and conf < 0.75:
            add(f"order.{field}", f"LLM 对字段 {field} 的证据置信度较低（{conf:.2f}），建议人工核对。")

    for index, item in enumerate(order.items):
        for field in item.uncertain_fields:
            add(f"details[{index}].{field}", f"LLM 对第 {index + 1} 行字段 {field} 置信度不足，建议人工核对。")
        for field in item.evidence:
            conf = _field_evidence_confidence(item.evidence, field)
            if conf is not None and conf < 0.75:
                add(
                    f"details[{index}].{field}",
                    f"LLM 对第 {index + 1} 行字段 {field} 的证据置信度较低（{conf:.2f}），建议人工核对。",
                )
        amount_without_tax = item.total_amount_without_tax or item.total_amount
        if item.quantity and item.unit_price_without_tax and amount_without_tax:
            expected = item.quantity * item.unit_price_without_tax
            if abs(expected - amount_without_tax) > max(0.05, abs(amount_without_tax) * 0.02):
                add(
                    f"details[{index}].amount",
                    f"第 {index + 1} 行金额与数量×不含税单价不一致，请人工核对。",
                )

    for note in order.extraction_notes:
        text = str(note).strip()
        if text:
            add("llm.extraction_notes", text[:200])


def _purchase_order_to_preview(order: PurchaseOrder, org_hint: str) -> OrderPreviewData:
    details: list[OrderPreviewDetail] = []
    for item in order.items:
        qty = item.quantity
        amount = _line_amount_without_tax(
            qty,
            item.unit_price_without_tax,
            item.total_amount_without_tax or item.total_amount,
        )
        all_amount = item.total_amount_with_tax or None
        tax_amount = None
        if amount is not None and all_amount is not None:
            tax_amount = round(all_amount - amount, 10)
        remark_parts = []
        if item.unit:
            remark_parts.append(f"单位：{item.unit}")
        if item.drawing_number:
            remark_parts.append(f"图号/生产单号：{item.drawing_number}")
        details.append(
            OrderPreviewDetail(
                materialCode=item.material_code,
                productName=item.material_name,
                productSpec=item.specification,
                ph=item.material_texture,
                customerMaterialNo="",
                qty=_zero_to_none(qty),
                price=_zero_to_none(item.unit_price_without_tax),
                taxPrice=_zero_to_none(item.unit_price_with_tax),
                amount=amount,
                allAmount=all_amount,
                tax=_zero_to_none(order.tax_rate),
                taxAmount=tax_amount,
                gift=False,
                remark="；".join(remark_parts),
            )
        )
    if not details:
        details.append(OrderPreviewDetail())
    return OrderPreviewData(
        order=OrderPreviewHeader(
            org=org_hint,
            customerName=order.purchaser_name,
            customerPoNo=order.order_number,
            salesUser="",
            orderDate=order.order_date,
            orderStatus="pending",
            deliveryAddr=order.delivery_address,
            rate=1,
            currency="CNY",
            deliveryDate=_first_delivery_date(order) or order.order_date,
        ),
        details=details,
    )


def try_apply_llm_preview(ingestion: IngestionResponse, document_text: str) -> bool:
    if not llm_available():
        logger.info("llm_preview_skipped ingestion_id=%s reason=not_available", ingestion.ingestion_id)
        return False
    if ingestion.doc_type_hint and ingestion.doc_type_hint.value != "PO":
        logger.info(
            "llm_preview_skipped ingestion_id=%s reason=unsupported_doc_type doc_type=%s",
            ingestion.ingestion_id,
            ingestion.doc_type_hint.value,
        )
        return False
    if not document_text.strip():
        logger.info("llm_preview_skipped ingestion_id=%s reason=empty_document_text", ingestion.ingestion_id)
        return False
    try:
        llm_started = perf_counter()
        user_prompt = _build_user_prompt(document_text, ingestion.resolved_fields)
        input_chars = len(user_prompt)
        content = chat_completion_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
        elapsed_ms = int((perf_counter() - llm_started) * 1000)
        parsed, repaired_json = _extract_json_with_repair(content)
        if "purchase_order" in parsed and isinstance(parsed["purchase_order"], dict):
            parsed = parsed["purchase_order"]
        purchase_order = PurchaseOrder.model_validate(parsed)
        preview = _purchase_order_to_preview(purchase_order, ingestion.org_id)
        _append_llm_quality_issues(ingestion, purchase_order)
    except Exception as exc:
        logger.warning("llm_preview_failed ingestion_id=%s err=%s", ingestion.ingestion_id, exc)
        ingestion.issues.append(
            PreviewIssue(path="llm", level="warning", message=f"LLM 抽取失败，已回退到规则抽取：{exc}")
        )
        return False

    apply_preview_to_ingestion(ingestion, preview)
    fields = preview_to_resolved_fields(preview)
    fields.update(
        {
            "order_number": purchase_order.order_number,
            "customer_po_no": purchase_order.order_number,
            "customer_name": purchase_order.purchaser_name,
            "supplier_name": purchase_order.supplier_name,
            "vendor_name": purchase_order.supplier_name,
            "payment_terms": purchase_order.payment_terms,
            "total_order_amount": "" if purchase_order.total_order_amount == 0 else str(purchase_order.total_order_amount),
        }
    )
    ingestion.resolved_fields.update({k: v for k, v in fields.items() if str(v).strip()})
    ingestion.model_version = llm_model_name()
    ingestion.prompt_version = llm_prompt_version()
    logger.info(
        "llm_preview_applied ingestion_id=%s model=%s items=%s input_chars=%s source_chars=%s elapsed_ms=%s json_repaired=%s",
        ingestion.ingestion_id,
        ingestion.model_version,
        len(purchase_order.items),
        input_chars,
        len(document_text),
        elapsed_ms,
        int(repaired_json),
    )
    return True

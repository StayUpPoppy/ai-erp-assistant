from __future__ import annotations

import re
from typing import Dict, Iterable, Optional

from app.schemas import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatTaskState,
    ChatToolMessage,
    ErrorCode,
    ToolResult,
    ToolUi,
)
from app.tools.pdf_to_erp import FIELD_LABELS, pdf_to_erp_tool


CREATE_DRAFT_WORDS = ("确认上传", "上传", "提交", "生成草稿", "创建草稿", "入库", "确认")
STATUS_WORDS = ("状态", "进度", "到哪", "处理", "查一下")
CANCEL_WORDS = ("取消", "不要", "先不")


def _infer_action(payload: ChatMessageRequest) -> str:
    if payload.action:
        return payload.action
    if payload.preview_data is not None:
        return "confirm_preview"
    if payload.fields:
        return "submit_missing_fields"
    text = (payload.message or "").strip()
    if any(word in text for word in CANCEL_WORDS):
        return "cancel"
    if any(word in text for word in CREATE_DRAFT_WORDS):
        return "create_draft"
    if any(word in text for word in STATUS_WORDS):
        return "get_status"
    return "get_status"


FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    "vendor_code": ("vendor_code", "供应商编码", "供应商", "客户编码"),
    "doc_date": ("doc_date", "单据日期", "订单日期", "日期"),
    "currency": ("currency", "币别", "币种", "货币"),
    "material_code": ("material_code", "物料编码", "物料", "料号"),
    "line_qty": ("line_qty", "数量", "订单数量"),
    "po_no": ("po_no", "采购订单号", "采购单号", "订单号"),
    "qty_received": ("qty_received", "收货数量"),
    "invoice_no": ("invoice_no", "发票号", "发票号码"),
    "invoice_date": ("invoice_date", "发票日期"),
    "warehouse_code": ("warehouse_code", "仓库编码", "仓库"),
    "tax_code": ("tax_code", "税码", "税率编码"),
    "org": ("org", "销售组织", "组织"),
    "customerName": ("customerName", "客户名称", "客户", "客户名"),
    "customerPoNo": ("customerPoNo", "客户采购单号", "客户PO", "客户订单号"),
    "salesUser": ("salesUser", "销售员", "业务员"),
    "delivery_date": ("delivery_date", "交货日期", "交期"),
    "deliveryDate": ("deliveryDate", "交货日期", "交期"),
}


def _candidate_keys(missing: Iterable[str]) -> list[str]:
    keys = [k for k in missing if k]
    return keys or list(FIELD_LABELS.keys())


def _extract_fields_from_text(text: str, missing: Iterable[str]) -> Dict[str, str]:
    text = (text or "").strip()
    if not text:
        return {}
    out: Dict[str, str] = {}
    stop = r"(?:\s*(?:,|，|;|；|\n|。)\s*)"
    for key in _candidate_keys(missing):
        aliases = FIELD_ALIASES.get(key, (key, FIELD_LABELS.get(key, key)))
        for alias in aliases:
            pat = rf"(?:^|{stop}){re.escape(alias)}\s*(?:是|为|=|:|：)?\s*([^,，;；\n。]+)"
            m = re.search(pat, text, flags=re.IGNORECASE)
            if not m:
                continue
            value = m.group(1).strip(" ：:=，,;；。 \t")
            if value:
                out[key] = value
                break
    return out


def _not_found(session_id: Optional[str], ingestion_id: Optional[str]) -> ChatMessageResponse:
    return ChatMessageResponse(
        session_id=session_id,
        messages=[ChatToolMessage(role="system", content="没有找到当前 PDF 转 ERP 任务，请重新上传文件后再继续。")],
        active_task=ChatTaskState(type="pdf_to_erp", ingestion_id=ingestion_id, status="NOT_FOUND"),
        ui=ToolUi(type="error", data={"error_code": ErrorCode.INGESTION_NOT_FOUND.value}),
    )


def _response(session_id: Optional[str], result: ToolResult) -> ChatMessageResponse:
    return ChatMessageResponse(
        session_id=session_id,
        messages=[ChatToolMessage(role="assistant", content=result.message)],
        active_task=ChatTaskState(type=result.tool, ingestion_id=result.ingestion_id, status=result.status),
        tool_result=result,
        ui=result.ui,
    )


def handle_chat_message(payload: ChatMessageRequest) -> ChatMessageResponse:
    tool_name = (payload.tool or "pdf_to_erp").strip()
    if tool_name != "pdf_to_erp":
        return ChatMessageResponse(
            session_id=payload.session_id,
            messages=[ChatToolMessage(role="system", content=f"暂时还没有接入工具：{tool_name}。")],
            active_task=ChatTaskState(type=tool_name, status="UNSUPPORTED"),
            ui=ToolUi(type="error", data={"error_code": "UNSUPPORTED_TOOL"}),
        )

    ingestion_id = (payload.active_task_id or "").strip()
    if not ingestion_id:
        return ChatMessageResponse(
            session_id=payload.session_id,
            messages=[ChatToolMessage(role="assistant", content="请先上传 PDF/订单文件，我会把它作为 PDF 转 ERP 任务继续处理。")],
            active_task=ChatTaskState(type="pdf_to_erp", status="WAITING_UPLOAD"),
            ui=ToolUi(type="waiting_upload", data={}),
        )

    action = _infer_action(payload)
    if action == "get_status" and payload.message and not payload.fields:
        status_result = pdf_to_erp_tool.get_status(ingestion_id)
        if status_result is None:
            return _not_found(payload.session_id, ingestion_id)
        ingestion = status_result.ingestion
        parsed_fields = _extract_fields_from_text(payload.message, ingestion.missing_fields if ingestion else [])
        if parsed_fields:
            payload.fields.update(parsed_fields)
            action = "submit_missing_fields"

    if action == "cancel":
        result = pdf_to_erp_tool.get_status(ingestion_id)
        if result is None:
            return _not_found(payload.session_id, ingestion_id)
        result.message = "好的，当前不会上传 ERP。任务信息仍保留，之后你可以再回复“确认上传”。"
        result.status = "WAITING_CONFIRMATION" if result.status == "WAITING_CONFIRMATION" else result.status
        return _response(payload.session_id, result)

    if action == "submit_missing_fields":
        result = pdf_to_erp_tool.submit_missing_fields(ingestion_id, payload.fields)
    elif action == "confirm_preview":
        if payload.preview_data is None:
            result = pdf_to_erp_tool.get_status(ingestion_id)
            if result is not None:
                result.message = "需要订单预览数据后才能确认。"
        else:
            result = pdf_to_erp_tool.confirm_preview(ingestion_id, payload.preview_data)
    elif action == "create_draft":
        result = pdf_to_erp_tool.create_draft(ingestion_id)
        if result is None:
            status_result = pdf_to_erp_tool.get_status(ingestion_id)
            if status_result is None:
                return _not_found(payload.session_id, ingestion_id)
            status_result.message = "现在还不能上传 ERP，请先补齐必填字段并完成校验。"
            result = status_result
    else:
        result = pdf_to_erp_tool.get_status(ingestion_id)

    if result is None:
        return _not_found(payload.session_id, ingestion_id)
    return _response(payload.session_id, result)

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from app.schemas import (
    ConfirmPreviewRequest,
    CreateDraftResponse,
    ErrorCode,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    PreviewEditableField,
    ResolveIngestionRequest,
    ToolResult,
    ToolUi,
)
from app.store import (
    confirm_preview_for_ingestion,
    create_draft_for_ingestion,
    get_ingestion,
    resolve_ingestion,
)


PROCESSING_STATUSES = {
    IngestionStatus.UPLOADED,
    IngestionStatus.CLASSIFIED,
    IngestionStatus.PARSED,
    IngestionStatus.EXTRACTED,
    IngestionStatus.MAPPED,
}


FIELD_LABELS: Dict[str, str] = {
    "vendor_code": "供应商/客户编码",
    "doc_date": "单据日期",
    "currency": "币别",
    "material_code": "物料编码",
    "line_qty": "数量",
    "po_no": "采购订单号",
    "qty_received": "收货数量",
    "invoice_no": "发票号",
    "invoice_date": "发票日期",
    "warehouse_code": "仓库编码",
    "tax_code": "税码",
    "org": "销售组织",
    "customerName": "客户名称",
    "customerPoNo": "客户采购单号",
    "salesUser": "销售员",
    "delivery_date": "交货日期",
    "deliveryDate": "交货日期",
}


def _tool_status(status: IngestionStatus) -> str:
    if status in PROCESSING_STATUSES:
        return "PROCESSING"
    if status == IngestionStatus.NEED_USER_INPUT:
        return "NEED_USER_INPUT"
    if status == IngestionStatus.VALIDATED:
        return "WAITING_CONFIRMATION"
    if status == IngestionStatus.DRAFT_CREATED:
        return "DONE"
    if status == IngestionStatus.FAILED:
        return "FAILED"
    if status == IngestionStatus.CANCELED:
        return "CANCELED"
    return status.value


def _message_for_ingestion(ingestion: IngestionResponse) -> str:
    status = ingestion.status
    if status in PROCESSING_STATUSES:
        return "文件已进入处理流程，我会继续检查识别和映射进度。"
    if status == IngestionStatus.NEED_USER_INPUT:
        missing = "、".join(ingestion.missing_fields or [])
        return f"还需要补充这些字段：{missing}。" if missing else "还有必填信息需要补充。"
    if status == IngestionStatus.VALIDATED:
        return "订单信息已经校验通过，请确认是否上传到 ERP。"
    if status == IngestionStatus.DRAFT_CREATED:
        return f"已上传 ERP，草稿单号：{ingestion.draft_no or '暂无'}。"
    if status == IngestionStatus.FAILED:
        return f"处理失败：{ingestion.error_code or '未知错误'}。"
    if status == IngestionStatus.CANCELED:
        return "任务已取消。"
    return f"当前任务状态：{status.value}。"


def _field_rows(ingestion: IngestionResponse) -> List[Dict[str, object]]:
    editable_by_key: Dict[str, PreviewEditableField] = {}
    for field in ingestion.editable_fields or []:
        editable_by_key[field.path] = field
        editable_by_key[field.path.split(".")[-1]] = field

    keys: Iterable[str] = ingestion.missing_fields or ingestion.required_resolve_keys or []
    rows: List[Dict[str, object]] = []
    for key in keys:
        meta = editable_by_key.get(key)
        rows.append(
            {
                "key": key,
                "label": meta.label if meta else FIELD_LABELS.get(key, key),
                "current_value": (ingestion.resolved_fields or {}).get(key, meta.current_value if meta else ""),
                "required": True if meta is None else meta.required,
                "reason": meta.reason if meta else "该字段是创建 ERP 草稿前的必填项。",
            }
        )
    return rows


def _ui_for_ingestion(ingestion: IngestionResponse) -> ToolUi:
    base = {
        "ingestion_id": ingestion.ingestion_id,
        "status": ingestion.status.value,
        "tool_status": _tool_status(ingestion.status),
    }
    if ingestion.status in PROCESSING_STATUSES:
        return ToolUi(type="processing", data=base)
    if ingestion.status == IngestionStatus.NEED_USER_INPUT:
        return ToolUi(
            type="missing_fields_form",
            data={
                **base,
                "fields": _field_rows(ingestion),
                "preview_data": ingestion.preview_data.model_dump() if ingestion.preview_data else None,
            },
        )
    if ingestion.status == IngestionStatus.VALIDATED:
        return ToolUi(
            type="upload_confirm",
            data={
                **base,
                "preview_data": ingestion.preview_data.model_dump() if ingestion.preview_data else None,
                "editable_fields": [x.model_dump() for x in ingestion.editable_fields or []],
                "issues": [x.model_dump() for x in ingestion.issues or []],
            },
        )
    if ingestion.status == IngestionStatus.DRAFT_CREATED:
        return ToolUi(
            type="draft_result",
            data={**base, "draft_no": ingestion.draft_no, "draft_url": ingestion.draft_url},
        )
    if ingestion.status == IngestionStatus.FAILED:
        return ToolUi(
            type="error",
            data={**base, "error_code": ingestion.error_code, "error_details": ingestion.error_details},
        )
    return ToolUi(type="status", data=base)


def _result(ingestion: IngestionResponse, message: Optional[str] = None) -> ToolResult:
    return ToolResult(
        tool="pdf_to_erp",
        status=_tool_status(ingestion.status),
        message=message or _message_for_ingestion(ingestion),
        ingestion_id=ingestion.ingestion_id,
        ui=_ui_for_ingestion(ingestion),
        ingestion=ingestion,
    )


class PdfToErpTool:
    name = "pdf_to_erp"

    def get_status(self, ingestion_id: str) -> Optional[ToolResult]:
        ingestion = get_ingestion(ingestion_id)
        if not ingestion:
            return None
        return _result(ingestion)

    def submit_missing_fields(self, ingestion_id: str, fields: Dict[str, str]) -> Optional[ToolResult]:
        ingestion = resolve_ingestion(ingestion_id, ResolveIngestionRequest(fields=fields))
        if not ingestion:
            return None
        return _result(ingestion, "已保存补充信息，正在重新校验订单。")

    def confirm_preview(self, ingestion_id: str, preview_data: OrderPreviewData) -> Optional[ToolResult]:
        _ = ConfirmPreviewRequest(preview_data=preview_data)
        ingestion = confirm_preview_for_ingestion(ingestion_id, preview_data)
        if not ingestion:
            return None
        return _result(ingestion, "订单预览已确认，正在校验是否可以上传。")

    def create_draft(self, ingestion_id: str) -> Optional[ToolResult]:
        draft = create_draft_for_ingestion(ingestion_id)
        if not draft:
            return None
        ingestion = get_ingestion(ingestion_id)
        if not ingestion:
            return ToolResult(
                tool=self.name,
                status="DONE" if draft.status == IngestionStatus.DRAFT_CREATED else draft.status.value,
                message=f"ERP 草稿已创建：{draft.draft_no}",
                ingestion_id=ingestion_id,
                ui=ToolUi(type="draft_result", data=draft.model_dump()),
                draft=draft,
            )
        result = _result(ingestion, f"已上传 ERP，草稿单号：{draft.draft_no}。")
        result.draft = CreateDraftResponse.model_validate(draft)
        return result


pdf_to_erp_tool = PdfToErpTool()

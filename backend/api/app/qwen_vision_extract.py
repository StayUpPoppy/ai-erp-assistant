from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict, List, Optional
from urllib import error, request

from app.llm_extract import _append_llm_quality_issues, _extract_json, _purchase_order_to_preview
from app.order_preview import apply_preview_to_ingestion, preview_to_resolved_fields
from app.schemas import DocType, IngestionResponse, OrderPreviewData, OrderPreviewDetail, PreviewIssue, PurchaseOrder

logger = logging.getLogger("ai_erp_api")

QWEN_VISION_PROMPT_VERSION = "qwen-vision-order-preview-v1"


class QwenVisionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class VisionImage:
    bytes: bytes
    mime_type: str
    page_number: int


@dataclass(frozen=True)
class QwenVisionApplyResult:
    attempted: bool = False
    applied: bool = False
    reason: str = ""
    pages: int = 0
    images: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    summary_text: str = ""


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float((os.getenv(name) or str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def qwen_vision_enabled() -> bool:
    return _env_truthy("QWEN_VISION_EXTRACT_ENABLED", False)


def qwen_vision_force_all() -> bool:
    return _env_truthy("QWEN_VISION_FORCE_ALL", False)


def qwen_vision_fallback_to_local() -> bool:
    return _env_truthy("QWEN_VISION_FALLBACK_TO_LOCAL", True)


def qwen_vision_model_name() -> str:
    return (os.getenv("QWEN_VISION_MODEL") or "qwen3.7-plus").strip() or "qwen3.7-plus"


def qwen_vision_base_url() -> str:
    return (os.getenv("QWEN_VISION_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip().rstrip("/")


def qwen_vision_api_key_configured() -> bool:
    return bool((os.getenv("QWEN_VISION_API_KEY") or "").strip())


def qwen_vision_timeout_seconds() -> float:
    return _env_float("QWEN_VISION_TIMEOUT_SECONDS", 180.0, 5.0, 900.0)


def qwen_vision_max_pdf_pages() -> int:
    return _env_int("QWEN_VISION_MAX_PDF_PAGES", 10, 1, 50)


def qwen_vision_render_dpi() -> int:
    return _env_int("QWEN_VISION_RENDER_DPI", 180, 96, 300)


def qwen_vision_health_payload() -> Dict[str, Any]:
    return {
        "qwen_vision_extract_enabled": qwen_vision_enabled(),
        "qwen_vision_force_all": qwen_vision_force_all(),
        "qwen_vision_model": qwen_vision_model_name(),
        "qwen_vision_base_url": qwen_vision_base_url(),
        "qwen_vision_api_key_configured": qwen_vision_api_key_configured(),
        "qwen_vision_timeout_seconds": qwen_vision_timeout_seconds(),
        "qwen_vision_max_pdf_pages": qwen_vision_max_pdf_pages(),
        "qwen_vision_render_dpi": qwen_vision_render_dpi(),
        "qwen_vision_fallback_to_local": qwen_vision_fallback_to_local(),
    }


def _is_pdf(raw: bytes, file_name: str, content_type: Optional[str]) -> bool:
    return raw.lstrip()[:5] == b"%PDF-" or (content_type or "").lower() == "application/pdf" or file_name.lower().endswith(".pdf")


def _image_mime_type(raw: bytes, file_name: str, content_type: Optional[str]) -> str:
    declared = (content_type or "").split(";")[0].strip().lower()
    lower_name = file_name.lower()
    if raw.startswith(b"\xff\xd8") or declared in {"image/jpeg", "image/jpg"} or lower_name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n") or declared == "image/png" or lower_name.endswith(".png"):
        return "image/png"
    return ""


def is_qwen_vision_supported_file(raw: bytes, file_name: str, content_type: Optional[str] = None) -> bool:
    if not raw:
        return False
    return _is_pdf(raw, file_name, content_type) or bool(_image_mime_type(raw, file_name, content_type))


def should_defer_local_parse_for_qwen(raw: bytes, file_name: str, content_type: Optional[str] = None) -> bool:
    return (
        qwen_vision_enabled()
        and qwen_vision_force_all()
        and qwen_vision_api_key_configured()
        and is_qwen_vision_supported_file(raw, file_name, content_type)
    )


def _render_pdf_images(raw: bytes) -> tuple[List[VisionImage], int, bool]:
    try:
        import fitz
    except ImportError as exc:
        raise QwenVisionError("pymupdf_not_installed") from exc

    max_pages = qwen_vision_max_pdf_pages()
    dpi = qwen_vision_render_dpi()
    scale = dpi / 72.0
    images: List[VisionImage] = []
    doc = None
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        page_count = int(doc.page_count)
        for index in range(min(page_count, max_pages)):
            page = doc.load_page(index)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            images.append(VisionImage(bytes=pix.tobytes("jpeg"), mime_type="image/jpeg", page_number=index + 1))
        return images, page_count, page_count > max_pages
    except Exception as exc:
        raise QwenVisionError(f"pdf_render_failed:{type(exc).__name__}") from exc
    finally:
        if doc is not None:
            doc.close()


def _source_images(raw: bytes, file_name: str, content_type: Optional[str]) -> tuple[List[VisionImage], int, bool]:
    if _is_pdf(raw, file_name, content_type):
        return _render_pdf_images(raw)
    mime_type = _image_mime_type(raw, file_name, content_type)
    if not mime_type:
        raise QwenVisionError("unsupported_file_type")
    return [VisionImage(bytes=raw, mime_type=mime_type, page_number=1)], 1, False


VISION_SYSTEM_PROMPT = """你是制造业采购订单视觉抽取引擎。
只允许依据用户提供的 PDF 页面或图片内容抽取字段，不要猜测、不要补全、不要编造 ERP 主数据。
输出必须是一个严格 JSON object，不要 Markdown，不要解释文字。

重要规则：
- material_code 表示原始订单上的物料/料号/客户物料编码，必须保留原文，不要转换成 ERP 内部物料编码。
- 不要合并不同明细行；不要把表头、合计、备注、页脚当成物料行。
- 金额、单价、税率只有原图明确出现时才填写；无法确定填 0。
- 日期统一 YYYY-MM-DD；无法确定填空字符串。
- 对不确定字段写入 uncertain_fields，并尽量在 evidence 中给出页码和置信度。

输出 JSON schema：
{"purchase_order":{"order_number":"","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"","material_name":"","specification":"","material_texture":"","quantity":0,"unit":"","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}"""


def _user_text_prompt(file_name: str, page_count: int, truncated: bool) -> str:
    suffix = "。注意：只提供了前几页，请在 extraction_notes 中说明可能存在页数截断。" if truncated else ""
    return (
        f"请从这个订单文件中抽取结构化采购订单 JSON。\n"
        f"文件名：{file_name or 'upload'}\n"
        f"页数/图片数：{page_count}\n"
        f"{suffix}"
    )


def _chat_completion_vision(images: List[VisionImage], *, file_name: str, page_count: int, truncated: bool) -> str:
    api_key = (os.getenv("QWEN_VISION_API_KEY") or "").strip()
    if not api_key:
        raise QwenVisionError("missing_qwen_vision_api_key")

    content: List[Dict[str, Any]] = [{"type": "text", "text": _user_text_prompt(file_name, page_count, truncated)}]
    for image in images:
        data = base64.b64encode(image.bytes).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime_type};base64,{data}",
                },
            }
        )

    payload: Dict[str, Any] = {
        "model": qwen_vision_model_name(),
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": _env_int("LLM_MAX_TOKENS", 8192, 1024, 32768),
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{qwen_vision_base_url()}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with request.urlopen(req, timeout=qwen_vision_timeout_seconds()) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError as exc:
        # Do not log request payload: it contains document images as base64.
        raise QwenVisionError(f"qwen_vision_http_{exc.code}", status_code=exc.code) from exc
    except error.URLError as exc:
        raise QwenVisionError(f"qwen_vision_network_error:{exc.reason}") from exc
    except TimeoutError as exc:
        raise QwenVisionError("qwen_vision_timeout") from exc

    try:
        parsed = json.loads(raw)
        choice = parsed["choices"][0]
        content_value = choice["message"]["content"]
        if choice.get("finish_reason") == "length":
            raise QwenVisionError("qwen_vision_response_truncated")
        if isinstance(content_value, str):
            return content_value
        if isinstance(content_value, list):
            return "".join(str(part.get("text") or "") for part in content_value if isinstance(part, dict))
        return str(content_value)
    except QwenVisionError:
        raise
    except Exception as exc:
        raise QwenVisionError("qwen_vision_bad_response") from exc


def _parse_purchase_order(raw_content: str) -> PurchaseOrder:
    parsed = _extract_json(raw_content)
    if "purchase_order" in parsed and isinstance(parsed["purchase_order"], dict):
        parsed = parsed["purchase_order"]
    return PurchaseOrder.model_validate(parsed)


def _has_useful_detail(preview: OrderPreviewData) -> bool:
    for detail in preview.details or []:
        has_text = any(
            str(value or "").strip()
            for value in (detail.materialCode, detail.productName, detail.productSpec, detail.ph, detail.customerMaterialNo)
        )
        has_qty = detail.qty is not None and detail.qty > 0
        if has_text and has_qty:
            return True
    return False


def _assert_useful_preview(preview: OrderPreviewData) -> None:
    if not preview.details or not _has_useful_detail(preview):
        raise QwenVisionError("qwen_vision_empty_or_incomplete_details")


def preview_to_qwen_search_text(preview: OrderPreviewData) -> str:
    order = preview.order
    lines = [
        "Purchase Order",
        f"Customer: {order.customerName}",
        f"Order No.: {order.customerPoNo}",
        f"Order Date: {order.orderDate}",
        f"Currency: {order.currency}",
        f"Delivery Date: {order.deliveryDate}",
        f"Delivery Address: {order.deliveryAddr}",
    ]
    for index, detail in enumerate(preview.details or [], start=1):
        lines.append(
            " | ".join(
                [
                    str(index),
                    detail.materialCode or detail.customerMaterialNo,
                    detail.productName,
                    detail.productSpec,
                    detail.ph,
                    "" if detail.qty is None else str(detail.qty),
                    "" if detail.taxPrice is None else str(detail.taxPrice),
                    "" if detail.allAmount is None else str(detail.allAmount),
                    detail.remark,
                ]
            )
        )
    return "\n".join(line for line in lines if line.strip())


def _apply_purchase_order(
    ingestion: IngestionResponse,
    purchase_order: PurchaseOrder,
    *,
    truncated: bool,
) -> str:
    ingestion.doc_type_hint = DocType.PO
    preview = _purchase_order_to_preview(purchase_order, ingestion.org_id)
    _assert_useful_preview(preview)
    _append_llm_quality_issues(ingestion, purchase_order)
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
    ingestion.model_version = qwen_vision_model_name()
    ingestion.prompt_version = QWEN_VISION_PROMPT_VERSION
    if truncated:
        ingestion.issues.append(
            PreviewIssue(path="qwen_vision", level="warning", message="Qwen视觉抽取只处理了PDF前几页，请人工核对是否存在遗漏明细。")
        )
    return preview_to_qwen_search_text(preview)


def try_apply_qwen_vision_preview(
    ingestion: IngestionResponse,
    raw: bytes,
    file_name: str,
    content_type: Optional[str] = None,
) -> QwenVisionApplyResult:
    if not qwen_vision_enabled():
        return QwenVisionApplyResult(reason="disabled")
    if not qwen_vision_api_key_configured():
        return QwenVisionApplyResult(reason="missing_api_key")
    if not is_qwen_vision_supported_file(raw, file_name, content_type):
        return QwenVisionApplyResult(reason="unsupported_file_type")

    started = perf_counter()
    page_count = 0
    image_count = 0
    truncated = False
    try:
        images, page_count, truncated = _source_images(raw, file_name, content_type)
        image_count = len(images)
        if not images:
            raise QwenVisionError("no_images_to_send")
        content = _chat_completion_vision(images, file_name=file_name, page_count=page_count, truncated=truncated)
        purchase_order = _parse_purchase_order(content)
        summary_text = _apply_purchase_order(ingestion, purchase_order, truncated=truncated)
        elapsed_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "qwen_vision_preview_applied ingestion_id=%s model=%s pages=%s images=%s truncated=%s elapsed_ms=%s items=%s",
            ingestion.ingestion_id,
            qwen_vision_model_name(),
            page_count,
            image_count,
            int(truncated),
            elapsed_ms,
            len(purchase_order.items),
        )
        return QwenVisionApplyResult(
            attempted=True,
            applied=True,
            pages=page_count,
            images=image_count,
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            summary_text=summary_text,
        )
    except Exception as exc:
        elapsed_ms = int((perf_counter() - started) * 1000)
        reason = str(exc) or type(exc).__name__
        logger.warning(
            "qwen_vision_preview_failed ingestion_id=%s pages=%s images=%s truncated=%s elapsed_ms=%s reason=%s",
            ingestion.ingestion_id,
            page_count,
            image_count,
            int(truncated),
            elapsed_ms,
            reason,
        )
        return QwenVisionApplyResult(
            attempted=True,
            applied=False,
            reason=reason,
            pages=page_count,
            images=image_count,
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

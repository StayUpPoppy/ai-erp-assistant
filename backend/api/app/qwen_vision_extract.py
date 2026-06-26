from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict, List, Optional
from urllib import error, request

from app.llm_extract import _append_llm_quality_issues, _extract_json, _purchase_order_to_preview
from app.order_preview import apply_preview_to_ingestion, preview_to_resolved_fields
from app.schemas import DocType, IngestionResponse, OrderPreviewData, OrderPreviewDetail, PreviewIssue, PurchaseOrder

logger = logging.getLogger("ai_erp_api")

QWEN_VISION_PROMPT_VERSION = "qwen-vision-order-preview-v2"


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


def qwen_vision_include_local_text() -> bool:
    return _env_truthy("QWEN_VISION_INCLUDE_LOCAL_TEXT", True)


def qwen_vision_local_text_max_chars() -> int:
    return _env_int("QWEN_VISION_LOCAL_TEXT_MAX_CHARS", 12000, 0, 50000)


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
        "qwen_vision_include_local_text": qwen_vision_include_local_text(),
        "qwen_vision_local_text_max_chars": qwen_vision_local_text_max_chars(),
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


ORDER_EXTRACTION_SYSTEM_PROMPT = """你是制造业采购订单抽取引擎，只能依据用户提供的订单原文抽取字段，不能猜测、补全或编造。

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
- items[].specification：规格型号/型号/尺寸。注意规格型号中间的乘号(比如'或")要替换*。识别数字时候不要遗漏小数点。
- items[].material_texture：材质/牌号。
- items[].quantity：采购数量，只填数字。
- items[].unit：计量单位，如 件、PCS、KG、套。
- items[].unit_price_without_tax：不含税单价；只有原文明确包括不含税/未税/除税/净价等字样填写。不要把含税单价填到这里。识别数字时候不要遗漏小数点。
- items[].unit_price_with_tax：含税单价；只有原文明确为含税/价税合计单价时填写。不要把不含税单价填到这里。识别数字时候不要遗漏小数点。
- items[].total_amount_without_tax：单行不含税金额；只有原文明确给出或列名为不含税/未税金额时填写。识别数字时候不要遗漏小数点。
- items[].total_amount_with_tax：单行含税金额；只有原文明确给出或列名为含税金额/价税合计时填写。识别数字时候不要遗漏小数点。
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
- 识别数字时候不要遗漏小数点。

输出 JSON 结构：
{"purchase_order":{"order_number":"","purchaser_name":"","supplier_name":"","order_date":"","payment_terms":"","tax_rate":0,"delivery_address":"","total_order_amount":0,"items":[{"material_code":"","material_name":"","specification":"","material_texture":"","quantity":0,"unit":"","unit_price_without_tax":0,"unit_price_with_tax":0,"total_amount":0,"total_amount_without_tax":0,"total_amount_with_tax":0,"delivery_date":"","drawing_number":"","evidence":{},"uncertain_fields":[]}],"evidence":{},"uncertain_fields":[],"extraction_notes":[]}}""".strip()


VISION_SYSTEM_PROMPT = (
    ORDER_EXTRACTION_SYSTEM_PROMPT
    + """

视觉输入补充规则：
- 这次用户提供的是 PDF 页面截图或图片，不是已经 OCR 好的纯文本；请直接依据图像可见内容抽取。
- 客户名称 purchaser_name 和收货地址 delivery_address 如果同一位置同时出现中文和英文，只返回中文名称/中文地址，不要把英文拼接进去；如果原图没有中文证据，保持空字符串。
- material_code 表示原始订单上的物料/料号/客户物料编码，必须保留原文，不要转换成 ERP 内部物料编码。
- material_name 表示“产品名称/物料名称/品名/Product Name”列，必须保留该列完整原文，不要因为识别出规格或牌号就删减名称内容。
- specification 表示“规格型号/规格/型号/Spec/Specification”列；如果图片或本地解析文本中有这些列，必须优先按明细行使用该列，不要从“产品名称”列里截取型号当规格。
- material_texture 表示物料牌号/材质/材料牌号，对应列名包括“物料牌号”“牌号”“材质”“Material Grade”“Grade”“Alloy”“PH”；如果本地解析文本或图片表格中有这些列，必须按明细行逐行填写，不得留空。
- 物料名称、物料规格、物料牌号必须分开填写：例如“产品名称=弹簧，16CD25-ISO 27509 IX-RH，INCONEL X-750-NC；规格型号=ISO 27509 IX,右旋”时，material_name 保留完整产品名称，specification 填“ISO 27509 IX,右旋”。
- 不要合并不同明细行；不要把表头、合计、备注、页脚当成物料行。
- evidence.page 使用图片/PDF页码；confidence 按视觉识别可信度给 0-1。
""".strip()
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CHINESE_COMPANY_RE = re.compile(
    r"([\u4e00-\u9fff0-9０-９（）()·\-]{2,80}?(?:有限责任公司|股份有限公司|有限公司|公司|集团|厂))"
)
_HEADER_LABEL_RE = re.compile(
    r"(?:客户名称|客户|买方|需方|甲方|采购商|收货方|收货人|收货地址|客户地址|送货地址|交货地点|交货地址|地址)\s*[:：]?\s*"
)
_ADDRESS_CHUNK_RE = re.compile(
    r"[\u4e00-\u9fff0-9０-９\s（）()()#\-—、，,。.：:；;号省市区县镇乡村路街道巷段弄园楼室单元栋座厂]+"
)


def _clean_chinese_value(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = _HEADER_LABEL_RE.sub("", text)
    return text.strip(" \t\r\n|:：,，;；")


def _longest_cjk_chunk(value: str) -> str:
    candidates = []
    for chunk in _ADDRESS_CHUNK_RE.findall(value or ""):
        cleaned = _clean_chinese_value(chunk)
        if _CJK_RE.search(cleaned):
            candidates.append(cleaned)
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (len(re.sub(r"\s+", "", item)), len(item)))


def _prefer_chinese_company(value: str) -> str:
    text = _clean_chinese_value(value)
    if not _CJK_RE.search(text):
        return ""
    match = _CHINESE_COMPANY_RE.search(text)
    if match:
        return _clean_chinese_value(match.group(1))
    return _longest_cjk_chunk(text)


def _prefer_chinese_address(value: str) -> str:
    text = _clean_chinese_value(value)
    if not _CJK_RE.search(text):
        return ""
    return _longest_cjk_chunk(text)


def _prefer_chinese_header_fields(order: PurchaseOrder) -> PurchaseOrder:
    return order.model_copy(
        update={
            "purchaser_name": _prefer_chinese_company(order.purchaser_name),
            "delivery_address": _prefer_chinese_address(order.delivery_address),
        }
    )


def _clip_local_text(local_text: Optional[str]) -> str:
    if not qwen_vision_include_local_text():
        return ""
    text = (local_text or "").strip()
    if not text:
        return ""
    max_chars = qwen_vision_local_text_max_chars()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _user_text_prompt(
    file_name: str,
    page_count: int,
    truncated: bool,
    *,
    local_text: Optional[str] = None,
    local_format: Optional[str] = None,
) -> str:
    suffix = "。注意：只提供了前几页，请在 extraction_notes 中说明可能存在页数截断。" if truncated else ""
    prompt = (
        f"请从这个订单文件中抽取结构化采购订单 JSON。\n"
        f"文件名：{file_name or 'upload'}\n"
        f"页数/图片数：{page_count}\n"
        f"{suffix}"
    )
    clipped = _clip_local_text(local_text)
    if clipped:
        prompt += (
            "\n\n下面是系统从同一文件中预提取的文字/表格，仅作为辅助参考。"
            "如果它与图片可见内容冲突，以图片为准；不要输出这里没有图像证据的字段。\n"
            f"<local_parse_text format=\"{(local_format or '').strip()}\">\n"
            f"{clipped}\n"
            "</local_parse_text>"
        )
    return prompt


def _chat_completion_vision(
    images: List[VisionImage],
    *,
    file_name: str,
    page_count: int,
    truncated: bool,
    local_text: Optional[str] = None,
    local_format: Optional[str] = None,
) -> str:
    api_key = (os.getenv("QWEN_VISION_API_KEY") or "").strip()
    if not api_key:
        raise QwenVisionError("missing_qwen_vision_api_key")

    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": _user_text_prompt(
                file_name,
                page_count,
                truncated,
                local_text=local_text,
                local_format=local_format,
            ),
        }
    ]
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
    return _prefer_chinese_header_fields(PurchaseOrder.model_validate(parsed))


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


_GRADE_HEADER_CANDIDATES = {
    "物料牌号",
    "牌号",
    "材质",
    "材料牌号",
    "materialgrade",
    "material_grade",
    "grade",
    "alloy",
    "ph",
    "materialtexture",
    "material_texture",
}
_NAME_HEADER_CANDIDATES = {
    "产品名称",
    "物料名称",
    "品名",
    "名称",
    "productname",
    "name",
    "description",
}
_SPEC_HEADER_CANDIDATES = {
    "规格型号",
    "物料规格",
    "规格",
    "型号",
    "spec",
    "specification",
    "productspec",
    "product_spec",
}


@dataclass(frozen=True)
class _LocalDetailRow:
    product_name: str = ""
    product_name_present: bool = False
    product_spec: str = ""
    product_spec_present: bool = False
    material_grade: str = ""
    material_grade_present: bool = False

_GRADE_WITH_MATERIAL_RE = re.compile(
    r"(?P<prefix>^|[\s,，;；/|]+)(?P<grade>(?:INCONEL|INCOLOY|MONEL|HASTELLOY|NIMONIC)\s+[A-Z0-9]+(?:\s*-\s*[A-Z0-9-]+)+)\s*$",
    re.IGNORECASE,
)
_GRADE_TOKEN_RE = re.compile(
    r"(?P<prefix>^|[\s,，;；/|]+)(?P<grade>[A-Z]{1,6}\s*-\s*[A-Z0-9-]+)\s*$",
    re.IGNORECASE,
)


def _clean_qwen_grade(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = text.strip(" \t\r\n|,，;；:：")
    text = re.sub(r"\s*-\s*", "-", text)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-./ ]*", text):
        return text.upper()
    return text


def _clean_local_table_value(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).strip(" \t\r\n|,，;；:：")


def _clean_qwen_spec(value: str) -> str:
    text = _clean_local_table_value(value)
    if text.strip().lower() in {"", "-", "--", "—", "——", "/", "n/a", "na", "无"}:
        return ""
    return text


def _split_local_table_cells(line: str) -> List[str]:
    if "|" not in line:
        return []
    cells = [cell.strip() for cell in line.strip().split("|")]
    if cells and not cells[0]:
        cells = cells[1:]
    if cells and not cells[-1]:
        cells = cells[:-1]
    return cells


def _is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip())


def _normalise_header_cell(cell: str) -> str:
    return re.sub(r"[\s:：_\-/（）()]+", "", (cell or "").strip().lower())


def _column_index(cells: List[str], candidates: set[str], contains_candidates: tuple[str, ...] = ()) -> int:
    for index, cell in enumerate(cells):
        norm = _normalise_header_cell(cell)
        if norm in candidates:
            return index
        if any(candidate in norm for candidate in contains_candidates):
            return index
    return -1


def _grade_column_index(cells: List[str]) -> int:
    return _column_index(
        cells,
        _GRADE_HEADER_CANDIDATES,
        ("物料牌号", "材料牌号", "materialgrade", "materialtexture"),
    )


def _local_detail_rows_from_parse_text(local_text: Optional[str]) -> List[_LocalDetailRow]:
    rows: List[_LocalDetailRow] = []
    current_indices: tuple[int, int, int] | None = None
    expected_width = 0
    for line in (local_text or "").splitlines():
        cells = _split_local_table_cells(line)
        if not cells:
            current_indices = None
            expected_width = 0
            continue
        if _is_separator_row(cells):
            continue
        name_idx = _column_index(cells, _NAME_HEADER_CANDIDATES, ("产品名称", "物料名称", "productname"))
        spec_idx = _column_index(cells, _SPEC_HEADER_CANDIDATES, ("规格型号", "物料规格", "specification", "productspec"))
        grade_idx = _grade_column_index(cells)
        if name_idx >= 0 or spec_idx >= 0 or grade_idx >= 0:
            current_indices = (name_idx, spec_idx, grade_idx)
            expected_width = len(cells)
            continue
        if current_indices is None:
            continue
        max_index = max(current_indices)
        if max_index < 0 or len(cells) <= max_index:
            continue
        if expected_width and len(cells) < min(expected_width, max_index + 1):
            continue
        name_idx, spec_idx, grade_idx = current_indices
        product_name_present = name_idx >= 0 and len(cells) > name_idx
        product_spec_present = spec_idx >= 0 and len(cells) > spec_idx
        material_grade_present = grade_idx >= 0 and len(cells) > grade_idx
        row = _LocalDetailRow(
            product_name=_clean_local_table_value(cells[name_idx]) if product_name_present else "",
            product_name_present=product_name_present,
            product_spec=_clean_qwen_spec(cells[spec_idx]) if product_spec_present else "",
            product_spec_present=product_spec_present,
            material_grade=_clean_qwen_grade(cells[grade_idx]) if material_grade_present else "",
            material_grade_present=material_grade_present,
        )
        if any((row.product_name_present, row.product_spec_present, row.material_grade_present)):
            rows.append(row)
    return rows


def _grades_from_local_parse_text(local_text: Optional[str]) -> List[str]:
    return [row.material_grade for row in _local_detail_rows_from_parse_text(local_text) if row.material_grade]


def _split_grade_from_field(value: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        return "", ""
    match = _GRADE_WITH_MATERIAL_RE.search(text) or _GRADE_TOKEN_RE.search(text)
    if not match:
        return text, ""
    prefix_start = match.start("prefix")
    prefix_value = match.group("prefix") or ""
    if prefix_start == 0 and not prefix_value.strip():
        return text, ""
    grade = _clean_qwen_grade(match.group("grade"))
    remainder = (text[:prefix_start] + prefix_value.strip()).strip(" \t\r\n|,，;；:：")
    if not remainder:
        return text, ""
    return remainder, grade


def _repair_qwen_material_grades(preview: OrderPreviewData, local_text: Optional[str]) -> None:
    local_rows = _local_detail_rows_from_parse_text(_clip_local_text(local_text))
    for index, detail in enumerate(preview.details or []):
        local_row = local_rows[index] if index < len(local_rows) else None
        if local_row and local_row.product_name_present and local_row.product_name:
            detail.productName = local_row.product_name
        if local_row and local_row.product_spec_present:
            detail.productSpec = local_row.product_spec
        if (detail.ph or "").strip():
            detail.ph = _clean_qwen_grade(detail.ph)
            continue
        if local_row and local_row.material_grade_present and local_row.material_grade:
            detail.ph = local_row.material_grade
            continue
        spec, grade = _split_grade_from_field(detail.productSpec)
        if grade:
            detail.productSpec = spec
            detail.ph = grade
            continue
        name, grade = _split_grade_from_field(detail.productName)
        if grade:
            detail.ph = grade


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
    local_text: Optional[str] = None,
) -> str:
    ingestion.doc_type_hint = DocType.PO
    preview = _purchase_order_to_preview(purchase_order, ingestion.org_id)
    _repair_qwen_material_grades(preview, local_text)
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
    *,
    local_text: Optional[str] = None,
    local_format: Optional[str] = None,
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
        content = _chat_completion_vision(
            images,
            file_name=file_name,
            page_count=page_count,
            truncated=truncated,
            local_text=local_text,
            local_format=local_format,
        )
        purchase_order = _parse_purchase_order(content)
        summary_text = _apply_purchase_order(ingestion, purchase_order, truncated=truncated, local_text=local_text)
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

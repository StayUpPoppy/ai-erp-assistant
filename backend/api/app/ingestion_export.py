"""将 ingestion 解析结果封装为对外集成用的稳定 JSON 结构。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.document_extract import extract_text_from_bytes, resolved_upload_file_name
from app.schemas import IngestionResponse
from app.storage_client import get_object_bytes

# 全文明文导出上限（防超大 PDF/日志误传撑爆响应）
_MAX_FULL_TEXT_BYTES = 2 * 1024 * 1024
_MAX_FULL_TEXT_CHARS = 500_000


def build_document_parse_export(
    ing: IngestionResponse,
    *,
    include_full_text: bool = False,
) -> Dict[str, Any]:
    """
    组装 ``GET /ingestions/{id}/document`` 的响应体（dict，便于与 Pydantic 模型对齐）。
    """
    full_text: Optional[str] = None
    full_text_truncated = False
    if include_full_text and ing.source_file_object_key:
        raw = get_object_bytes(ing.source_file_object_key)
        if raw and len(raw) <= _MAX_FULL_TEXT_BYTES:
            name = resolved_upload_file_name(ing.source_file_object_key, ing.source_file_name)
            full_text, _fmt = extract_text_from_bytes(raw, name)
            if full_text and len(full_text) > _MAX_FULL_TEXT_CHARS:
                full_text = full_text[:_MAX_FULL_TEXT_CHARS]
                full_text_truncated = True
        elif raw and len(raw) > _MAX_FULL_TEXT_BYTES:
            full_text_truncated = True

    line_items: List[Dict[str, Any]] = []
    extracted = dict(ing.resolved_fields)
    lj = (extracted.pop("line_items_json", None) or "").strip()
    if lj:
        try:
            parsed = json.loads(lj)
            if isinstance(parsed, list):
                line_items = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return {
        "schema_version": "1.0",
        "ingestion_id": ing.ingestion_id,
        "file_id": ing.file_id,
        "file_hash": ing.file_hash,
        "org_id": ing.org_id,
        "user_id": ing.user_id,
        "status": ing.status.value,
        "doc_type_hint": ing.doc_type_hint.value if ing.doc_type_hint else None,
        "file": {
            "source_file_name": ing.source_file_name,
            "source_file_object_key": ing.source_file_object_key,
        },
        "parse": {
            "format_label": ing.parse_format_label,
            "char_count": ing.parsed_char_count,
            "text_preview": ing.extract_preview,
            "full_text": full_text,
            "full_text_truncated": full_text_truncated,
        },
        "extracted_fields": extracted,
        "line_items": line_items,
        "missing_required_fields": list(ing.missing_fields),
        "mapping_candidates": {
            "vendor": list(ing.vendor_candidates),
            "material": list(ing.material_candidates),
            "warehouse": list(ing.warehouse_candidates),
            "tax_code": list(ing.tax_code_candidates),
        },
        "versions": {
            "extract_version": ing.extract_version,
            "model_version": ing.model_version,
            "prompt_version": ing.prompt_version,
        },
        "error_code": ing.error_code,
        "error_details": ing.error_details,
    }

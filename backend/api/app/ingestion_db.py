"""
ingestion 与数据库行之间的转换与持久化辅助函数。

注意：调用方需自行管理事务边界（commit/rollback），本模块不隐式提交，
以便在 store 层用同一把锁串行化写操作时保持语义清晰。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction_profile import effective_required_field_keys, get_profile
from app.orm_models import IngestionRow
from app.schemas import (
    AuditEvent,
    DocType,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    PreviewEditableField,
    PreviewIssue,
)

logger = logging.getLogger("ai_erp_api")


def _coerce_erp_call_log(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[-30:]:
        if isinstance(item, dict):
            out.append(dict(item))
    return out


def _coerce_candidate_list(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw[:50]:
        if isinstance(item, dict):
            out.append({str(k): str(v) if v is not None else "" for k, v in item.items()})
    return out


def _audit_event_dump(event: AuditEvent) -> Dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump()
    return event.dict()


def row_to_ingestion(row: IngestionRow) -> IngestionResponse:
    """将数据库行还原为 API 对外返回的 Pydantic 模型。"""
    events_raw: List[Dict[str, Any]] = list(row.audit_events or [])
    audit_events = [AuditEvent(**e) for e in events_raw]
    ctx = dict(getattr(row, "ingestion_context", None) or {})
    extract_preview = ctx.get("extract_preview")
    if extract_preview is not None:
        extract_preview = str(extract_preview)
    parsed_raw = ctx.get("parsed_char_count")
    parsed_char_count = None
    if isinstance(parsed_raw, int):
        parsed_char_count = parsed_raw
    elif parsed_raw is not None:
        try:
            parsed_char_count = int(parsed_raw)
        except (TypeError, ValueError):
            parsed_char_count = None
    parse_format_label = ctx.get("parse_format_label")
    if parse_format_label is not None:
        parse_format_label = str(parse_format_label)
    else:
        parse_format_label = None
    source_file_name = ctx.get("source_file_name")
    if source_file_name is not None:
        source_file_name = str(source_file_name)
    else:
        source_file_name = None

    vendor_candidates = _coerce_candidate_list(ctx.get("vendor_candidates"))
    material_candidates = _coerce_candidate_list(ctx.get("material_candidates"))
    warehouse_candidates = _coerce_candidate_list(ctx.get("warehouse_candidates"))
    tax_code_candidates = _coerce_candidate_list(ctx.get("tax_code_candidates"))
    erp_call_log = _coerce_erp_call_log(ctx.get("erp_call_log"))
    preview_data = None
    if isinstance(ctx.get("preview_data"), dict):
        try:
            preview_data = OrderPreviewData.model_validate(ctx.get("preview_data"))
        except Exception:
            preview_data = None
    editable_fields: List[PreviewEditableField] = []
    if isinstance(ctx.get("editable_fields"), list):
        for item in ctx.get("editable_fields", []):
            if isinstance(item, dict):
                try:
                    editable_fields.append(PreviewEditableField.model_validate(item))
                except Exception:
                    continue
    issues: List[PreviewIssue] = []
    if isinstance(ctx.get("issues"), list):
        for item in ctx.get("issues", []):
            if isinstance(item, dict):
                try:
                    issues.append(PreviewIssue.model_validate(item))
                except Exception:
                    continue

    ext_prof = ctx.get("extraction_profile_id")
    extraction_profile_id = str(ext_prof).strip() if ext_prof else None
    req_raw = ctx.get("extraction_profile_requested")
    extraction_profile_requested = str(req_raw).strip() if req_raw else None
    res_raw = ctx.get("extraction_profile_resolution")
    extraction_profile_resolution = str(res_raw).strip() if res_raw else None
    rr_raw = ctx.get("required_resolve_keys")
    required_resolve_keys: List[str] = []
    if isinstance(rr_raw, list):
        required_resolve_keys = [str(x).strip() for x in rr_raw if str(x).strip()]

    doc_hint = DocType(row.doc_type_hint) if row.doc_type_hint else None
    if not required_resolve_keys and doc_hint:
        prof = get_profile(extraction_profile_id)
        required_resolve_keys = effective_required_field_keys(doc_hint.value, prof)

    return IngestionResponse(
        ingestion_id=row.ingestion_id,
        file_id=row.file_id,
        file_hash=row.file_hash,
        user_id=row.user_id,
        org_id=row.org_id,
        source_file_object_key=row.source_file_object_key,
        source_file_name=source_file_name,
        extract_version=row.extract_version,
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        status=IngestionStatus(row.status),
        doc_type_hint=doc_hint,
        extraction_profile_id=extraction_profile_id,
        extraction_profile_requested=extraction_profile_requested,
        extraction_profile_resolution=extraction_profile_resolution,
        required_resolve_keys=required_resolve_keys,
        missing_fields=list(row.missing_fields or []),
        resolved_fields=dict(row.resolved_fields or {}),
        audit_events=audit_events,
        draft_no=row.draft_no,
        draft_url=row.draft_url,
        error_code=row.error_code,
        error_details=dict(row.error_details or {}),
        extract_preview=extract_preview,
        parsed_char_count=parsed_char_count,
        parse_format_label=parse_format_label,
        vendor_candidates=vendor_candidates,
        material_candidates=material_candidates,
        warehouse_candidates=warehouse_candidates,
        tax_code_candidates=tax_code_candidates,
        erp_call_log=erp_call_log,
        preview_data=preview_data,
        editable_fields=editable_fields,
        issues=issues,
    )


def apply_ingestion_to_row(row: IngestionRow, ing: IngestionResponse) -> None:
    """把内存中的 ingestion 状态写回到 ORM 行对象（不执行 commit）。"""
    row.file_id = ing.file_id
    row.file_hash = ing.file_hash
    row.user_id = ing.user_id
    row.org_id = ing.org_id
    row.source_file_object_key = ing.source_file_object_key
    row.extract_version = ing.extract_version
    row.model_version = ing.model_version
    row.prompt_version = ing.prompt_version
    row.status = ing.status.value if isinstance(ing.status, IngestionStatus) else str(ing.status)
    row.doc_type_hint = ing.doc_type_hint.value if ing.doc_type_hint else None
    row.missing_fields = list(ing.missing_fields or [])
    row.resolved_fields = dict(ing.resolved_fields or {})
    row.audit_events = [_audit_event_dump(e) for e in (ing.audit_events or [])]
    row.draft_no = ing.draft_no
    row.draft_url = ing.draft_url
    row.error_code = ing.error_code
    row.error_details = dict(ing.error_details or {})
    ctx = dict(getattr(row, "ingestion_context", None) or {})
    if ing.extract_preview is None:
        ctx.pop("extract_preview", None)
    else:
        ctx["extract_preview"] = ing.extract_preview
    if ing.parsed_char_count is None:
        ctx.pop("parsed_char_count", None)
    else:
        ctx["parsed_char_count"] = ing.parsed_char_count
    if ing.parse_format_label is None:
        ctx.pop("parse_format_label", None)
    else:
        ctx["parse_format_label"] = ing.parse_format_label
    if ing.source_file_name is None:
        ctx.pop("source_file_name", None)
    else:
        ctx["source_file_name"] = ing.source_file_name
    if ing.vendor_candidates:
        ctx["vendor_candidates"] = [dict(x) for x in ing.vendor_candidates]
    else:
        ctx.pop("vendor_candidates", None)
    if ing.material_candidates:
        ctx["material_candidates"] = [dict(x) for x in ing.material_candidates]
    else:
        ctx.pop("material_candidates", None)
    if ing.warehouse_candidates:
        ctx["warehouse_candidates"] = [dict(x) for x in ing.warehouse_candidates]
    else:
        ctx.pop("warehouse_candidates", None)
    if ing.tax_code_candidates:
        ctx["tax_code_candidates"] = [dict(x) for x in ing.tax_code_candidates]
    else:
        ctx.pop("tax_code_candidates", None)
    if ing.erp_call_log:
        ctx["erp_call_log"] = [dict(x) for x in ing.erp_call_log][-30:]
    else:
        ctx.pop("erp_call_log", None)
    if ing.preview_data is not None:
        ctx["preview_data"] = ing.preview_data.model_dump()
    else:
        ctx.pop("preview_data", None)
    if ing.editable_fields:
        ctx["editable_fields"] = [x.model_dump() for x in ing.editable_fields]
    else:
        ctx.pop("editable_fields", None)
    if ing.issues:
        ctx["issues"] = [x.model_dump() for x in ing.issues]
    else:
        ctx.pop("issues", None)
    if ing.extraction_profile_id:
        ctx["extraction_profile_id"] = ing.extraction_profile_id
    else:
        ctx.pop("extraction_profile_id", None)
    if ing.extraction_profile_requested:
        ctx["extraction_profile_requested"] = ing.extraction_profile_requested
    else:
        ctx.pop("extraction_profile_requested", None)
    if ing.extraction_profile_resolution:
        ctx["extraction_profile_resolution"] = ing.extraction_profile_resolution
    else:
        ctx.pop("extraction_profile_resolution", None)
    if ing.required_resolve_keys:
        ctx["required_resolve_keys"] = list(ing.required_resolve_keys)
    else:
        ctx.pop("required_resolve_keys", None)
    row.ingestion_context = ctx


def new_row_from_ingestion(ing: IngestionResponse) -> IngestionRow:
    """从 ingestion 模型创建新 ORM 行（用于 insert）。"""
    row = IngestionRow(ingestion_id=ing.ingestion_id)
    apply_ingestion_to_row(row, ing)
    return row


def get_by_id(session: Session, ingestion_id: str) -> Optional[IngestionResponse]:
    row = session.get(IngestionRow, ingestion_id)
    if not row:
        logger.info("db_ingestion_not_found_by_id ingestion_id=%s", ingestion_id)
        return None
    logger.info("db_ingestion_loaded_by_id ingestion_id=%s status=%s", ingestion_id, row.status)
    return row_to_ingestion(row)


def get_by_file_hash(session: Session, file_hash: str) -> Optional[IngestionResponse]:
    stmt = select(IngestionRow).where(IngestionRow.file_hash == file_hash).limit(1)
    row = session.execute(stmt).scalars().first()
    if not row:
        logger.info("db_ingestion_not_found_by_hash file_hash_prefix=%s", file_hash[:12])
        return None
    logger.info(
        "db_ingestion_loaded_by_hash ingestion_id=%s file_hash_prefix=%s status=%s",
        row.ingestion_id,
        file_hash[:12],
        row.status,
    )
    return row_to_ingestion(row)


def get_by_file_hash_and_user_id(session: Session, file_hash: str, user_id: str) -> Optional[IngestionResponse]:
    stmt = (
        select(IngestionRow)
        .where(IngestionRow.file_hash == file_hash, IngestionRow.user_id == user_id)
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        logger.info("db_ingestion_not_found_by_hash_user file_hash_prefix=%s user_id=%s", file_hash[:12], user_id)
        return None
    logger.info(
        "db_ingestion_loaded_by_hash_user ingestion_id=%s file_hash_prefix=%s user_id=%s status=%s",
        row.ingestion_id,
        file_hash[:12],
        user_id,
        row.status,
    )
    return row_to_ingestion(row)


def upsert_ingestion(session: Session, ing: IngestionResponse) -> None:
    """按 ingestion_id upsert：存在则更新，不存在则插入。"""
    row = session.get(IngestionRow, ing.ingestion_id)
    if row is None:
        session.add(new_row_from_ingestion(ing))
        logger.info(
            "db_ingestion_inserted ingestion_id=%s status=%s file_hash_prefix=%s",
            ing.ingestion_id,
            ing.status.value if isinstance(ing.status, IngestionStatus) else str(ing.status),
            ing.file_hash[:12],
        )
        return
    apply_ingestion_to_row(row, ing)
    logger.info(
        "db_ingestion_updated ingestion_id=%s status=%s missing_fields=%s",
        ing.ingestion_id,
        ing.status.value if isinstance(ing.status, IngestionStatus) else str(ing.status),
        len(ing.missing_fields or []),
    )

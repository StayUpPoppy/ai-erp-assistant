from dataclasses import dataclass
from datetime import datetime
import logging
import os
from threading import Lock
from typing import Any, Dict, Optional
from uuid import uuid4

from app.database import SessionLocal, is_database_enabled
from app.erp_audit_log import append_erp_call_log, append_erp_call_log_with_upstream
from app.erp_client import ErpClientError, clear_last_upstream_meta, erp_client
from app import ingestion_db
from app.order_preview import (
    apply_preview_to_ingestion,
    build_order_preview_data,
    merge_non_empty,
    preview_missing_keys,
    preview_to_resolved_fields,
)
from app.workflow import run_ingestion_processing_workflow
from app.extraction_profile import (
    effective_required_field_keys,
    get_profile,
    refresh_ingestion_required_keys,
    resolve_extraction_profile,
)
from app.schemas import (
    AuditEvent,
    CreateIngestionRequest,
    CreateDraftResponse,
    DocType,
    ErrorCode,
    IngestionResponse,
    IngestionStatus,
    OrderPreviewData,
    ResolveIngestionRequest,
    UploadRequest,
)

logger = logging.getLogger("ai_erp_api")


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


def _effective_draft_doc_type(ingestion: IngestionResponse) -> str:
    if _should_force_datynk_sale_order_doc_type():
        return "PO"
    return ingestion.doc_type_hint.value if ingestion.doc_type_hint else "PO"


def _mark_datynk_sale_order_doc_type(ingestion: IngestionResponse) -> None:
    if _should_force_datynk_sale_order_doc_type() and ingestion.doc_type_hint != DocType.PO:
        ingestion.doc_type_hint = DocType.PO


def _append_erp_call_with_upstream(
    ingestion: IngestionResponse,
    base: Dict[str, Any],
    exc: Optional[ErpClientError] = None,
) -> None:
    """写入 erp_call_log，并合并最近一次 Real ERP HTTP 元数据（若有）。"""
    append_erp_call_log_with_upstream(ingestion, base, exc)


@dataclass
class InMemoryStore:
    # 当未配置 DATABASE_URL 时使用内存存储，便于本地零依赖开发。
    ingestions: Dict[str, IngestionResponse]
    file_hash_to_ingestion: Dict[str, str]
    lock: Lock


store = InMemoryStore(ingestions={}, file_hash_to_ingestion={}, lock=Lock())


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _merge_upload_payload_into_ingestion(ing: IngestionResponse, payload: CreateIngestionRequest) -> bool:
    """
    同 file_hash 命中幂等返回时：若本次请求带了新的 object_key（例如先前无 MinIO 未落盘，
    现已写入 __local__/ 或 MinIO），则更新记录，避免永远卡在 parse_skipped_no_bytes。
    """
    new_key = (payload.source_file_object_key or "").strip()
    if not new_key:
        return False
    old_key = (ing.source_file_object_key or "").strip()
    if old_key == new_key:
        return False
    ing.source_file_object_key = payload.source_file_object_key
    if payload.source_file_name and str(payload.source_file_name).strip():
        ing.source_file_name = str(payload.source_file_name).strip()
    return True


def _append_event(ingestion: IngestionResponse, status: IngestionStatus, message: str) -> None:
    # 统一的状态推进入口：每次状态变化都必须走这个方法。
    # 这样可以保证“状态值更新”和“审计事件追加”始终一起发生，
    # 避免出现状态变了但审计缺失、或审计有了但状态没改的分裂问题。
    ingestion.status = status
    ingestion.audit_events.append(AuditEvent(at=_now_iso(), status=status, message=message))


def append_ingestion_event(
    ingestion_id: str,
    status: IngestionStatus,
    message: str,
) -> Optional[IngestionResponse]:
    with store.lock:
        if is_database_enabled():
            session = _db_session()
            try:
                ingestion = ingestion_db.get_by_id(session, ingestion_id)
                if not ingestion:
                    logger.warning("append_ingestion_event_not_found ingestion_id=%s", ingestion_id)
                    return None
                _append_event(ingestion, status, message)
                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                return ingestion
            except Exception:
                session.rollback()
                logger.exception("append_ingestion_event_failed ingestion_id=%s storage=db", ingestion_id)
                raise
            finally:
                session.close()

        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("append_ingestion_event_not_found ingestion_id=%s", ingestion_id)
            return None
        _append_event(ingestion, status, message)
        store.ingestions[ingestion_id] = ingestion
        return ingestion


def _run_mock_pipeline(ingestion: IngestionResponse) -> None:
    # 这是本地模拟流水线方法，用于未接通完整异步链路时快速演示状态推进。
    # 目前主流程已经切到 worker 异步驱动，这个方法保留为调试与回归测试辅助。
    _append_event(ingestion, IngestionStatus.UPLOADED, "file metadata accepted")
    _append_event(ingestion, IngestionStatus.CLASSIFIED, "document classified to business type")
    _append_event(ingestion, IngestionStatus.PARSED, "document parsed and OCR completed")
    _append_event(ingestion, IngestionStatus.EXTRACTED, "structured fields extracted")
    _append_event(ingestion, IngestionStatus.MAPPED, "ERP master data mapping completed")
    _append_event(ingestion, IngestionStatus.NEED_USER_INPUT, "required fields missing, waiting user resolve")


def _new_ingestion_model(payload: CreateIngestionRequest, ingestion_id: str) -> IngestionResponse:
    """根据创建请求拼装 ingestion 内存对象（数据库与内存路径共用）。"""
    pick = resolve_extraction_profile(payload)
    prof = get_profile(pick.profile_id)
    dt = payload.doc_type_hint.value if payload.doc_type_hint else None
    required = effective_required_field_keys(dt, prof)
    return IngestionResponse(
        ingestion_id=ingestion_id,
        file_id=payload.file_id,
        file_hash=payload.file_hash,
        user_id=payload.user_id,
        org_id=payload.org_id,
        source_file_object_key=payload.source_file_object_key,
        source_file_name=payload.source_file_name,
        extract_version=payload.extract_version,
        model_version=payload.model_version,
        prompt_version=payload.prompt_version,
        status=IngestionStatus.UPLOADED,
        doc_type_hint=payload.doc_type_hint,
        extraction_profile_id=pick.profile_id,
        extraction_profile_requested=pick.requested_explicit,
        extraction_profile_resolution=pick.resolution,
        required_resolve_keys=list(required),
        missing_fields=list(required),
    )


def _reset_ingestion_for_reprocess(ingestion: IngestionResponse, payload: CreateIngestionRequest) -> IngestionResponse:
    fresh = _new_ingestion_model(payload, ingestion.ingestion_id)
    _append_event(fresh, IngestionStatus.UPLOADED, "file reprocess requested by user")
    return fresh


def _should_auto_reset_existing_ingestion(existing: IngestionResponse) -> bool:
    return existing.status == IngestionStatus.CANCELED


def _db_session():
    """获取一个新的同步 Session；仅在 DATABASE_URL 已配置时可用。"""
    assert SessionLocal is not None
    return SessionLocal()


def _map_erp_error_code(err_code: str) -> str:
    code = (err_code or "").upper()
    if code in {"MASTER_DATA_NOT_FOUND", "ERP_MASTER_DATA_NOT_FOUND"}:
        return ErrorCode.ERP_MASTER_DATA_NOT_FOUND.value
    if code in {"PERMISSION_DENIED", "ERP_PERMISSION_DENIED", "FORBIDDEN"}:
        return ErrorCode.ERP_PERMISSION_DENIED.value
    if code in {"UPSTREAM_TIMEOUT", "ERP_UPSTREAM_TIMEOUT", "TIMEOUT"}:
        return ErrorCode.ERP_UPSTREAM_TIMEOUT.value
    return ErrorCode.ERP_UPSTREAM_ERROR.value


def _build_erp_error_details(exc: ErpClientError) -> Dict[str, object]:
    raw = exc.details if isinstance(exc.details, dict) else {}
    field_errors = raw.get("violations", [])
    if not isinstance(field_errors, list):
        field_errors = []
    upstream_request_id = raw.get("request_id")
    if upstream_request_id is not None:
        upstream_request_id = str(upstream_request_id)
    category = "upstream_error"
    code = (exc.code or "").upper()
    if "TIMEOUT" in code:
        category = "timeout"
    elif code in {"MASTER_DATA_NOT_FOUND", "ERP_MASTER_DATA_NOT_FOUND"}:
        category = "master_data"
    elif code in {"PERMISSION_DENIED", "ERP_PERMISSION_DENIED", "FORBIDDEN"}:
        category = "permission"
    return {
        "category": category,
        "erp_error_code": exc.code,
        "erp_message": exc.message,
        "erp_status_code": exc.status_code,
        "upstream_request_id": upstream_request_id,
        "field_errors": field_errors,
        "raw": raw,
    }


def create_ingestion(payload: CreateIngestionRequest) -> IngestionResponse:
    with store.lock:
        logger.info("create_ingestion_started org_id=%s user_id=%s file_hash_prefix=%s", payload.org_id, payload.user_id, payload.file_hash[:12])
        if is_database_enabled():
            session = _db_session()
            try:
                # 使用 file_hash 做去重：数据库层同样以唯一约束保证幂等。
                existing = ingestion_db.get_by_file_hash(session, payload.file_hash)
                if existing:
                    if payload.force_reprocess or _should_auto_reset_existing_ingestion(existing):
                        existing = _reset_ingestion_for_reprocess(existing, payload)
                        ingestion_db.upsert_ingestion(session, existing)
                        session.commit()
                        logger.info(
                            "create_ingestion_reset_existing ingestion_id=%s status=%s file_hash_prefix=%s",
                            existing.ingestion_id,
                            existing.status,
                            payload.file_hash[:12],
                        )
                    elif _merge_upload_payload_into_ingestion(existing, payload):
                        ingestion_db.upsert_ingestion(session, existing)
                        session.commit()
                        logger.info(
                            "create_ingestion_idempotent_storage_merged ingestion_id=%s file_hash_prefix=%s",
                            existing.ingestion_id,
                            payload.file_hash[:12],
                        )
                    else:
                        logger.info(
                            "create_ingestion_idempotent_hit ingestion_id=%s file_hash_prefix=%s",
                            existing.ingestion_id,
                            payload.file_hash[:12],
                        )
                    return existing

                ingestion_id = str(uuid4())
                ingestion = _new_ingestion_model(payload, ingestion_id)
                _append_event(ingestion, IngestionStatus.UPLOADED, "file metadata accepted")
                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                logger.info("create_ingestion_succeeded ingestion_id=%s status=%s storage=db", ingestion.ingestion_id, ingestion.status)
                return ingestion
            except Exception:
                session.rollback()
                logger.exception("create_ingestion_failed storage=db file_hash_prefix=%s", payload.file_hash[:12])
                raise
            finally:
                session.close()

        existing_id = store.file_hash_to_ingestion.get(payload.file_hash)
        if existing_id:
            existing = store.ingestions[existing_id]
            if payload.force_reprocess or _should_auto_reset_existing_ingestion(existing):
                existing = _reset_ingestion_for_reprocess(existing, payload)
                store.ingestions[existing_id] = existing
                logger.info(
                    "create_ingestion_reset_existing ingestion_id=%s status=%s file_hash_prefix=%s",
                    existing.ingestion_id,
                    existing.status,
                    payload.file_hash[:12],
                )
            elif _merge_upload_payload_into_ingestion(existing, payload):
                logger.info(
                    "create_ingestion_idempotent_storage_merged ingestion_id=%s file_hash_prefix=%s",
                    existing.ingestion_id,
                    payload.file_hash[:12],
                )
            else:
                logger.info(
                    "create_ingestion_idempotent_hit ingestion_id=%s file_hash_prefix=%s",
                    existing.ingestion_id,
                    payload.file_hash[:12],
                )
            return existing

        ingestion_id = str(uuid4())
        ingestion = _new_ingestion_model(payload, ingestion_id)
        _append_event(ingestion, IngestionStatus.UPLOADED, "file metadata accepted")
        store.ingestions[ingestion_id] = ingestion
        store.file_hash_to_ingestion[payload.file_hash] = ingestion_id
        logger.info("create_ingestion_succeeded ingestion_id=%s status=%s storage=memory", ingestion.ingestion_id, ingestion.status)
        return ingestion


def create_upload(payload: UploadRequest) -> IngestionResponse:
    file_id = str(uuid4())
    ingestion_payload = CreateIngestionRequest(
        file_id=file_id,
        file_hash=payload.file_hash,
        user_id=payload.user_id,
        org_id=payload.org_id,
        source_file_object_key=payload.source_file_object_key,
        source_file_name=payload.file_name,
        extraction_profile_id=payload.extraction_profile_id,
        force_reprocess=payload.force_reprocess,
        extract_version=payload.extract_version,
        model_version=payload.model_version,
        prompt_version=payload.prompt_version,
    )
    return create_ingestion(ingestion_payload)


def get_ingestion(ingestion_id: str) -> Optional[IngestionResponse]:
    if is_database_enabled():
        session = _db_session()
        try:
            return ingestion_db.get_by_id(session, ingestion_id)
        finally:
            session.close()
    with store.lock:
        return store.ingestions.get(ingestion_id)


def _refresh_preview_from_resolved_fields(ingestion: IngestionResponse) -> None:
    existing_preview = ingestion.preview_data
    ingestion.preview_data = None
    preview = build_order_preview_data(ingestion)
    ingestion.preview_data = existing_preview
    if preview is not None:
        apply_preview_to_ingestion(ingestion, preview)


def confirm_preview_for_ingestion(ingestion_id: str, preview_data: OrderPreviewData) -> Optional[IngestionResponse]:
    with store.lock:
        logger.info("confirm_preview_started ingestion_id=%s details=%s", ingestion_id, len(preview_data.details))
        preview_fields = preview_to_resolved_fields(preview_data)
        preview_missing = preview_missing_keys(preview_data)
        if is_database_enabled():
            session = _db_session()
            try:
                ingestion = ingestion_db.get_by_id(session, ingestion_id)
                if not ingestion:
                    logger.warning("confirm_preview_not_found ingestion_id=%s", ingestion_id)
                    return None
                _mark_datynk_sale_order_doc_type(ingestion)
                ingestion.preview_data = preview_data
                ingestion.resolved_fields = merge_non_empty(ingestion.resolved_fields, preview_fields)
                apply_preview_to_ingestion(ingestion, preview_data)
                ingestion.missing_fields = list(preview_missing)
                if preview_missing:
                    ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
                    _append_event(
                        ingestion,
                        IngestionStatus.NEED_USER_INPUT,
                        f"preview_confirmed_but_missing_fields missing={','.join(preview_missing)}",
                    )
                else:
                    clear_last_upstream_meta()
                    try:
                        valid, missing = erp_client.validate_draft(
                            _effective_draft_doc_type(ingestion),
                            dict(ingestion.resolved_fields),
                            required_keys=ingestion.required_resolve_keys or None,
                        )
                    except ErpClientError as exc:
                        ingestion.error_code = _map_erp_error_code(exc.code)
                        ingestion.error_details = _build_erp_error_details(exc)
                        _append_event(
                            ingestion,
                            IngestionStatus.FAILED,
                            f"preview_confirm_validate_failed code={exc.code} message={exc.message}",
                        )
                        ingestion_db.upsert_ingestion(session, ingestion)
                        session.commit()
                        return ingestion
                    ingestion.missing_fields = list(missing)
                    if valid:
                        ingestion.error_code = None
                        _append_event(ingestion, IngestionStatus.VALIDATED, "preview confirmed and ERP validate passed")
                    else:
                        ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
                        _append_event(
                            ingestion,
                            IngestionStatus.NEED_USER_INPUT,
                            f"preview_confirmed_but_validate_missing missing={','.join(missing)}",
                        )
                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                return ingestion
            except Exception:
                session.rollback()
                logger.exception("confirm_preview_failed ingestion_id=%s storage=db", ingestion_id)
                raise
            finally:
                session.close()

        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("confirm_preview_not_found ingestion_id=%s", ingestion_id)
            return None
        _mark_datynk_sale_order_doc_type(ingestion)
        ingestion.preview_data = preview_data
        ingestion.resolved_fields = merge_non_empty(ingestion.resolved_fields, preview_fields)
        apply_preview_to_ingestion(ingestion, preview_data)
        ingestion.missing_fields = list(preview_missing)
        if preview_missing:
            ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
            _append_event(
                ingestion,
                IngestionStatus.NEED_USER_INPUT,
                f"preview_confirmed_but_missing_fields missing={','.join(preview_missing)}",
            )
        else:
            clear_last_upstream_meta()
            try:
                valid, missing = erp_client.validate_draft(
                    _effective_draft_doc_type(ingestion),
                    dict(ingestion.resolved_fields),
                    required_keys=ingestion.required_resolve_keys or None,
                )
            except ErpClientError as exc:
                ingestion.error_code = _map_erp_error_code(exc.code)
                ingestion.error_details = _build_erp_error_details(exc)
                _append_event(
                    ingestion,
                    IngestionStatus.FAILED,
                    f"preview_confirm_validate_failed code={exc.code} message={exc.message}",
                )
                store.ingestions[ingestion_id] = ingestion
                return ingestion
            ingestion.missing_fields = list(missing)
            if valid:
                ingestion.error_code = None
                _append_event(ingestion, IngestionStatus.VALIDATED, "preview confirmed and ERP validate passed")
            else:
                ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
                _append_event(
                    ingestion,
                    IngestionStatus.NEED_USER_INPUT,
                    f"preview_confirmed_but_validate_missing missing={','.join(missing)}",
                )
        store.ingestions[ingestion_id] = ingestion
        return ingestion


def process_ingestion(ingestion_id: str) -> Optional[IngestionResponse]:
    logger.info("process_ingestion_started ingestion_id=%s", ingestion_id)
    if is_database_enabled():
        session = _db_session()
        try:
            ingestion = ingestion_db.get_by_id(session, ingestion_id)
            if not ingestion:
                logger.warning("process_ingestion_not_found ingestion_id=%s", ingestion_id)
                return None
            if ingestion.status != IngestionStatus.UPLOADED:
                logger.info("process_ingestion_skip_status ingestion_id=%s status=%s", ingestion_id, ingestion.status)
                return ingestion

            run_ingestion_processing_workflow(ingestion=ingestion, erp=erp_client, append_event=_append_event)

            session.expire_all()
            latest = ingestion_db.get_by_id(session, ingestion_id)
            if latest and latest.status != IngestionStatus.UPLOADED:
                logger.info("process_ingestion_skip_after_workflow ingestion_id=%s status=%s storage=db", ingestion_id, latest.status)
                return latest

            ingestion_db.upsert_ingestion(session, ingestion)
            session.commit()
            logger.info("process_ingestion_succeeded ingestion_id=%s status=%s storage=db", ingestion_id, ingestion.status)
            return ingestion
        except Exception:
            session.rollback()
            logger.exception("process_ingestion_failed ingestion_id=%s storage=db", ingestion_id)
            raise
        finally:
            session.close()

    with store.lock:
        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("process_ingestion_not_found ingestion_id=%s", ingestion_id)
            return None
        if ingestion.status != IngestionStatus.UPLOADED:
            logger.info("process_ingestion_skip_status ingestion_id=%s status=%s", ingestion_id, ingestion.status)
            return ingestion
        working = ingestion.model_copy(deep=True)

    run_ingestion_processing_workflow(ingestion=working, erp=erp_client, append_event=_append_event)

    with store.lock:
        current = store.ingestions.get(ingestion_id)
        if current is None:
            logger.warning("process_ingestion_lost_in_memory_record ingestion_id=%s", ingestion_id)
            return None
        if current.status == IngestionStatus.UPLOADED:
            store.ingestions[ingestion_id] = working
            current = working
        logger.info("process_ingestion_succeeded ingestion_id=%s status=%s storage=memory", ingestion_id, current.status)
        return current


def cancel_ingestion(ingestion_id: str, reason: str = "canceled by user") -> Optional[IngestionResponse]:
    with store.lock:
        if is_database_enabled():
            session = _db_session()
            try:
                ingestion = ingestion_db.get_by_id(session, ingestion_id)
                if not ingestion:
                    logger.warning("cancel_ingestion_not_found ingestion_id=%s", ingestion_id)
                    return None
                if ingestion.status in {IngestionStatus.DRAFT_CREATED, IngestionStatus.FAILED, IngestionStatus.CANCELED}:
                    return ingestion
                ingestion.error_code = ErrorCode.INGESTION_CANCELED.value
                ingestion.error_details = {"reason": reason}
                _append_event(ingestion, IngestionStatus.CANCELED, reason)
                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                logger.info("cancel_ingestion_succeeded ingestion_id=%s storage=db", ingestion_id)
                return ingestion
            except Exception:
                session.rollback()
                logger.exception("cancel_ingestion_failed ingestion_id=%s storage=db", ingestion_id)
                raise
            finally:
                session.close()

        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("cancel_ingestion_not_found ingestion_id=%s", ingestion_id)
            return None
        if ingestion.status in {IngestionStatus.DRAFT_CREATED, IngestionStatus.FAILED, IngestionStatus.CANCELED}:
            return ingestion
        ingestion.error_code = ErrorCode.INGESTION_CANCELED.value
        ingestion.error_details = {"reason": reason}
        _append_event(ingestion, IngestionStatus.CANCELED, reason)
        store.ingestions[ingestion_id] = ingestion
        logger.info("cancel_ingestion_succeeded ingestion_id=%s storage=memory", ingestion_id)
        return ingestion


def resolve_ingestion(ingestion_id: str, payload: ResolveIngestionRequest) -> Optional[IngestionResponse]:
    with store.lock:
        logger.info("resolve_ingestion_started ingestion_id=%s input_fields=%s", ingestion_id, len(payload.fields))
        if is_database_enabled():
            session = _db_session()
            try:
                ingestion = ingestion_db.get_by_id(session, ingestion_id)
                if not ingestion:
                    logger.warning("resolve_ingestion_not_found ingestion_id=%s", ingestion_id)
                    return None

                merged_fields = {**ingestion.resolved_fields, **payload.fields}
                doc_type = _effective_draft_doc_type(ingestion)
                clear_last_upstream_meta()
                refresh_ingestion_required_keys(ingestion)
                req_keys = ingestion.required_resolve_keys or None
                try:
                    valid, missing = erp_client.validate_draft(doc_type, merged_fields, required_keys=req_keys)
                except ErpClientError as exc:
                    _append_erp_call_with_upstream(
                        ingestion,
                        {
                            "at": _now_iso(),
                            "operation": "validate_draft",
                            "doc_type": doc_type,
                            "ok": False,
                            "erp_error_code": exc.code,
                        },
                        exc=exc,
                    )
                    ingestion.error_code = _map_erp_error_code(exc.code)
                    ingestion.error_details = _build_erp_error_details(exc)
                    _append_event(
                        ingestion,
                        IngestionStatus.FAILED,
                        f"erp_validate_failed code={exc.code} message={exc.message}",
                    )
                    ingestion_db.upsert_ingestion(session, ingestion)
                    session.commit()
                    logger.error(
                        "resolve_ingestion_erp_failed ingestion_id=%s erp_code=%s mapped_error=%s storage=db",
                        ingestion_id,
                        exc.code,
                        ingestion.error_code,
                    )
                    return ingestion
                ingestion.resolved_fields = merged_fields
                _refresh_preview_from_resolved_fields(ingestion)
                ingestion.missing_fields = missing
                ingestion.error_code = None
                ingestion.error_details = {}
                _append_erp_call_with_upstream(
                    ingestion,
                    {
                        "at": _now_iso(),
                        "operation": "validate_draft",
                        "doc_type": doc_type,
                        "ok": valid,
                        "missing_fields": list(missing),
                    },
                )
                keys_note = ",".join(sorted(payload.fields.keys()))
                if valid:
                    _append_event(
                        ingestion,
                        IngestionStatus.VALIDATED,
                        f"required fields completed and ERP validate passed user_submitted_keys={keys_note}",
                    )
                else:
                    _append_event(
                        ingestion,
                        IngestionStatus.NEED_USER_INPUT,
                        f"still missing required fields user_submitted_keys={keys_note}",
                    )

                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                logger.info("resolve_ingestion_succeeded ingestion_id=%s status=%s missing_fields=%s storage=db", ingestion_id, ingestion.status, len(ingestion.missing_fields))
                return ingestion
            except Exception:
                session.rollback()
                logger.exception("resolve_ingestion_failed ingestion_id=%s storage=db", ingestion_id)
                raise
            finally:
                session.close()

        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("resolve_ingestion_not_found ingestion_id=%s", ingestion_id)
            return None

        merged_fields = {**ingestion.resolved_fields, **payload.fields}
        doc_type = _effective_draft_doc_type(ingestion)
        clear_last_upstream_meta()
        refresh_ingestion_required_keys(ingestion)
        req_keys = ingestion.required_resolve_keys or None
        try:
            valid, missing = erp_client.validate_draft(doc_type, merged_fields, required_keys=req_keys)
        except ErpClientError as exc:
            _append_erp_call_with_upstream(
                ingestion,
                {
                    "at": _now_iso(),
                    "operation": "validate_draft",
                    "doc_type": doc_type,
                    "ok": False,
                    "erp_error_code": exc.code,
                },
                exc=exc,
            )
            ingestion.error_code = _map_erp_error_code(exc.code)
            ingestion.error_details = _build_erp_error_details(exc)
            _append_event(
                ingestion,
                IngestionStatus.FAILED,
                f"erp_validate_failed code={exc.code} message={exc.message}",
            )
            store.ingestions[ingestion_id] = ingestion
            logger.error(
                "resolve_ingestion_erp_failed ingestion_id=%s erp_code=%s mapped_error=%s storage=memory",
                ingestion_id,
                exc.code,
                ingestion.error_code,
            )
            return ingestion
        ingestion.resolved_fields = merged_fields
        _refresh_preview_from_resolved_fields(ingestion)
        ingestion.missing_fields = missing
        ingestion.error_code = None
        ingestion.error_details = {}
        _append_erp_call_with_upstream(
            ingestion,
            {
                "at": _now_iso(),
                "operation": "validate_draft",
                "doc_type": doc_type,
                "ok": valid,
                "missing_fields": list(missing),
            },
        )
        keys_note = ",".join(sorted(payload.fields.keys()))
        if valid:
            _append_event(
                ingestion,
                IngestionStatus.VALIDATED,
                f"required fields completed and ERP validate passed user_submitted_keys={keys_note}",
            )
        else:
            _append_event(
                ingestion,
                IngestionStatus.NEED_USER_INPUT,
                f"still missing required fields user_submitted_keys={keys_note}",
            )
        store.ingestions[ingestion_id] = ingestion
        logger.info("resolve_ingestion_succeeded ingestion_id=%s status=%s missing_fields=%s storage=memory", ingestion_id, ingestion.status, len(ingestion.missing_fields))
        return ingestion


def create_draft_for_ingestion(ingestion_id: str) -> Optional[CreateDraftResponse]:
    with store.lock:
        logger.info("create_draft_started ingestion_id=%s", ingestion_id)
        if is_database_enabled():
            session = _db_session()
            try:
                ingestion = ingestion_db.get_by_id(session, ingestion_id)
                if not ingestion:
                    logger.warning("create_draft_not_found ingestion_id=%s", ingestion_id)
                    return None

                if ingestion.missing_fields:
                    _append_event(ingestion, IngestionStatus.NEED_USER_INPUT, "draft creation blocked by missing fields")
                    ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
                    ingestion_db.upsert_ingestion(session, ingestion)
                    session.commit()
                    logger.warning("create_draft_blocked_missing_fields ingestion_id=%s missing_fields=%s", ingestion_id, len(ingestion.missing_fields))
                    return None

                doc_type = _effective_draft_doc_type(ingestion)
                idempotency_key = f"{ingestion.org_id}:{ingestion.file_hash}:{doc_type}"

                if ingestion.draft_no and ingestion.draft_url:
                    # 幂等重放：数据库中已存在草稿号，不再重复写入 ERP。
                    logger.info("create_draft_idempotent_replay ingestion_id=%s draft_no=%s", ingestion_id, ingestion.draft_no)
                    if ingestion.status != IngestionStatus.DRAFT_CREATED:
                        _append_event(
                            ingestion,
                            IngestionStatus.DRAFT_CREATED,
                            f"ERP draft already exists draft_no={ingestion.draft_no} idempotency_key={idempotency_key} doc_type={doc_type}",
                        )
                    append_erp_call_log(
                        ingestion,
                        {
                            "at": _now_iso(),
                            "operation": "create_draft",
                            "doc_type": doc_type,
                            "result": "idempotent_replay",
                            "draft_no": ingestion.draft_no,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    ingestion_db.upsert_ingestion(session, ingestion)
                    session.commit()
                    return CreateDraftResponse(
                        ingestion_id=ingestion_id,
                        status=ingestion.status,
                        draft_no=ingestion.draft_no,
                        draft_url=ingestion.draft_url,
                        idempotency_key=idempotency_key,
                    )

                clear_last_upstream_meta()
                try:
                    draft_no, draft_url = erp_client.create_draft(doc_type, ingestion.resolved_fields, idempotency_key)
                except ErpClientError as exc:
                    _append_erp_call_with_upstream(
                        ingestion,
                        {
                            "at": _now_iso(),
                            "operation": "create_draft",
                            "doc_type": doc_type,
                            "ok": False,
                            "erp_error_code": exc.code,
                            "idempotency_key": idempotency_key,
                        },
                        exc=exc,
                    )
                    ingestion.error_code = _map_erp_error_code(exc.code)
                    ingestion.error_details = _build_erp_error_details(exc)
                    _append_event(
                        ingestion,
                        IngestionStatus.FAILED,
                        f"erp_create_draft_failed code={exc.code} message={exc.message}",
                    )
                    ingestion_db.upsert_ingestion(session, ingestion)
                    session.commit()
                    logger.error(
                        "create_draft_erp_failed ingestion_id=%s erp_code=%s mapped_error=%s storage=db",
                        ingestion_id,
                        exc.code,
                        ingestion.error_code,
                    )
                    return None
                _append_erp_call_with_upstream(
                    ingestion,
                    {
                        "at": _now_iso(),
                        "operation": "create_draft",
                        "doc_type": doc_type,
                        "ok": True,
                        "draft_no": draft_no,
                        "idempotency_key": idempotency_key,
                    },
                )
                _append_event(
                    ingestion,
                    IngestionStatus.DRAFT_CREATED,
                    f"ERP draft created draft_no={draft_no} idempotency_key={idempotency_key} doc_type={doc_type}",
                )
                ingestion.draft_no = draft_no
                ingestion.draft_url = draft_url
                ingestion.error_code = None
                ingestion.error_details = {}
                ingestion_db.upsert_ingestion(session, ingestion)
                session.commit()
                logger.info("create_draft_succeeded ingestion_id=%s draft_no=%s storage=db", ingestion_id, draft_no)
                return CreateDraftResponse(
                    ingestion_id=ingestion_id,
                    status=ingestion.status,
                    draft_no=draft_no,
                    draft_url=draft_url,
                    idempotency_key=idempotency_key,
                )
            except Exception:
                session.rollback()
                logger.exception("create_draft_failed ingestion_id=%s storage=db", ingestion_id)
                raise
            finally:
                session.close()

        ingestion = store.ingestions.get(ingestion_id)
        if not ingestion:
            logger.warning("create_draft_not_found ingestion_id=%s", ingestion_id)
            return None

        if ingestion.missing_fields:
            _append_event(ingestion, IngestionStatus.NEED_USER_INPUT, "draft creation blocked by missing fields")
            ingestion.error_code = ErrorCode.MISSING_REQUIRED_FIELDS.value
            store.ingestions[ingestion_id] = ingestion
            logger.warning("create_draft_blocked_missing_fields ingestion_id=%s missing_fields=%s", ingestion_id, len(ingestion.missing_fields))
            return None

        doc_type = _effective_draft_doc_type(ingestion)
        idempotency_key = f"{ingestion.org_id}:{ingestion.file_hash}:{doc_type}"

        if ingestion.draft_no and ingestion.draft_url:
            logger.info("create_draft_idempotent_replay ingestion_id=%s draft_no=%s", ingestion_id, ingestion.draft_no)
            if ingestion.status != IngestionStatus.DRAFT_CREATED:
                _append_event(
                    ingestion,
                    IngestionStatus.DRAFT_CREATED,
                    f"ERP draft already exists draft_no={ingestion.draft_no} idempotency_key={idempotency_key} doc_type={doc_type}",
                )
            append_erp_call_log(
                ingestion,
                {
                    "at": _now_iso(),
                    "operation": "create_draft",
                    "doc_type": doc_type,
                    "result": "idempotent_replay",
                    "draft_no": ingestion.draft_no,
                    "idempotency_key": idempotency_key,
                },
            )
            store.ingestions[ingestion_id] = ingestion
            return CreateDraftResponse(
                ingestion_id=ingestion_id,
                status=ingestion.status,
                draft_no=ingestion.draft_no,
                draft_url=ingestion.draft_url,
                idempotency_key=idempotency_key,
            )

        clear_last_upstream_meta()
        try:
            draft_no, draft_url = erp_client.create_draft(doc_type, ingestion.resolved_fields, idempotency_key)
        except ErpClientError as exc:
            _append_erp_call_with_upstream(
                ingestion,
                {
                    "at": _now_iso(),
                    "operation": "create_draft",
                    "doc_type": doc_type,
                    "ok": False,
                    "erp_error_code": exc.code,
                    "idempotency_key": idempotency_key,
                },
                exc=exc,
            )
            ingestion.error_code = _map_erp_error_code(exc.code)
            ingestion.error_details = _build_erp_error_details(exc)
            _append_event(
                ingestion,
                IngestionStatus.FAILED,
                f"erp_create_draft_failed code={exc.code} message={exc.message}",
            )
            store.ingestions[ingestion_id] = ingestion
            logger.error(
                "create_draft_erp_failed ingestion_id=%s erp_code=%s mapped_error=%s storage=memory",
                ingestion_id,
                exc.code,
                ingestion.error_code,
            )
            return None
        _append_erp_call_with_upstream(
            ingestion,
            {
                "at": _now_iso(),
                "operation": "create_draft",
                "doc_type": doc_type,
                "ok": True,
                "draft_no": draft_no,
                "idempotency_key": idempotency_key,
            },
        )
        _append_event(
            ingestion,
            IngestionStatus.DRAFT_CREATED,
            f"ERP draft created draft_no={draft_no} idempotency_key={idempotency_key} doc_type={doc_type}",
        )
        ingestion.draft_no = draft_no
        ingestion.draft_url = draft_url
        ingestion.error_code = None
        ingestion.error_details = {}
        store.ingestions[ingestion_id] = ingestion
        logger.info("create_draft_succeeded ingestion_id=%s draft_no=%s storage=memory", ingestion_id, draft_no)
        return CreateDraftResponse(
            ingestion_id=ingestion_id,
            status=ingestion.status,
            draft_no=draft_no,
            draft_url=draft_url,
            idempotency_key=idempotency_key,
        )

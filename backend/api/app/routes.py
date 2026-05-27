import hashlib
import json
import logging
from threading import Thread

from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.erp_client import ErpClientError, clear_last_upstream_meta, erp_adapter_health_payload, erp_client
from app.erp_payload_preview import build_datynk_sale_order_payload
from app.erp_qa import answer_with_erp_tools
from app.erp_qa_reports import erp_qa_reports_health_payload
from app.document_extract import tesseract_health_payload
from app.extraction_profile import profiles_directory_stats
from app.assistant_orchestrator import handle_assistant_message, handle_assistant_route_decision
from app.assistant_llm_router import (
    AssistantRouteDecision,
    assistant_llm_router_enabled,
    decide_with_llm,
    probe_llm_router,
    should_use_plain_chat_fast_path,
    stream_assistant_answer_with_llm,
)
from app.assistant_session_store import append_response, append_user_message, ensure_session_id, get_session_response
from app.chat_orchestrator import handle_chat_message
from app.ingestion_export import build_document_parse_export
from app.llm_client import LlmClientError, llm_api_key_configured, llm_base_url, llm_extract_enabled, llm_model_name, llm_prompt_version
from app.schemas import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatTaskState,
    ChatToolMessage,
    AssistantLlmProbeRequest,
    AssistantLlmProbeResponse,
    AssistantSessionResponse,
    ChatErpQaRequest,
    ChatErpQaResponse,
    ConfirmPreviewRequest,
    CreateDraftResponse,
    CreateIngestionRequest,
    DocumentParseExport,
    ErpPayloadPreviewResponse,
    ErrorCode,
    HealthResponse,
    IngestionResponse,
    IngestionStatus,
    ResolveIngestionRequest,
    ToolUi,
    SaveCustomerRequest,
    SaveCustomerResponse,
    UploadRequest,
    UploadResponse,
)
from app.tools.pdf_to_erp import pdf_to_erp_tool
from app.queue_client import enqueue_ingestion_job, get_ingestion_fallback_mode, queue_health_payload, remove_ingestion_job
from app.storage_client import save_binary_file
from app.store import (
    append_ingestion_event,
    confirm_preview_for_ingestion,
    cancel_ingestion,
    create_draft_for_ingestion,
    create_ingestion,
    create_upload,
    get_ingestion,
    process_ingestion,
    resolve_ingestion,
)

router = APIRouter()
logger = logging.getLogger("ai_erp_api")


@router.get("/")
def service_index() -> Dict[str, Any]:
    """根路径：返回常用相对 URL，便于本机浏览器或脚本快速发现能力（不含密钥）。"""
    return {
        "service": "ai-erp-assistant-api",
        "version": "0.1.0",
        "links": {
            "health": "/health",
            "docs": "/docs",
            "openapi_json": "/openapi.json",
            "erp_customer_save": {"method": "POST", "path": "/integrations/erp/customer"},
            "chat_erp_qa": {"method": "POST", "path": "/chat/erp-qa"},
            "chat_messages": {"method": "POST", "path": "/chat/messages"},
            "assistant_messages": {"method": "POST", "path": "/assistant/messages"},
            "assistant_messages_stream": {"method": "POST", "path": "/assistant/messages/stream"},
            "assistant_files": {"method": "POST", "path": "/assistant/files"},
            "assistant_llm_probe": {"method": "POST", "path": "/assistant/llm-router/probe"},
            "uploads": {"method": "POST", "path": "/uploads"},
            "ingestions_create": {"method": "POST", "path": "/ingestions"},
            "ingestion_get": {"method": "GET", "path": "/ingestions/{ingestion_id}"},
            "ingestion_resolve": {"method": "POST", "path": "/ingestions/{ingestion_id}/resolve"},
            "ingestion_confirm_preview": {"method": "POST", "path": "/ingestions/{ingestion_id}/confirm-preview"},
            "ingestion_create_draft": {"method": "POST", "path": "/ingestions/{ingestion_id}/create-draft"},
        },
    }


def _chat_erp_qa_upstream_hint(exc: ErpClientError) -> str:
    """将上游异常转写为聊天里可读的配置/排障提示（不含密钥）。"""
    code = (exc.code or "").strip()
    sc = int(exc.status_code or 0)
    if code == "ERP_COOKIE_LOGIN_CONFIGURE":
        return "请配置 `ERP_LOGIN_USERNAME` 与 `ERP_LOGIN_PASSWORD`（`ERP_AUTH_MODE=cookie_session`）。"
    if sc == 401:
        return "上游返回 **401**：请检查 `ERP_API_TOKEN` / `ERP_DATA_API_TOKEN` / `ERP_WRITE_API_TOKEN`，或改用 `ERP_AUTH_MODE=cookie_session` 并配置登录项。"
    if sc == 403:
        return "上游返回 **403**：权限不足，请核对账号与组织（`org_id` 会映射为销单查询的 `org` 等）。"
    if sc == 404:
        return "上游返回 **404**：路径或资源不存在。主数据搜索可改 `ERP_VENDORS_SEARCH_PATH` 等；Datynk 模式下对 404/405 主数据查询默认 **软失败为空列表**（见 `GET /health` 的 `erp_soft_fail_master_search`）。"
    if code in {"UPSTREAM_TIMEOUT", "ERP_UPSTREAM_TIMEOUT", "TIMEOUT"} or sc == 504:
        return "上游超时：可增大 `ERP_TIMEOUT_SECONDS` 或检查本机到 ERP 的网络。"
    return "排障：查看 API 日志中的 `erp_path`、`upstream_request_id`。"


def _http_exception_for_erp_client_error(exc: ErpClientError) -> HTTPException:
    """将适配层 ErpClientError 映射为对外 HTTP 状态与稳定 ErrorCode（与 store 侧 ingestion 映射语义对齐）。"""
    code = (exc.code or "").upper()
    if code == "ERP_CUSTOMER_SAVE_DISABLED":
        return HTTPException(status_code=503, detail=ErrorCode.ERP_CUSTOMER_SAVE_DISABLED.value)
    if code in {"UPSTREAM_TIMEOUT", "ERP_UPSTREAM_TIMEOUT", "TIMEOUT"}:
        return HTTPException(status_code=504, detail=ErrorCode.ERP_UPSTREAM_TIMEOUT.value)
    if code in {"MASTER_DATA_NOT_FOUND", "ERP_MASTER_DATA_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=ErrorCode.ERP_MASTER_DATA_NOT_FOUND.value)
    if code in {"PERMISSION_DENIED", "ERP_PERMISSION_DENIED", "FORBIDDEN"}:
        return HTTPException(status_code=403, detail=ErrorCode.ERP_PERMISSION_DENIED.value)
    return HTTPException(status_code=502, detail=ErrorCode.ERP_UPSTREAM_ERROR.value)


def _process_ingestion_in_background(ingestion_id: str, request_id: str) -> None:
    try:
        result = process_ingestion(ingestion_id)
        logger.info(
            "process_thread_after_enqueue_miss request_id=%s ingestion_id=%s result_status=%s",
            request_id,
            ingestion_id,
            getattr(result, "status", None) if result else None,
        )
    except Exception:
        logger.exception(
            "process_thread_after_enqueue_miss_failed request_id=%s ingestion_id=%s",
            request_id,
            ingestion_id,
        )


def _handle_queue_dispatch_outcome(ingestion_id: str, enqueued: bool, request_id: str) -> None:
    if enqueued:
        append_ingestion_event(
            ingestion_id,
            IngestionStatus.UPLOADED,
            "task enqueued for async worker processing",
        )
        return

    fallback_mode = get_ingestion_fallback_mode()
    logger.warning(
        "enqueue_failed request_id=%s ingestion_id=%s fallback_mode=%s",
        request_id,
        ingestion_id,
        fallback_mode,
    )
    if fallback_mode == "inline":
        append_ingestion_event(
            ingestion_id,
            IngestionStatus.UPLOADED,
            "queue unavailable; falling back to inline processing (dev only)",
        )
        try:
            result = process_ingestion(ingestion_id)
            logger.info(
                "process_inline_after_enqueue_miss request_id=%s ingestion_id=%s result_status=%s",
                request_id,
                ingestion_id,
                getattr(result, "status", None) if result else None,
            )
        except Exception:
            logger.exception(
                "process_inline_after_enqueue_miss_failed request_id=%s ingestion_id=%s",
                request_id,
                ingestion_id,
            )
        return

    if fallback_mode == "thread":
        append_ingestion_event(
            ingestion_id,
            IngestionStatus.UPLOADED,
            "queue unavailable; scheduled local background processing fallback (dev only)",
        )
        Thread(
            target=_process_ingestion_in_background,
            args=(ingestion_id, request_id),
            name=f"ingestion-fallback-{ingestion_id[:8]}",
            daemon=True,
        ).start()
        return

    append_ingestion_event(
        ingestion_id,
        IngestionStatus.UPLOADED,
        "queue unavailable; task remains uploaded until async worker/queue recovers",
    )


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    tess = tesseract_health_payload()
    prof_dir, prof_n = profiles_directory_stats()
    return HealthResponse(
        status="ok",
        extraction_profiles_dir=prof_dir,
        extraction_profile_json_count=prof_n,
        **tess,
        **queue_health_payload(),
        **erp_adapter_health_payload(),
        **erp_qa_reports_health_payload(),
        llm_extract_enabled=llm_extract_enabled(),
        llm_router_enabled=assistant_llm_router_enabled(),
        llm_api_key_configured=llm_api_key_configured(),
        llm_model=llm_model_name(),
        llm_base_url=llm_base_url(),
        llm_prompt_version=llm_prompt_version(),
    )


# 单次二进制上传允许的最大字节数（防止误传超大文件撑爆内存）；后续改为直传 MinIO 流式后可调大。
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024


async def _create_ingestion_from_upload_file(
    *,
    request: Request,
    file: UploadFile,
    user_id: str,
    org_id: str,
    extraction_profile_id: Optional[str],
    log_event: str,
) -> IngestionResponse:
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        logger.warning(
            "upload_binary_rejected_too_large request_id=%s size=%s",
            getattr(request.state, "request_id", "n/a"),
            len(raw),
        )
        raise HTTPException(status_code=413, detail="FILE_TOO_LARGE")

    file_hash = hashlib.sha256(raw).hexdigest()
    file_name = file.filename or "upload.bin"
    object_key = save_binary_file(raw=raw, file_name=file_name, file_hash=file_hash, org_id=org_id)

    prof = (extraction_profile_id or "").strip() or None
    payload = UploadRequest(
        file_name=file_name,
        file_hash=file_hash,
        user_id=user_id,
        org_id=org_id,
        source_file_object_key=object_key,
        extraction_profile_id=prof,
    )
    ingestion = create_upload(payload)
    enqueued = enqueue_ingestion_job(ingestion.ingestion_id)
    request_id = getattr(request.state, "request_id", "n/a")
    logger.info(
        "%s request_id=%s org_id=%s user_id=%s ingestion_id=%s file_id=%s bytes=%s hash_prefix=%s object_key=%s",
        log_event,
        request_id,
        ingestion.org_id,
        ingestion.user_id,
        ingestion.ingestion_id,
        ingestion.file_id,
        len(raw),
        file_hash[:12],
        object_key or "none",
    )
    if not enqueued:
        logger.warning("enqueue_failed request_id=%s ingestion_id=%s", request_id, ingestion.ingestion_id)
    _handle_queue_dispatch_outcome(ingestion.ingestion_id, enqueued, request_id)
    return ingestion


@router.post("/uploads", response_model=UploadResponse)
def upload(payload: UploadRequest, request: Request) -> UploadResponse:
    # 上传接口的职责是“接收文件元信息并创建 ingestion 任务”。
    # 这里不做耗时解析，只做快速入库与入队，保证前端得到即时响应，
    # 后续解析/抽取/映射由 worker 异步处理，符合解耦和可扩展原则。
    ingestion = create_upload(payload)
    enqueued = enqueue_ingestion_job(ingestion.ingestion_id)
    request_id = getattr(request.state, "request_id", "n/a")
    logger.info(
        "upload_created request_id=%s org_id=%s user_id=%s ingestion_id=%s file_id=%s",
        request_id,
        ingestion.org_id,
        ingestion.user_id,
        ingestion.ingestion_id,
        ingestion.file_id,
    )
    if not enqueued:
        logger.warning("enqueue_failed request_id=%s ingestion_id=%s", request_id, ingestion.ingestion_id)
    _handle_queue_dispatch_outcome(ingestion.ingestion_id, enqueued, request_id)
    return UploadResponse(
        file_id=ingestion.file_id,
        ingestion_id=ingestion.ingestion_id,
        status=ingestion.status,
    )


@router.post("/uploads/binary", response_model=UploadResponse)
async def upload_binary(
    request: Request,
    file: UploadFile = File(..., description="用户上传的原始文件（当前在内存中计算哈希，后续接 MinIO 落库）"),
    user_id: str = Form(..., description="业务用户标识（简化认证阶段由前端传入）"),
    org_id: str = Form(..., description="组织标识，用于幂等键与权限隔离（后续接 SSO）"),
    extraction_profile_id: Optional[str] = Form(
        default=None,
        description="可选：解析档案 id（backend/config/extraction_profiles/{id}.json）；空则按 org_id/default 自动选择",
    ),
) -> UploadResponse:
    """
    二进制 multipart 上传入口。

    与 ``POST /uploads``（JSON 元数据）的区别：
    - 由服务端读取文件字节并计算 SHA-256，避免前端大文件算哈希卡顿或不一致；
    - 仍复用同一套 ``create_upload`` 创建 ingestion 与入队逻辑。

    当前版本会优先尝试写入 MinIO 兼容对象存储；
    若 MinIO 未配置，则降级为本地目录（``__local__/...`` key，见 ``LOCAL_OBJECT_STORAGE_DIR``），
    以便异步解析仍能读取上传字节。
    """
    ingestion = await _create_ingestion_from_upload_file(
        request=request,
        file=file,
        user_id=user_id,
        org_id=org_id,
        extraction_profile_id=extraction_profile_id,
        log_event="upload_binary_created",
    )
    return UploadResponse(
        file_id=ingestion.file_id,
        ingestion_id=ingestion.ingestion_id,
        status=ingestion.status,
    )


@router.post("/ingestions", response_model=IngestionResponse)
def create_ingestion_route(payload: CreateIngestionRequest, request: Request) -> IngestionResponse:
    # 该接口用于“直接创建 ingestion”，主要给非文件来源场景预留：
    # 例如后续可能接入 ERP 主动推送、批量导入、外部系统 webhook 等。
    # 它与 /uploads 的核心差异是：调用方自己提供 file_id/file_hash 等上下文。
    ingestion = create_ingestion(payload)
    enqueued = enqueue_ingestion_job(ingestion.ingestion_id)
    request_id = getattr(request.state, "request_id", "n/a")
    logger.info(
        "ingestion_created request_id=%s org_id=%s user_id=%s ingestion_id=%s status=%s",
        request_id,
        ingestion.org_id,
        ingestion.user_id,
        ingestion.ingestion_id,
        ingestion.status,
    )
    if not enqueued:
        logger.warning("enqueue_failed request_id=%s ingestion_id=%s", request_id, ingestion.ingestion_id)
    _handle_queue_dispatch_outcome(ingestion.ingestion_id, enqueued, request_id)
    return ingestion


@router.post("/integrations/erp/customer", response_model=SaveCustomerResponse)
def save_customer_route(payload: SaveCustomerRequest, request: Request) -> SaveCustomerResponse:
    """
    直连写侧 ERP 保存客户（Datynk：`POST /api/customer/save` 等）。

    请求体中的 ``org_id`` 会写入扁平字段 ``org``（若 ``fields`` 中尚未提供），便于与现有 PO 契约对齐。
    需在环境中启用 ``ERP_CUSTOMER_SAVE_ENABLED``；见 ``GET /health`` 的 ``erp_customer_save_enabled``。
    """
    request_id = getattr(request.state, "request_id", "n/a")
    merged: dict[str, str] = dict(payload.fields)
    org = (payload.org_id or "").strip()
    if org:
        merged.setdefault("org", org)
    clear_last_upstream_meta()
    try:
        customer_no, customer_url = erp_client.save_customer(merged)
    except ErpClientError as exc:
        logger.warning(
            "save_customer_erp_failed request_id=%s org_id=%s user_id=%s erp_code=%s",
            request_id,
            payload.org_id,
            payload.user_id or "n/a",
            exc.code,
        )
        raise _http_exception_for_erp_client_error(exc) from exc
    logger.info(
        "save_customer_succeeded request_id=%s org_id=%s user_id=%s customer_no=%s",
        request_id,
        payload.org_id,
        payload.user_id or "n/a",
        customer_no,
    )
    return SaveCustomerResponse(customer_no=customer_no, customer_url=customer_url)


@router.post("/chat/erp-qa", response_model=ChatErpQaResponse)
def chat_erp_qa_route(payload: ChatErpQaRequest, request: Request) -> ChatErpQaResponse:
    """
    ERP 主数据问答（MVP）：仅组合 ERP 适配层查询（search_vendors / search_materials / 仓库税码 / search_customers / search_sale_orders 等），
    不调用大模型，避免「无工具凭感觉答实时主数据」。
    若用户问「如何新建/保存客户」，返回 `POST /integrations/erp/customer` 等说明，不自动调用写接口。
    """
    request_id = getattr(request.state, "request_id", "n/a")
    try:
        answer, tools_used, _raw = answer_with_erp_tools(payload.org_id, payload.message, erp_client)
    except ErpClientError as exc:
        logger.warning(
            "chat_erp_qa_erp_client_error request_id=%s code=%s status=%s",
            request_id,
            exc.code,
            exc.status_code,
        )
        hint = _chat_erp_qa_upstream_hint(exc)
        sc = int(exc.status_code or 0)
        body = (
            f"调用 ERP 接口失败：**{exc.code}**（HTTP {sc if sc else 'n/a'}）\n"
            f"{exc.message}\n\n"
            f"{hint}"
        )
        return ChatErpQaResponse(answer=body, erp_tools_used=[f"(upstream_error:{exc.code})"])
    logger.info(
        "chat_erp_qa request_id=%s org_id=%s user_id=%s tools=%s",
        request_id,
        payload.org_id,
        payload.user_id or "n/a",
        tools_used,
    )
    return ChatErpQaResponse(answer=answer, erp_tools_used=tools_used)


@router.post("/chat/messages", response_model=ChatMessageResponse)
def chat_messages_route(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse:
    """统一对话入口：先接入 pdf_to_erp 工具，后续可继续挂库存、订单查询等工具。"""
    request_id = getattr(request.state, "request_id", "n/a")
    logger.info(
        "chat_messages request_id=%s session_id=%s tool=%s action=%s active_task_id=%s",
        request_id,
        payload.session_id or "n/a",
        payload.tool or "pdf_to_erp",
        payload.action or "infer",
        payload.active_task_id or "n/a",
    )
    return handle_chat_message(payload)


@router.post("/chat/files", response_model=ChatMessageResponse)
async def chat_files_route(
    request: Request,
    file: UploadFile = File(..., description="用户上传的 PDF/订单文件"),
    user_id: str = Form(...),
    org_id: str = Form(...),
    session_id: Optional[str] = Form(default=None),
    extraction_profile_id: Optional[str] = Form(default=None),
) -> ChatMessageResponse:
    """对话式文件入口：上传文件后直接返回 pdf_to_erp 工具消息与处理卡片。"""
    ingestion = await _create_ingestion_from_upload_file(
        request=request,
        file=file,
        user_id=user_id,
        org_id=org_id,
        extraction_profile_id=extraction_profile_id,
        log_event="chat_file_created",
    )
    result = pdf_to_erp_tool.get_status(ingestion.ingestion_id)
    if result is None:
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    result.message = f"已收到《{ingestion.source_file_name or file.filename or '上传文件'}》，我开始识别并准备转换为 ERP 订单。"
    sid = ensure_session_id(session_id)
    append_user_message(sid, f"上传文件：{ingestion.source_file_name or file.filename or '上传文件'}")
    result.message = f"已收到《{ingestion.source_file_name or file.filename or '上传文件'}》，我会把它作为订单文件识别，并在需要时请你补充字段。"
    response = ChatMessageResponse(
        session_id=sid,
        messages=[ChatToolMessage(role="assistant", content=result.message)],
        active_task=ChatTaskState(type=result.tool, ingestion_id=result.ingestion_id, status=result.status),
        tool_result=result,
        ui=ToolUi(
            type="processing",
            data={
                "ingestion_id": ingestion.ingestion_id,
                "status": result.status,
                "file_name": ingestion.source_file_name or file.filename or "",
            },
        ),
    )
    return append_response(sid, response)


@router.post("/assistant/messages", response_model=ChatMessageResponse)
def assistant_messages_route(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse:
    """Unified assistant entrypoint. Rule-based today; LLM tool routing can replace this later."""
    request_id = getattr(request.state, "request_id", "n/a")
    session_id = ensure_session_id(payload.session_id)
    payload = payload.model_copy(update={"session_id": session_id})
    append_user_message(session_id, payload.message)
    logger.info(
        "assistant_messages request_id=%s session_id=%s active_task_id=%s",
        request_id,
        session_id,
        payload.active_task_id or "n/a",
    )
    return append_response(session_id, handle_assistant_message(payload))


def _model_payload(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "dict"):
        return model.dict()
    return model


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/assistant/messages/stream")
def assistant_messages_stream_route(payload: ChatMessageRequest, request: Request) -> StreamingResponse:
    """SSE assistant entrypoint. Ordinary LLM replies stream; tool results are sent as one final event."""
    request_id = getattr(request.state, "request_id", "n/a")
    session_id = ensure_session_id(payload.session_id)
    payload = payload.model_copy(update={"session_id": session_id})
    append_user_message(session_id, payload.message)
    logger.info(
        "assistant_messages_stream request_id=%s session_id=%s active_task_id=%s",
        request_id,
        session_id,
        payload.active_task_id or "n/a",
    )

    def events() -> Iterator[str]:
        yield _sse("session", {"session_id": session_id})
        if should_use_plain_chat_fast_path(payload):
            logger.info("assistant_stream_fast_path request_id=%s session_id=%s", request_id, session_id)
            decision: Optional[AssistantRouteDecision] = AssistantRouteDecision(
                route="assistant",
                reason="plain_chat_fast_path",
                source="fast_path",
            )
        else:
            decision = decide_with_llm(payload)
        if decision is not None and decision.route == "assistant":
            chunks: list[str] = []
            try:
                for chunk in stream_assistant_answer_with_llm(payload):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    yield _sse("delta", {"content": chunk})
            except LlmClientError as exc:
                yield _sse("error", {"message": str(exc)})
                response = handle_assistant_route_decision(payload, decision)
                saved = append_response(session_id, response)
                yield _sse("final", _model_payload(saved))
                return

            answer = "".join(chunks).strip()
            if answer:
                response = ChatMessageResponse(
                    session_id=session_id,
                    messages=[ChatToolMessage(role="assistant", content=answer)],
                    active_task=ChatTaskState(type="assistant", status="DONE"),
                    ui=ToolUi(type="assistant_reply", data={}),
                )
            else:
                response = handle_assistant_route_decision(payload, decision)
            saved = append_response(session_id, response)
            yield _sse("final", _model_payload(saved))
            return

        if decision is not None:
            response = handle_assistant_route_decision(payload, decision)
        else:
            response = handle_assistant_message(payload)
        saved = append_response(session_id, response)
        yield _sse("final", _model_payload(saved))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/assistant/llm-router/probe", response_model=AssistantLlmProbeResponse)
def assistant_llm_router_probe_route(
    payload: AssistantLlmProbeRequest,
    request: Request,
) -> AssistantLlmProbeResponse:
    request_id = getattr(request.state, "request_id", "n/a")
    logger.info("assistant_llm_probe request_id=%s message_len=%s", request_id, len(payload.message or ""))
    return probe_llm_router(
        ChatMessageRequest(
            message=payload.message,
            org_id=payload.org_id,
            user_id=payload.user_id,
            active_task_id=payload.active_task_id,
        )
    )


@router.post("/assistant/files", response_model=ChatMessageResponse)
async def assistant_files_route(
    request: Request,
    file: UploadFile = File(..., description="用户上传给助手处理的文件"),
    user_id: str = Form(...),
    org_id: str = Form(...),
    session_id: Optional[str] = Form(default=None),
    extraction_profile_id: Optional[str] = Form(default=None),
) -> ChatMessageResponse:
    """Unified assistant file entrypoint. Files currently route to pdf_to_erp."""
    ingestion = await _create_ingestion_from_upload_file(
        request=request,
        file=file,
        user_id=user_id,
        org_id=org_id,
        extraction_profile_id=extraction_profile_id,
        log_event="assistant_file_created",
    )
    result = pdf_to_erp_tool.get_status(ingestion.ingestion_id)
    if result is None:
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    sid = ensure_session_id(session_id)
    append_user_message(sid, f"Uploaded file: {ingestion.source_file_name or file.filename or 'upload'}")
    result.message = f"已收到《{ingestion.source_file_name or file.filename or '上传文件'}》，我会把它作为订单文件识别，并在需要时请你补充字段。"
    response = ChatMessageResponse(
        session_id=sid,
        messages=[ChatToolMessage(role="assistant", content=result.message)],
        active_task=ChatTaskState(type=result.tool, ingestion_id=result.ingestion_id, status=result.status),
        tool_result=result,
        ui=ToolUi(
            type="processing",
            data={
                "ingestion_id": ingestion.ingestion_id,
                "status": result.status,
                "file_name": ingestion.source_file_name or file.filename or "",
            },
        ),
    )
    return append_response(sid, response)


@router.get("/assistant/sessions/{session_id}", response_model=AssistantSessionResponse)
def assistant_session_route(session_id: str, request: Request) -> AssistantSessionResponse:
    session = get_session_response(session_id)
    if session is None:
        logger.info(
            "assistant_session_empty request_id=%s session_id=%s",
            getattr(request.state, "request_id", "n/a"),
            session_id,
        )
        return AssistantSessionResponse(session_id=session_id)
    return session


@router.get("/ingestions/{ingestion_id}", response_model=IngestionResponse)
def get_ingestion_route(ingestion_id: str, request: Request) -> IngestionResponse:
    ingestion = get_ingestion(ingestion_id)
    if not ingestion:
        logger.warning("ingestion_not_found request_id=%s ingestion_id=%s", getattr(request.state, "request_id", "n/a"), ingestion_id)
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    return ingestion


@router.get("/ingestions/{ingestion_id}/document", response_model=DocumentParseExport)
def get_ingestion_document_route(
    ingestion_id: str,
    request: Request,
    include_full_text: bool = Query(
        False,
        description="为 true 时从对象存储重新抽取全文（有字节/字符上限，大文件可能截断）",
    ),
) -> DocumentParseExport:
    """将解析与抽取结果封装为稳定 JSON，供外部系统拉取或落库。"""
    ingestion = get_ingestion(ingestion_id)
    if not ingestion:
        logger.warning(
            "ingestion_document_not_found request_id=%s ingestion_id=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
        )
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    payload = build_document_parse_export(ingestion, include_full_text=include_full_text)
    logger.info(
        "ingestion_document_export request_id=%s ingestion_id=%s include_full_text=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        include_full_text,
    )
    return DocumentParseExport.model_validate(payload)


@router.get("/ingestions/{ingestion_id}/erp-payload", response_model=ErpPayloadPreviewResponse)
def get_ingestion_erp_payload_route(ingestion_id: str, request: Request) -> ErpPayloadPreviewResponse:
    ingestion = get_ingestion(ingestion_id)
    if not ingestion:
        logger.warning(
            "ingestion_erp_payload_not_found request_id=%s ingestion_id=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
        )
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    doc_type = "PO" if ingestion.doc_type_hint is None else ingestion.doc_type_hint.value
    payload = build_datynk_sale_order_payload(ingestion)
    return ErpPayloadPreviewResponse(
        ingestion_id=ingestion_id,
        doc_type=doc_type,
        body_style="datynk_sale_order",
        payload=payload,
    )


@router.post("/ingestions/{ingestion_id}/resolve", response_model=IngestionResponse)
def resolve_ingestion_route(ingestion_id: str, payload: ResolveIngestionRequest, request: Request) -> IngestionResponse:
    # 当任务进入 NEED_USER_INPUT 状态时，前端会把用户补全字段提交到这里。
    # 后端会合并已解析字段与用户补充字段，然后重新执行校验，
    # 并据结果把状态推进到 VALIDATED 或继续维持 NEED_USER_INPUT。
    ingestion = resolve_ingestion(ingestion_id, payload)
    if not ingestion:
        logger.warning("resolve_ingestion_not_found request_id=%s ingestion_id=%s", getattr(request.state, "request_id", "n/a"), ingestion_id)
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    logger.info(
        "ingestion_resolved request_id=%s ingestion_id=%s status=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        ingestion.status,
    )
    return ingestion


@router.post("/ingestions/{ingestion_id}/confirm-preview", response_model=IngestionResponse)
def confirm_preview_route(ingestion_id: str, payload: ConfirmPreviewRequest, request: Request) -> IngestionResponse:
    ingestion = confirm_preview_for_ingestion(ingestion_id, payload.preview_data)
    if not ingestion:
        logger.warning(
            "confirm_preview_not_found request_id=%s ingestion_id=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
        )
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    logger.info(
        "preview_confirmed request_id=%s ingestion_id=%s status=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        ingestion.status,
    )
    return ingestion


@router.post("/ingestions/{ingestion_id}/create-draft", response_model=CreateDraftResponse)
def create_draft_route(ingestion_id: str, request: Request) -> CreateDraftResponse:
    # 创建草稿接口必须幂等：同一个 ingestion 重复触发不能重复建单。
    # 因此 store 层会根据稳定的 idempotency_key 返回同一草稿结果，
    # 从而避免“重复上传/重复点击”造成 ERP 侧重复草稿。
    draft = create_draft_for_ingestion(ingestion_id)
    if not draft:
        logger.warning(
            "create_draft_failed_missing_fields request_id=%s ingestion_id=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
        )
        raise HTTPException(status_code=400, detail=ErrorCode.MISSING_REQUIRED_FIELDS.value)
    logger.info(
        "draft_created request_id=%s ingestion_id=%s draft_no=%s idempotency_key=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        draft.draft_no,
        draft.idempotency_key,
    )
    return draft


@router.post("/ingestions/{ingestion_id}/cancel", response_model=IngestionResponse)
def cancel_ingestion_route(ingestion_id: str, request: Request) -> IngestionResponse:
    removed = remove_ingestion_job(ingestion_id)
    ingestion = cancel_ingestion(ingestion_id, "canceled after chat session deletion")
    if ingestion is None:
        logger.warning(
            "cancel_ingestion_not_found request_id=%s ingestion_id=%s queue_removed=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
            removed,
        )
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    logger.info(
        "ingestion_canceled request_id=%s ingestion_id=%s status=%s queue_removed=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        ingestion.status,
        removed,
    )
    return ingestion


@router.post("/internal/ingestions/{ingestion_id}/process", response_model=IngestionResponse)
def process_ingestion_route(ingestion_id: str, request: Request) -> IngestionResponse:
    # 这是内部处理端点，只给 worker 调用，不面向业务前端直接使用。
    # worker 从队列取到任务后调用本接口，推进状态机：
    # UPLOADED -> CLASSIFIED -> PARSED -> EXTRACTED -> MAPPED -> NEED_USER_INPUT 或 VALIDATED（自动校验通过时）
    ingestion = process_ingestion(ingestion_id)
    if not ingestion:
        logger.warning(
            "process_ingestion_not_found request_id=%s ingestion_id=%s",
            getattr(request.state, "request_id", "n/a"),
            ingestion_id,
        )
        raise HTTPException(status_code=404, detail=ErrorCode.INGESTION_NOT_FOUND.value)
    logger.info(
        "ingestion_processed request_id=%s ingestion_id=%s status=%s",
        getattr(request.state, "request_id", "n/a"),
        ingestion_id,
        ingestion.status,
    )
    return ingestion

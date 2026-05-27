from __future__ import annotations

import logging
from typing import Optional

from app.assistant_llm_router import (
    AssistantRouteDecision,
    answer_assistant_with_llm,
    decide_with_llm,
    should_use_plain_chat_fast_path,
)
from app.schemas import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatTaskState,
    ChatToolMessage,
    ToolUi,
)
from app.tools.registry import invoke_tool


logger = logging.getLogger("ai_erp_api")

PDF_TASK_WORDS = (
    "上传",
    "确认",
    "进度",
    "处理",
    "补充",
    "字段",
    "草稿",
    "订单",
    "入库",
    "pdf",
    "PDF",
)
ERP_QUERY_WORDS = (
    "查",
    "查询",
    "供应商",
    "物料",
    "客户",
    "库存",
    "仓库",
    "税码",
    "销售订单",
    "订单",
)


def _looks_like_pdf_task(payload: ChatMessageRequest) -> bool:
    if payload.tool == "pdf_to_erp":
        return True
    if payload.action or payload.fields or payload.preview_data is not None:
        return True
    text = (payload.message or "").strip()
    if payload.active_task_id and any(word in text for word in PDF_TASK_WORDS):
        return True
    return False


def _looks_like_erp_query(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(word in t for word in ERP_QUERY_WORDS)


def _assistant_help(session_id: Optional[str]) -> ChatMessageResponse:
    return ChatMessageResponse(
        session_id=session_id,
        messages=[
            ChatToolMessage(
                role="assistant",
                content=(
                    "我可以帮你处理订单 PDF、补充缺失字段并上传 ERP，也可以查询 ERP 里的供应商、物料、客户、仓库、税码和订单数据。"
                    "你可以直接上传文件，或者说“查物料 M001”。"
                ),
            )
        ],
        active_task=ChatTaskState(type="assistant", status="READY"),
        ui=ToolUi(type="assistant_help", data={}),
    )


def _assistant_llm_reply(payload: ChatMessageRequest) -> ChatMessageResponse:
    answer = answer_assistant_with_llm(payload)
    if not answer:
        return _assistant_help(payload.session_id)
    return ChatMessageResponse(
        session_id=payload.session_id,
        messages=[ChatToolMessage(role="assistant", content=answer)],
        active_task=ChatTaskState(type="assistant", status="DONE"),
        ui=ToolUi(type="assistant_reply", data={}),
    )


def _handle_erp_query(payload: ChatMessageRequest, text: str) -> ChatMessageResponse:
    result = invoke_tool("erp_qa", payload.model_copy(update={"message": text}))
    if result is not None:
        return result
    return _assistant_help(payload.session_id)
    try:
        answer, tools_used, _raw = answer_with_erp_tools(payload.org_id, text, erp_client)
    except ErpClientError as exc:
        answer = f"ERP 查询失败：{exc.code}。{exc.message}"
        tools_used = [f"upstream_error:{exc.code}"]
    return ChatMessageResponse(
        session_id=payload.session_id,
        messages=[ChatToolMessage(role="assistant", content=answer)],
        active_task=ChatTaskState(type="erp_qa", status="DONE"),
        ui=ToolUi(type="erp_query_result", data={"tools_used": tools_used}),
    )


def _handle_llm_decision(payload: ChatMessageRequest, decision: AssistantRouteDecision) -> ChatMessageResponse:
    logger.info(
        "assistant_tool_decision source=%s tool=%s action=%s active_task_id=%s reason=%s",
        decision.source,
        decision.tool_name,
        decision.action or "none",
        payload.active_task_id or "n/a",
        decision.reason[:160],
    )
    if decision.route == "pdf_to_erp":
        routed = payload.model_copy(
            update={
                "tool": "pdf_to_erp",
                "action": decision.action,
                "fields": decision.fields or payload.fields,
                "message": decision.message or payload.message,
            }
        )
        result = invoke_tool("pdf_to_erp", routed)
        if result is not None:
            return result
        return _assistant_help(payload.session_id)
    if decision.route == "erp_qa":
        return _handle_erp_query(payload, decision.message or payload.message)
    return _assistant_llm_reply(payload)


def handle_assistant_route_decision(payload: ChatMessageRequest, decision: AssistantRouteDecision) -> ChatMessageResponse:
    return _handle_llm_decision(payload, decision)


def handle_assistant_message(payload: ChatMessageRequest) -> ChatMessageResponse:
    text = (payload.message or "").strip()
    if should_use_plain_chat_fast_path(payload):
        logger.info("assistant_tool_decision source=fast_path tool=assistant action=reply")
        return _assistant_llm_reply(payload)

    decision = decide_with_llm(payload)
    if decision is not None:
        return _handle_llm_decision(payload, decision)

    if _looks_like_pdf_task(payload):
        logger.info(
            "assistant_tool_decision source=rules tool=pdf_to_erp action=%s active_task_id=%s",
            payload.action or "infer",
            payload.active_task_id or "n/a",
        )
        routed = payload.model_copy(update={"tool": "pdf_to_erp"})
        result = invoke_tool("pdf_to_erp", routed)
        if result is not None:
            return result
        return _assistant_help(payload.session_id)

    if _looks_like_erp_query(text):
        logger.info("assistant_tool_decision source=rules tool=erp_qa action=query")
        return _handle_erp_query(payload, text)

    logger.info("assistant_tool_decision source=rules tool=assistant action=help")
    return _assistant_help(payload.session_id)

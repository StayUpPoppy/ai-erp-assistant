from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from app.llm_client import (
    LlmClientError,
    chat_completion_json,
    chat_completion_text,
    chat_completion_text_stream,
    llm_api_key_configured,
    llm_base_url,
    llm_model_name,
)
from app.schemas import AssistantLlmProbeResponse, ChatMessageRequest
from app.tools.registry import registered_tool_names, registered_tool_specs


logger = logging.getLogger("ai_erp_api")


ALLOWED_ACTIONS = {"get_status", "submit_missing_fields", "confirm_preview", "create_draft", "cancel"}
FAST_PATH_BLOCK_WORDS = (
    "pdf",
    "erp",
    "订单",
    "单据",
    "上传",
    "文件",
    "发票",
    "入库",
    "草稿",
    "确认",
    "提交",
    "字段",
    "补充",
    "进度",
    "状态",
    "物料",
    "供应商",
    "客户",
    "库存",
    "仓库",
    "税码",
    "销售",
    "采购",
    "报表",
    "查询",
)
FAST_PATH_ALLOW_PATTERNS = (
    r"^(你好|您好|hello|hi|嗨|早上好|下午好|晚上好)[，,。!！\s]?.*",
    r".*(介绍一下你自己|你是谁|你能做什么|怎么称呼你).*",
    r"^(帮我)?(写|改写|润色|翻译|总结|解释|扩写|缩写).+",
    r"^(什么是|为什么|怎么理解|如何理解).+",
    r".*(聊聊天|讲个笑话|谢谢|多谢|辛苦了).*",
)
WRITE_ACTIONS = {"confirm_preview", "create_draft"}
CONFIRM_WORDS = ("确认", "同意", "上传", "提交", "生成草稿", "创建草稿", "是的", "yes")


@dataclass
class AssistantRouteDecision:
    route: str
    action: Optional[str] = None
    message: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    arguments: Dict[str, object] = field(default_factory=dict)
    reason: str = ""
    source: str = "llm"

    @property
    def tool_name(self) -> str:
        return self.route


def assistant_llm_router_enabled() -> bool:
    return os.getenv("ASSISTANT_LLM_ROUTER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def assistant_router_max_tokens() -> int:
    raw = (os.getenv("ASSISTANT_ROUTER_MAX_TOKENS") or "512").strip()
    try:
        return max(64, int(raw))
    except ValueError:
        return 512


def assistant_router_timeout_seconds() -> float:
    raw = (os.getenv("ASSISTANT_ROUTER_TIMEOUT_SECONDS") or "20").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 20.0


def assistant_plain_chat_fast_path_enabled() -> bool:
    return os.getenv("ASSISTANT_PLAIN_CHAT_FAST_PATH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def should_use_plain_chat_fast_path(payload: ChatMessageRequest) -> bool:
    if not assistant_plain_chat_fast_path_enabled():
        return False
    if payload.active_task_id or payload.tool or payload.action or payload.fields or payload.preview_data is not None:
        return False

    text = (payload.message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(word.lower() in lowered for word in FAST_PATH_BLOCK_WORDS):
        return False
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in FAST_PATH_ALLOW_PATTERNS)


def _first_json_object(text: str) -> Dict[str, object]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            raise
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i, ch in enumerate(raw[start:], start=start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            raise
        parsed = json.loads(raw[start:end])
    if not isinstance(parsed, dict):
        raise ValueError("assistant route JSON must be an object")
    return parsed


def _sanitize_fields(raw: object) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).strip()
        v = str(value).strip()
        if k and v:
            out[k] = v
    return out


def _sanitize_arguments(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, object] = {}
    for key, value in raw.items():
        k = str(key).strip()
        if not k:
            continue
        if isinstance(value, dict):
            out[k] = _sanitize_fields(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[k] = value
    return out


def _explicitly_confirmed(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(word.lower() in t for word in CONFIRM_WORDS)


def _sanitize_decision(raw: Dict[str, object], payload: ChatMessageRequest) -> AssistantRouteDecision:
    tool_call = raw.get("tool_call")
    if isinstance(tool_call, dict):
        route = str(tool_call.get("name") or tool_call.get("tool") or "assistant").strip()
        arguments = _sanitize_arguments(tool_call.get("arguments"))
    else:
        route = str(raw.get("route") or raw.get("tool") or "assistant").strip()
        arguments = _sanitize_arguments(raw.get("arguments"))

    if route not in set(registered_tool_names()) | {"assistant"}:
        route = "assistant"

    action_raw = arguments.get("action") or raw.get("action")
    action = str(action_raw).strip() if action_raw is not None else None
    if action not in ALLOWED_ACTIONS:
        action = None

    fields = _sanitize_fields(arguments.get("fields") or raw.get("fields"))
    if fields and route == "assistant":
        route = "pdf_to_erp"
        action = "submit_missing_fields"

    if action in WRITE_ACTIONS and not _explicitly_confirmed(payload.message):
        action = "get_status"

    if route == "pdf_to_erp" and not payload.active_task_id and action != "cancel":
        action = "get_status"

    message = str(
        arguments.get("query")
        or arguments.get("message")
        or raw.get("message")
        or payload.message
        or ""
    ).strip()

    return AssistantRouteDecision(
        route=route,
        action=action,
        message=message,
        fields=fields,
        arguments=arguments,
        reason=str(raw.get("reason") or "").strip(),
    )


def decide_with_llm(payload: ChatMessageRequest) -> Optional[AssistantRouteDecision]:
    if not assistant_llm_router_enabled():
        return None

    system = (
        "You are an ERP assistant tool router. Return exactly one JSON object and no prose.\n"
        "Prefer this format: {\"tool_call\":{\"name\":\"pdf_to_erp|erp_qa|assistant\",\"arguments\":{}},\"reason\":\"...\"}.\n"
        "Use pdf_to_erp for order/PDF processing, checking status, submitting missing fields, confirming preview, or creating ERP drafts.\n"
        "Use erp_qa for ERP data queries such as suppliers, materials, customers, warehouses, inventory, tax codes, and orders.\n"
        "Use assistant for ordinary help or when no tool is needed.\n"
        "Never choose create_draft or confirm_preview unless the user explicitly confirms the write action."
    )
    context = {
        "message": payload.message,
        "has_active_pdf_to_erp_task": bool(payload.active_task_id),
        "active_task_id": payload.active_task_id,
        "provided_fields": payload.fields,
        "has_preview_data": payload.preview_data is not None,
        "available_tools": registered_tool_specs(),
    }
    try:
        raw = chat_completion_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            max_tokens=assistant_router_max_tokens(),
            timeout_seconds=assistant_router_timeout_seconds(),
        )
        parsed = _first_json_object(raw)
        decision = _sanitize_decision(parsed, payload)
        logger.info(
            "assistant_llm_tool_call tool=%s action=%s fields=%s reason=%s",
            decision.route,
            decision.action or "none",
            sorted(decision.fields.keys()),
            decision.reason[:160],
        )
        return decision
    except (LlmClientError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("assistant_llm_route_failed fallback_to_rules err=%s", exc)
        return None


def assistant_answer_messages(payload: ChatMessageRequest) -> List[Dict[str, str]]:
    system = (
        "You are a concise ERP assistant in a chat product.\n"
        "Answer ordinary user questions directly in Chinese.\n"
        "If the user asks for ERP data or PDF-to-ERP work, explain briefly that the tool router will handle it instead of fabricating data.\n"
        "Do not claim that an ERP write action has happened unless a tool result is provided."
    )
    context = (
        "Current context:\n"
        f"- org_id: {payload.org_id}\n"
        f"- has_active_pdf_to_erp_task: {bool(payload.active_task_id)}\n"
        f"- active_task_id: {payload.active_task_id or ''}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": context},
        {"role": "user", "content": payload.message or ""},
    ]


def answer_assistant_with_llm(payload: ChatMessageRequest) -> Optional[str]:
    if not assistant_llm_router_enabled():
        return None

    try:
        text = chat_completion_text(assistant_answer_messages(payload)).strip()
    except LlmClientError as exc:
        logger.warning("assistant_llm_answer_failed fallback_to_help err=%s", exc)
        return None
    return text or None


def stream_assistant_answer_with_llm(payload: ChatMessageRequest) -> Iterator[str]:
    if not assistant_llm_router_enabled():
        return
    try:
        yield from chat_completion_text_stream(assistant_answer_messages(payload))
    except LlmClientError as exc:
        logger.warning("assistant_llm_stream_failed err=%s", exc)
        raise


def probe_llm_router(payload: ChatMessageRequest) -> AssistantLlmProbeResponse:
    enabled = assistant_llm_router_enabled()
    key_configured = llm_api_key_configured()
    base = AssistantLlmProbeResponse(
        enabled=enabled,
        api_key_configured=key_configured,
        model=llm_model_name(),
        base_url=llm_base_url(),
        ok=False,
    )
    if not enabled:
        base.error = "ASSISTANT_LLM_ROUTER_ENABLED is false"
        return base
    if not key_configured:
        base.error = "LLM API key is not configured"
        return base

    context = {
        "message": payload.message,
        "has_active_pdf_to_erp_task": bool(payload.active_task_id),
        "active_task_id": payload.active_task_id,
        "provided_fields": payload.fields,
        "has_preview_data": payload.preview_data is not None,
        "available_tools": registered_tool_specs(),
    }
    system = (
        "You are an ERP assistant tool router probe. Return exactly one JSON object.\n"
        "Use format {\"tool_call\":{\"name\":\"pdf_to_erp|erp_qa|assistant\",\"arguments\":{}},\"reason\":\"...\"}."
    )
    try:
        raw = chat_completion_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            max_tokens=assistant_router_max_tokens(),
            timeout_seconds=assistant_router_timeout_seconds(),
        )
        decision = _sanitize_decision(_first_json_object(raw), payload)
        return AssistantLlmProbeResponse(
            enabled=enabled,
            api_key_configured=key_configured,
            model=llm_model_name(),
            base_url=llm_base_url(),
            ok=True,
            attempted=True,
            tool_name=decision.tool_name,
            action=decision.action,
            arguments=decision.arguments,
            reason=decision.reason,
        )
    except (LlmClientError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return AssistantLlmProbeResponse(
            enabled=enabled,
            api_key_configured=key_configured,
            model=llm_model_name(),
            base_url=llm_base_url(),
            ok=False,
            attempted=True,
            error=str(exc),
        )

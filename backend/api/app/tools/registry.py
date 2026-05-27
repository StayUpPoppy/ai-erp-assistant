from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.chat_orchestrator import handle_chat_message
from app.schemas import ChatMessageRequest, ChatMessageResponse
from app.tools.erp_qa import erp_qa_tool


ToolInvoker = Callable[[ChatMessageRequest], ChatMessageResponse]


@dataclass(frozen=True)
class AssistantTool:
    name: str
    description: str
    parameters_schema: dict
    invoke: ToolInvoker


def _invoke_pdf_to_erp(payload: ChatMessageRequest) -> ChatMessageResponse:
    return handle_chat_message(payload.model_copy(update={"tool": "pdf_to_erp"}))


TOOLS: Dict[str, AssistantTool] = {
    "pdf_to_erp": AssistantTool(
        name="pdf_to_erp",
        description=(
            "Handle order/PDF to ERP workflows: check status, submit missing fields, "
            "confirm preview, and create an ERP draft after explicit user confirmation."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_status", "submit_missing_fields", "confirm_preview", "create_draft", "cancel"],
                },
                "ingestion_id": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                "message": {"type": "string"},
            },
            "required": ["action"],
        },
        invoke=_invoke_pdf_to_erp,
    ),
    "erp_qa": AssistantTool(
        name="erp_qa",
        description=(
            "Query ERP master data and business data, such as suppliers, materials, "
            "customers, warehouses, tax codes, inventory, and sales orders."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
        invoke=erp_qa_tool.invoke,
    ),
}


def get_tool(name: str) -> Optional[AssistantTool]:
    return TOOLS.get((name or "").strip())


def invoke_tool(name: str, payload: ChatMessageRequest) -> Optional[ChatMessageResponse]:
    tool = get_tool(name)
    if tool is None:
        return None
    return tool.invoke(payload)


def registered_tool_names() -> list[str]:
    return sorted(TOOLS)


def registered_tool_specs() -> list[dict]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_schema,
        }
        for tool in sorted(TOOLS.values(), key=lambda item: item.name)
    ]

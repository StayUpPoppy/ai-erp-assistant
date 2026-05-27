from __future__ import annotations

from app.erp_client import ErpClientError, erp_client
from app.erp_qa import answer_with_erp_tools
from app.schemas import ChatMessageRequest, ChatMessageResponse, ChatTaskState, ChatToolMessage, ToolUi


class ErpQaTool:
    name = "erp_qa"

    def invoke(self, payload: ChatMessageRequest) -> ChatMessageResponse:
        text = (payload.message or "").strip()
        try:
            answer, tools_used, _raw = answer_with_erp_tools(payload.org_id, text, erp_client)
        except ErpClientError as exc:
            answer = f"ERP 查询失败：{exc.code}。{exc.message}"
            tools_used = [f"upstream_error:{exc.code}"]
        return ChatMessageResponse(
            session_id=payload.session_id,
            messages=[ChatToolMessage(role="assistant", content=answer)],
            active_task=ChatTaskState(type=self.name, status="DONE"),
            ui=ToolUi(type="erp_query_result", data={"tools_used": tools_used}),
        )


erp_qa_tool = ErpQaTool()


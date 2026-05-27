from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant_session_db import apply_session_to_row, row_to_session
from app.assistant_session_store import AssistantSession
from app.orm_models import AssistantSessionRow
from app.schemas import ChatTaskState, ChatToolMessage, ToolUi


def test_assistant_session_row_roundtrip_json_fields():
    session = AssistantSession(
        session_id="s-roundtrip",
        messages=[
            ChatToolMessage(role="user", content="hello"),
            ChatToolMessage(role="assistant", content="processing", ui=ToolUi(type="processing", data={"x": 1})),
        ],
        active_task=ChatTaskState(type="pdf_to_erp", ingestion_id="ing-1", status="PROCESSING"),
        ui=ToolUi(type="processing", data={"ingestion_id": "ing-1"}),
        created_at="2026-05-24T00:00:00Z",
        updated_at="2026-05-24T00:01:00Z",
    )

    row = AssistantSessionRow(session_id=session.session_id)
    apply_session_to_row(row, session)
    restored = row_to_session(row)

    assert restored.session_id == "s-roundtrip"
    assert [m.role for m in restored.messages] == ["user", "assistant"]
    assert restored.messages[1].ui is not None
    assert restored.messages[1].ui.type == "processing"
    assert restored.active_task is not None
    assert restored.active_task.ingestion_id == "ing-1"
    assert restored.ui is not None
    assert restored.ui.data["ingestion_id"] == "ing-1"

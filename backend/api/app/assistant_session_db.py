from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.assistant_session_store import AssistantSession
from app.orm_models import AssistantSessionRow
from app.schemas import ChatTaskState, ChatToolMessage, ToolUi


logger = logging.getLogger("ai_erp_api")


def _dump_model(model: Any) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def row_to_session(row: AssistantSessionRow) -> AssistantSession:
    messages: List[ChatToolMessage] = []
    for item in list(row.messages or [])[-200:]:
        if not isinstance(item, dict):
            continue
        try:
            messages.append(ChatToolMessage.model_validate(item))
        except Exception:
            logger.warning("assistant_session_message_invalid session_id=%s", row.session_id, exc_info=True)

    active_task = None
    if isinstance(row.active_task, dict):
        try:
            active_task = ChatTaskState.model_validate(row.active_task)
        except Exception:
            logger.warning("assistant_session_active_task_invalid session_id=%s", row.session_id, exc_info=True)

    ui = None
    if isinstance(row.ui, dict):
        try:
            ui = ToolUi.model_validate(row.ui)
        except Exception:
            logger.warning("assistant_session_ui_invalid session_id=%s", row.session_id, exc_info=True)

    return AssistantSession(
        session_id=row.session_id,
        messages=messages,
        active_task=active_task,
        ui=ui,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_session_to_row(row: AssistantSessionRow, session_model: AssistantSession) -> None:
    row.messages = [_dump_model(m) for m in session_model.messages[-200:]]
    row.active_task = _dump_model(session_model.active_task) if session_model.active_task is not None else None
    row.ui = _dump_model(session_model.ui) if session_model.ui is not None else None
    row.created_at = session_model.created_at
    row.updated_at = session_model.updated_at


def get_by_id(session: Session, session_id: str) -> Optional[AssistantSession]:
    row = session.get(AssistantSessionRow, session_id)
    if row is None:
        logger.info("db_assistant_session_not_found session_id=%s", session_id)
        return None
    return row_to_session(row)


def upsert_session(session: Session, session_model: AssistantSession) -> None:
    row = session.get(AssistantSessionRow, session_model.session_id)
    if row is None:
        row = AssistantSessionRow(session_id=session_model.session_id)
        apply_session_to_row(row, session_model)
        session.add(row)
        logger.info("db_assistant_session_inserted session_id=%s", session_model.session_id)
        return
    apply_session_to_row(row, session_model)
    logger.info("db_assistant_session_updated session_id=%s messages=%s", session_model.session_id, len(session_model.messages))

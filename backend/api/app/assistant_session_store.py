from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, Optional
from uuid import uuid4

from app.database import SessionLocal, is_database_enabled
from app.schemas import AssistantSessionResponse, ChatMessageResponse, ChatTaskState, ChatToolMessage, ToolUi


MAX_MESSAGES_PER_SESSION = 200


@dataclass
class AssistantSession:
    session_id: str
    messages: list[ChatToolMessage] = field(default_factory=list)
    active_task: Optional[ChatTaskState] = None
    ui: Optional[ToolUi] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


_sessions: Dict[str, AssistantSession] = {}
_lock = Lock()


def ensure_session_id(session_id: Optional[str]) -> str:
    sid = (session_id or "").strip()
    return sid or str(uuid4())


def get_or_create_session(session_id: Optional[str]) -> AssistantSession:
    sid = ensure_session_id(session_id)
    with _lock:
        if is_database_enabled():
            return _db_get_or_create_session(sid)
        session = _sessions.get(sid)
        if session is None:
            session = AssistantSession(session_id=sid)
            _sessions[sid] = session
        return session


def append_user_message(session_id: str, content: str) -> None:
    text = (content or "").strip()
    if not text:
        return
    with _lock:
        session = _get_or_create_session_locked(session_id)
        session.messages.append(ChatToolMessage(role="user", content=text))
        _trim_and_touch(session)
        _persist_if_database_enabled(session)


def append_response(session_id: str, response: ChatMessageResponse) -> ChatMessageResponse:
    with _lock:
        session = _get_or_create_session_locked(session_id)
        for message in response.messages:
            session.messages.append(
                ChatToolMessage(role=message.role, content=message.content, ui=message.ui or response.ui)
            )
        if response.active_task is not None:
            session.active_task = response.active_task
        if response.ui is not None:
            session.ui = response.ui
        _trim_and_touch(session)
        _persist_if_database_enabled(session)
    response.session_id = session_id
    return response


def get_session_response(session_id: str) -> Optional[AssistantSessionResponse]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _lock:
        if is_database_enabled():
            session = _db_get_session(sid)
            if session is None:
                return None
            return _to_response(session)
        session = _sessions.get(sid)
        if session is None:
            return None
        return _to_response(session)


def reset_sessions_for_tests() -> None:
    with _lock:
        _sessions.clear()
        if is_database_enabled():
            from sqlalchemy import delete

            from app.orm_models import AssistantSessionRow

            session = _db_session()
            try:
                session.execute(delete(AssistantSessionRow))
                session.commit()
            finally:
                session.close()


def _get_or_create_session_locked(session_id: Optional[str]) -> AssistantSession:
    sid = ensure_session_id(session_id)
    if is_database_enabled():
        return _db_get_or_create_session(sid)
    session = _sessions.get(sid)
    if session is None:
        session = AssistantSession(session_id=sid)
        _sessions[sid] = session
    return session


def _trim_and_touch(session: AssistantSession) -> None:
    if len(session.messages) > MAX_MESSAGES_PER_SESSION:
        session.messages = session.messages[-MAX_MESSAGES_PER_SESSION:]
    session.updated_at = datetime.utcnow().isoformat() + "Z"


def _to_response(session: AssistantSession) -> AssistantSessionResponse:
    return AssistantSessionResponse(
        session_id=session.session_id,
        messages=list(session.messages),
        active_task=session.active_task,
        ui=session.ui,
    )


def _db_session():
    assert SessionLocal is not None
    return SessionLocal()


def _db_get_session(session_id: str) -> Optional[AssistantSession]:
    from app import assistant_session_db

    session = _db_session()
    try:
        return assistant_session_db.get_by_id(session, session_id)
    finally:
        session.close()


def _db_get_or_create_session(session_id: str) -> AssistantSession:
    existing = _db_get_session(session_id)
    if existing is not None:
        return existing
    created = AssistantSession(session_id=session_id)
    _persist_if_database_enabled(created)
    return created


def _persist_if_database_enabled(session_model: AssistantSession) -> None:
    if not is_database_enabled():
        return
    from app import assistant_session_db

    session = _db_session()
    try:
        assistant_session_db.upsert_session(session, session_model)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

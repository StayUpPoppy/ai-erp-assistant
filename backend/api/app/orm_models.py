"""
ingestion 表的 ORM 定义。

字段设计目标：
- 能完整还原 IngestionResponse（供前端轮询与审计回放）；
- file_hash 用于「同一用户同一文件不重复建任务」的幂等查询；
- JSON 字段承载缺失字段、已解析/补全字段、审计事件数组。
"""

from typing import List, Optional

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IngestionRow(Base):
    __tablename__ = "ingestions"

    ingestion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_file_object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    extract_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v0")
    model_version: Mapped[str] = mapped_column(String(128), nullable=False, default="mock-llm-v1")
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False, default="prompt-v1")

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    doc_type_hint: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    missing_fields: Mapped[List] = mapped_column(JSONB, nullable=False)
    resolved_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    audit_events: Mapped[List] = mapped_column(JSONB, nullable=False)

    draft_no: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 解析预览等非错误上下文（避免为预览字段频繁改表结构）
    ingestion_context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class AssistantSessionRow(Base):
    __tablename__ = "assistant_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    messages: Mapped[List] = mapped_column(JSONB, nullable=False, default=list)
    active_task: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ui: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

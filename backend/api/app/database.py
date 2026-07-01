"""
数据库连接与建表入口。

说明：
- 当环境变量 DATABASE_URL 为空时，业务仍使用内存 store（便于无 Docker 时开发）。
- 当 DATABASE_URL 已配置时，ingestion 数据写入 Postgres，服务重启不丢任务。
"""

import logging
import os
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类，所有 ORM 模型继承此类。"""


logger = logging.getLogger("ai_erp_api")


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def is_database_enabled() -> bool:
    return bool(_database_url())


_engine = None
# sessionmaker 工厂实例；未启用数据库时为 None。
SessionLocal: Optional[Any] = None

if is_database_enabled():
    _engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """
    启动时创建缺失的表（等同轻量迁移）。
    后续若表结构频繁变更，可改为 Alembic 管理版本化迁移。
    """
    if not _engine:
        return
    # 延迟导入，避免在未启用数据库时加载 ORM 模型产生副作用
    from app.orm_models import AssistantSessionRow, IngestionRow, WecomOrderRouteRow  # noqa: F401

    Base.metadata.create_all(bind=_engine)
    # 已有部署可能缺新列：用 IF NOT EXISTS 做轻量迁移（当前仅 PostgreSQL 目标库）。
    try:
        with _engine.begin() as conn:
            conn.execute(
                text(
                    """
                    ALTER TABLE ingestions
                    ADD COLUMN IF NOT EXISTS ingestion_context jsonb NOT NULL DEFAULT '{}'::jsonb
                    """
                )
            )
    except Exception:
        # 非 PG 或权限不足时不阻断启动；内存模式与单测不依赖该列。
        logger.warning("ingestion_context migration skipped", exc_info=True)

from pathlib import Path
import sys

import pytest
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ingestion_db
from app.database import SessionLocal, init_db, is_database_enabled
from app.orm_models import AssistantSessionRow, IngestionRow
from app.schemas import CreateIngestionRequest, ErrorCode, IngestionStatus
from app.store import create_ingestion, process_ingestion


pytestmark = pytest.mark.skipif(
    not is_database_enabled() or SessionLocal is None,
    reason="DATABASE_URL 未配置，跳过 DB 集成测试",
)


def _new_payload(file_hash: str) -> CreateIngestionRequest:
    return CreateIngestionRequest(
        file_id=f"file-{file_hash}",
        file_hash=file_hash,
        user_id="u-test",
        org_id="org-test",
        extract_version="v0",
        model_version="mock-llm-v1",
        prompt_version="prompt-v1",
    )


@pytest.fixture(autouse=True)
def _prepare_db():
    # 每个测试前清理数据，避免唯一键冲突和状态污染。
    init_db()
    session = SessionLocal()
    try:
        session.execute(delete(IngestionRow))
        session.execute(delete(AssistantSessionRow))
        session.commit()
    finally:
        session.close()


def test_db_mode_create_and_query_by_hash():
    created = create_ingestion(_new_payload("hash-db-query"))

    session = SessionLocal()
    try:
        by_id = ingestion_db.get_by_id(session, created.ingestion_id)
        by_hash = ingestion_db.get_by_file_hash(session, created.file_hash)
    finally:
        session.close()

    assert by_id is not None
    assert by_hash is not None
    assert by_id.ingestion_id == created.ingestion_id
    assert by_hash.ingestion_id == created.ingestion_id
    assert by_id.status == IngestionStatus.UPLOADED


def test_db_mode_persists_workflow_error_code(monkeypatch):
    created = create_ingestion(_new_payload("hash-db-error-code"))

    def _fake_workflow_failure(ingestion, erp, append_event):
        append_event(ingestion, IngestionStatus.FAILED, "forced db workflow failure for testing")
        ingestion.error_code = ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value
        return ingestion

    monkeypatch.setattr("app.store.run_ingestion_processing_workflow", _fake_workflow_failure)
    processed = process_ingestion(created.ingestion_id)
    assert processed is not None
    assert processed.status == IngestionStatus.FAILED
    assert processed.error_code == ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value

    session = SessionLocal()
    try:
        persisted = ingestion_db.get_by_id(session, created.ingestion_id)
    finally:
        session.close()

    assert persisted is not None
    assert persisted.status == IngestionStatus.FAILED
    assert persisted.error_code == ErrorCode.WORKFLOW_MAP_RETRY_TIMEOUT.value

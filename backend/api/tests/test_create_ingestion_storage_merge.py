"""create_ingestion 幂等命中时合并 object_key（修复重复上传仍无字节可读）。"""

from __future__ import annotations

import pytest

from app.schemas import CreateIngestionRequest
from app.store import create_ingestion, store


@pytest.fixture(autouse=True)
def clear_memory_store() -> None:
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()
    yield
    store.ingestions.clear()
    store.file_hash_to_ingestion.clear()


def test_idempotent_hit_updates_object_key_when_new_payload_has_key() -> None:
    h = "e" * 64
    first = CreateIngestionRequest(
        file_id="file-a",
        file_hash=h,
        user_id="u1",
        org_id="org1",
        source_file_object_key=None,
        source_file_name="doc.pdf",
    )
    ing1 = create_ingestion(first)
    assert ing1.source_file_object_key is None

    key = "__local__/uploads/org1/2099-01-01/eeeeeeeeeeee-doc.pdf"
    second = CreateIngestionRequest(
        file_id="file-b",
        file_hash=h,
        user_id="u1",
        org_id="org1",
        source_file_object_key=key,
        source_file_name="doc.pdf",
    )
    ing2 = create_ingestion(second)
    assert ing2.ingestion_id == ing1.ingestion_id
    assert ing2.source_file_object_key == key

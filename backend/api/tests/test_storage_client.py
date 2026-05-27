"""storage_client：无 MinIO 时本机目录降级读写。"""

from __future__ import annotations

import pytest

from app.storage_client import LOCAL_OBJECT_KEY_PREFIX, get_object_bytes, save_binary_file


@pytest.fixture
def no_minio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)


def test_save_binary_file_uses_local_prefix_when_minio_unconfigured(
    no_minio_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOCAL_OBJECT_STORAGE_DIR", str(tmp_path))
    file_hash = "a" * 64
    key = save_binary_file(b"hello-bytes", "note.txt", file_hash, "org-demo")
    assert key is not None
    assert key.startswith(LOCAL_OBJECT_KEY_PREFIX)
    assert "uploads/org-demo/" in key
    assert "note.txt" in key


def test_get_object_bytes_reads_local_fallback(
    no_minio_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOCAL_OBJECT_STORAGE_DIR", str(tmp_path))
    file_hash = "b" * 64
    key = save_binary_file(b"roundtrip", "doc.pdf", file_hash, "org-x")
    assert get_object_bytes(key) == b"roundtrip"


def test_save_binary_file_empty_returns_none(no_minio_env: None) -> None:
    assert save_binary_file(b"", "empty.bin", "c" * 64, "org") is None

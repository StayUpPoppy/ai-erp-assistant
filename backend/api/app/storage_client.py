"""
对象存储客户端（MinIO / S3 兼容）.

当前作用：
- 接收二进制文件并保存到对象存储；
- 返回 object_key 供日志与后续审计字段使用。

说明：
- 若 MinIO 环境变量未配置完整，**自动降级为本地目录存储**（仍返回 object_key，
  前缀 ``__local__/``），保证 worker 解析阶段 ``get_object_bytes`` 能读到上传字节；
- 若 MinIO 已配置，行为与原先一致（S3 兼容 put/get）。
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from minio import Minio

logger = logging.getLogger("ai_erp_api")

# 无 MinIO 时写入本机目录，key 带此前缀；get_object_bytes 优先按此前缀读盘
LOCAL_OBJECT_KEY_PREFIX = "__local__/"


def _build_client() -> Optional[Minio]:
    endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
    use_ssl_raw = os.getenv("MINIO_USE_SSL", "false").strip().lower()
    if not endpoint or not access_key or not secret_key:
        return None
    use_ssl = use_ssl_raw in {"1", "true", "yes", "on"}
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=use_ssl)


def _local_storage_root() -> Path:
    """本机降级存储根目录；可通过 LOCAL_OBJECT_STORAGE_DIR 覆盖。"""
    explicit = os.getenv("LOCAL_OBJECT_STORAGE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(tempfile.gettempdir()).resolve() / "ai-erp-assistant" / "local-objects"


def _save_to_local_filesystem(raw: bytes, file_name: str, file_hash: str, org_id: str) -> str:
    safe_name = file_name.replace("\\", "_").replace("/", "_")
    date_part = datetime.utcnow().strftime("%Y-%m-%d")
    rel = f"uploads/{org_id}/{date_part}/{file_hash[:12]}-{safe_name}"
    root = _local_storage_root()
    full = root.joinpath(*rel.split("/"))
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(raw)
    return f"{LOCAL_OBJECT_KEY_PREFIX}{rel}"


def _read_local_object(object_key: str) -> Optional[bytes]:
    if not object_key.startswith(LOCAL_OBJECT_KEY_PREFIX):
        return None
    rel = object_key[len(LOCAL_OBJECT_KEY_PREFIX) :].lstrip("/").replace("\\", "/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return None
    root = _local_storage_root()
    try:
        full = root.joinpath(*parts).resolve()
    except OSError:
        return None
    try:
        root_resolved = root.resolve()
        full.relative_to(root_resolved)
    except ValueError:
        return None
    try:
        return full.read_bytes()
    except OSError:
        return None


def save_binary_file(raw: bytes, file_name: str, file_hash: str, org_id: str) -> Optional[str]:
    """
    保存文件到对象存储并返回 object_key。

    key 结构（MinIO 与本地一致，仅前缀不同）：
    uploads/{org_id}/{yyyy-mm-dd}/{hash前12位}-{原始文件名}
    本地降级时返回 ``__local__/uploads/...``。
    """
    client = _build_client()
    if client is not None:
        bucket = os.getenv("MINIO_BUCKET", "ai-erp-assistant").strip() or "ai-erp-assistant"
        safe_name = file_name.replace("\\", "_").replace("/", "_")
        date_part = datetime.utcnow().strftime("%Y-%m-%d")
        object_key = f"uploads/{org_id}/{date_part}/{file_hash[:12]}-{safe_name}"
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            data_stream = io.BytesIO(raw)
            client.put_object(
                bucket_name=bucket,
                object_name=object_key,
                data=data_stream,
                length=len(raw),
                content_type="application/octet-stream",
            )
            return object_key
        except Exception:
            logger.warning(
                "save_binary_file_minio_failed_fallback_local org_id=%s file_hash_prefix=%s",
                org_id,
                file_hash[:12],
                exc_info=True,
            )

    if not raw:
        return None
    return _save_to_local_filesystem(raw, file_name, file_hash, org_id)


def get_object_bytes(object_key: Optional[str]) -> Optional[bytes]:
    """
    按 object_key 读取对象字节。

    优先处理 ``__local__/`` 本机降级 key；否则走 MinIO。
    key 为空或无法读取时返回 None。
    """
    if not object_key:
        return None

    if object_key.startswith(LOCAL_OBJECT_KEY_PREFIX):
        return _read_local_object(object_key)

    client = _build_client()
    if client is None:
        return None
    bucket = os.getenv("MINIO_BUCKET", "ai-erp-assistant").strip() or "ai-erp-assistant"
    try:
        response = client.get_object(bucket_name=bucket, object_name=object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    except Exception:
        return None

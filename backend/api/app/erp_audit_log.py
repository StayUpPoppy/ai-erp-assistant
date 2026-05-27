"""
ERP 调用摘要写入 ingestion（供审计与前端展示）。

原则：仅存可审计元数据（操作名、单据类型、是否成功、错误码、草稿号等），
不把完整业务 payload 或敏感字段全文落库；详细字段级错误仍走 error_details。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.erp_client import ErpClientError, consume_last_upstream_meta
from app.schemas import IngestionResponse

_MAX_ENTRIES = 30


def append_erp_call_log(ingestion: IngestionResponse, entry: Dict[str, Any]) -> None:
    """向 ingestion.erp_call_log 追加一条并截断长度。"""
    log = list(ingestion.erp_call_log or [])
    log.append(entry)
    ingestion.erp_call_log = log[-_MAX_ENTRIES:]


def append_erp_call_log_with_upstream(
    ingestion: IngestionResponse,
    base: Dict[str, Any],
    exc: Optional[ErpClientError] = None,
) -> None:
    """写入 erp_call_log，并合并最近一次 Real ERP HTTP 元数据（若有）。"""
    u = consume_last_upstream_meta()
    entry: Dict[str, Any] = dict(base)
    if u.get("upstream_request_id"):
        entry["upstream_request_id"] = u["upstream_request_id"]
    elif exc is not None and isinstance(exc.details, dict):
        rid = exc.details.get("request_id")
        if rid is None and isinstance(exc.details.get("raw"), dict):
            rid = exc.details["raw"].get("request_id")
        if rid:
            entry["upstream_request_id"] = str(rid)
    if u.get("erp_path"):
        entry["erp_path"] = u["erp_path"]
    if u.get("http_status") is not None:
        entry["http_status"] = u["http_status"]
    append_erp_call_log(ingestion, entry)

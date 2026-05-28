from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

logger = logging.getLogger("ai_erp_api")


class MineruClientError(RuntimeError):
    pass


def mineru_enabled() -> bool:
    return os.getenv("MINERU_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def mineru_base_url() -> str:
    return (os.getenv("MINERU_API_BASE") or "https://mineru.net/api/v1/agent").strip().rstrip("/")


def mineru_health_payload() -> Dict[str, object]:
    return {
        "mineru_enabled": mineru_enabled(),
        "mineru_api_base": mineru_base_url() if mineru_enabled() else "",
        "mineru_model": (os.getenv("MINERU_MODEL_VERSION") or "agent-lightweight").strip() or "agent-lightweight",
    }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _json_request(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MineruClientError(f"MinerU HTTP {exc.code}: {body[:300]}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise MineruClientError(f"MinerU network error: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MineruClientError(f"MinerU bad JSON: {raw[:300]}") from exc
    if not isinstance(parsed, dict):
        raise MineruClientError("MinerU response root is not an object")
    return parsed


def _download_text(url: str, timeout: float) -> str:
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise MineruClientError(f"MinerU markdown download failed: {exc}") from exc


def _put_file(upload_url: str, raw: bytes, timeout: float) -> None:
    req = request.Request(upload_url, data=raw, method="PUT", headers={"Accept": "*/*"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                raise MineruClientError(f"MinerU upload failed status={resp.status}")
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise MineruClientError(f"MinerU upload failed: {exc}") from exc


def parse_pdf_bytes_with_mineru(raw: bytes, file_name: str) -> Tuple[str, str]:
    """
    MinerU Agent Lightweight signed-upload flow.

    The API returns Markdown; callers continue feeding that text into the existing
    ERP/Pydantic extraction pipeline.
    """
    if not mineru_enabled():
        return "", "mineru_disabled"
    if not raw:
        return "", "empty"

    base = mineru_base_url()
    file_name = file_name or "document.pdf"
    language = (os.getenv("MINERU_LANGUAGE") or "ch").strip() or "ch"
    timeout = float(os.getenv("MINERU_HTTP_TIMEOUT_SECONDS", "30").strip() or "30")
    poll_timeout = float(os.getenv("MINERU_POLL_TIMEOUT_SECONDS", "180").strip() or "180")
    poll_interval = float(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "3").strip() or "3")
    payload: Dict[str, Any] = {
        "file_name": file_name,
        "language": language,
        "enable_table": _env_bool("MINERU_ENABLE_TABLE", True),
        "is_ocr": _env_bool("MINERU_IS_OCR", True),
        "enable_formula": _env_bool("MINERU_ENABLE_FORMULA", False),
    }
    page_range = (os.getenv("MINERU_PAGE_RANGE") or "").strip()
    if page_range:
        payload["page_range"] = page_range

    started = time.monotonic()
    create = _json_request("POST", f"{base}/parse/file", payload, timeout=timeout)
    if create.get("code") != 0:
        raise MineruClientError(f"MinerU create failed: {create.get('msg') or create}")
    data = create.get("data")
    if not isinstance(data, dict):
        raise MineruClientError("MinerU create missing data")
    task_id = str(data.get("task_id") or "").strip()
    upload_url = str(data.get("file_url") or "").strip()
    if not task_id or not upload_url:
        raise MineruClientError("MinerU create missing task_id/file_url")

    _put_file(upload_url, raw, timeout=timeout)

    while time.monotonic() - started < poll_timeout:
        result = _json_request("GET", f"{base}/parse/{task_id}", None, timeout=timeout)
        if result.get("code") != 0:
            raise MineruClientError(f"MinerU poll failed: {result.get('msg') or result}")
        rd = result.get("data")
        if not isinstance(rd, dict):
            raise MineruClientError("MinerU poll missing data")
        state = str(rd.get("state") or "").strip()
        if state == "done":
            markdown_url = str(rd.get("markdown_url") or "").strip()
            if not markdown_url:
                raise MineruClientError("MinerU done but missing markdown_url")
            text = _download_text(markdown_url, timeout=timeout)
            text = text.strip()
            if not text:
                return "", "mineru_empty"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("mineru_parse_done file_name=%s task_id=%s chars=%s elapsed_ms=%s", file_name, task_id, len(text), elapsed_ms)
            return text, "mineru_markdown"
        if state == "failed":
            raise MineruClientError(f"MinerU task failed: {rd.get('err_msg') or rd.get('err_code') or 'unknown'}")
        time.sleep(max(0.5, poll_interval))

    raise MineruClientError(f"MinerU poll timeout task_id={task_id} timeout={poll_timeout}")

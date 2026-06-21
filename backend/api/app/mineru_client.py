from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from io import BytesIO
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

logger = logging.getLogger("ai_erp_api")


class MineruClientError(RuntimeError):
    pass


def mineru_enabled() -> bool:
    return os.getenv("MINERU_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def mineru_mode() -> str:
    mode = (os.getenv("MINERU_MODE") or "precise_v4").strip().lower()
    return mode if mode in {"precise_v4", "agent"} else "precise_v4"


def mineru_base_url() -> str:
    if mineru_mode() == "agent":
        return (os.getenv("MINERU_AGENT_API_BASE") or os.getenv("MINERU_API_BASE") or "https://mineru.net/api/v1/agent").strip().rstrip("/")
    return (os.getenv("MINERU_PRECISE_API_BASE") or "https://mineru.net/api/v4").strip().rstrip("/")


def mineru_health_payload() -> Dict[str, object]:
    return {
        "mineru_enabled": mineru_enabled(),
        "mineru_api_base": mineru_base_url() if mineru_enabled() else "",
        "mineru_mode": mineru_mode(),
        "mineru_token_configured": bool((os.getenv("MINERU_API_TOKEN") or "").strip()),
        "mineru_model": (os.getenv("MINERU_MODEL_VERSION") or ("vlm" if mineru_mode() == "precise_v4" else "agent-lightweight")).strip(),
    }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _json_request(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    request_headers.update(headers or {})
    req = request.Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
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


def _parse_pdf_bytes_with_agent(raw: bytes, file_name: str) -> Tuple[str, str]:
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


def _download_archive_markdown(url: str, timeout: float) -> str:
    max_bytes = 64 * 1024 * 1024
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            try:
                content_length = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > max_bytes:
                raise MineruClientError(f"MinerU result archive too large: {content_length}")
            raw = resp.read(max_bytes + 1)
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise MineruClientError(f"MinerU result download failed: {exc}") from exc
    if len(raw) > max_bytes:
        raise MineruClientError("MinerU result archive exceeds 64MB")
    try:
        with zipfile.ZipFile(BytesIO(raw), "r") as archive:
            candidates = [name for name in archive.namelist() if name.replace("\\", "/").endswith("/full.md") or name == "full.md"]
            if not candidates:
                candidates = [name for name in archive.namelist() if name.lower().endswith(".md")]
            if not candidates:
                raise MineruClientError("MinerU result archive missing Markdown")
            info = archive.getinfo(candidates[0])
            if info.file_size > 32 * 1024 * 1024:
                raise MineruClientError("MinerU Markdown result exceeds 32MB")
            return archive.read(info).decode("utf-8", errors="replace").strip()
    except zipfile.BadZipFile as exc:
        raise MineruClientError("MinerU result is not a valid zip archive") from exc


def _parse_pdf_bytes_precise(raw: bytes, file_name: str) -> Tuple[str, str]:
    token = (os.getenv("MINERU_API_TOKEN") or "").strip()
    if not token:
        raise MineruClientError("MinerU precise API token is not configured")
    base = mineru_base_url()
    timeout = float(os.getenv("MINERU_HTTP_TIMEOUT_SECONDS", "30").strip() or "30")
    poll_timeout = float(os.getenv("MINERU_POLL_TIMEOUT_SECONDS", "180").strip() or "180")
    poll_interval = float(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "3").strip() or "3")
    model = (os.getenv("MINERU_MODEL_VERSION") or "vlm").strip() or "vlm"
    headers = {"Authorization": f"Bearer {token}"}
    file_spec: Dict[str, Any] = {
        "name": file_name or "document.pdf",
        "is_ocr": _env_bool("MINERU_IS_OCR", True),
    }
    page_ranges = (os.getenv("MINERU_PAGE_RANGE") or "").strip()
    if page_ranges:
        file_spec["page_ranges"] = page_ranges
    payload: Dict[str, Any] = {
        "files": [file_spec],
        "model_version": model,
        "language": (os.getenv("MINERU_LANGUAGE") or "ch").strip() or "ch",
        "enable_table": _env_bool("MINERU_ENABLE_TABLE", True),
        "enable_formula": _env_bool("MINERU_ENABLE_FORMULA", False),
    }
    started = time.monotonic()
    create = _json_request("POST", f"{base}/file-urls/batch", payload, timeout=timeout, headers=headers)
    if create.get("code") != 0:
        raise MineruClientError(f"MinerU create failed: {create.get('msg') or create.get('code')}")
    data = create.get("data")
    if not isinstance(data, dict):
        raise MineruClientError("MinerU create missing data")
    batch_id = str(data.get("batch_id") or "").strip()
    file_urls = data.get("file_urls")
    upload_url = str(file_urls[0] if isinstance(file_urls, list) and file_urls else "").strip()
    if not batch_id or not upload_url:
        raise MineruClientError("MinerU create missing batch_id/file_url")
    _put_file(upload_url, raw, timeout=timeout)

    while time.monotonic() - started < poll_timeout:
        response = _json_request(
            "GET",
            f"{base}/extract-results/batch/{batch_id}",
            timeout=timeout,
            headers=headers,
        )
        if response.get("code") != 0:
            raise MineruClientError(f"MinerU poll failed: {response.get('msg') or response.get('code')}")
        response_data = response.get("data")
        results = response_data.get("extract_result") if isinstance(response_data, dict) else None
        item = results[0] if isinstance(results, list) and results else None
        if not isinstance(item, dict):
            time.sleep(max(0.5, poll_interval))
            continue
        state = str(item.get("state") or "").strip().lower()
        if state == "done":
            archive_url = str(item.get("full_zip_url") or "").strip()
            if not archive_url:
                raise MineruClientError("MinerU done but missing full_zip_url")
            text = _download_archive_markdown(archive_url, timeout=timeout)
            if not text:
                return "", "mineru_v4_empty"
            logger.info(
                "mineru_v4_parse_done file_name=%s batch_id=%s chars=%s elapsed_ms=%s",
                file_name,
                batch_id,
                len(text),
                int((time.monotonic() - started) * 1000),
            )
            return text, f"mineru_v4_{model}_markdown"
        if state == "failed":
            raise MineruClientError(f"MinerU task failed: {item.get('err_msg') or 'unknown'}")
        time.sleep(max(0.5, poll_interval))
    raise MineruClientError(f"MinerU poll timeout batch_id={batch_id} timeout={poll_timeout}")


def parse_pdf_bytes_with_mineru(raw: bytes, file_name: str) -> Tuple[str, str]:
    if not mineru_enabled():
        return "", "mineru_disabled"
    if not raw:
        return "", "empty"
    if mineru_mode() == "agent":
        return _parse_pdf_bytes_with_agent(raw, file_name)
    return _parse_pdf_bytes_precise(raw, file_name)

"""
阿里云通用文字识别 RecognizeGeneral（RPC + HMAC-SHA1，POST 图片二进制）。

文档：
https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-recognizegeneral
https://help.aliyun.com/zh/ocr/developer-reference/signature-method

环境变量（与 ocr_engine 配合）：
- ALIYUN_OCR_ACCESS_KEY_ID / ALIYUN_OCR_ACCESS_KEY_SECRET（必填）
- ALIYUN_OCR_ENDPOINT：默认 ocr-api.cn-hangzhou.aliyuncs.com（不含 https://）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
from urllib import error, request
from urllib.parse import parse_qs, quote, urlparse

logger = logging.getLogger("ai_erp_api")

_MAX_BODY_BYTES = 10 * 1024 * 1024


def _percent_encode(s: str) -> str:
    if s is None:
        return ""
    return quote(str(s), safe="~").replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def _compute_signature(url: str, access_key_secret: str, http_method: str) -> str:
    """与阿里云 OCR 文档 Python 示例一致的签名。"""
    queries = parse_qs(urlparse(url).query, keep_blank_values=True)
    keys = sorted(queries.keys())
    canonical = ""
    for k in keys:
        if k == "Signature":
            continue
        vals = queries.get(k) or []
        if not vals:
            continue
        v = vals[0]
        canonical += "&" + _percent_encode(k) + "=" + _percent_encode(v)
    canonical = canonical[1:] if canonical.startswith("&") else canonical
    string_to_sign = http_method + "&" + _percent_encode("/") + "&" + _percent_encode(canonical)
    key = (access_key_secret or "") + "&"
    digest = hmac.new(key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _common_params(access_key_id: str) -> Dict[str, str]:
    return {
        "Action": "RecognizeGeneral",
        "Version": "2021-07-07",
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
    }


def _build_signed_post_url(endpoint: str, access_key_id: str, access_key_secret: str) -> str:
    host = (endpoint or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not host:
        host = "ocr-api.cn-hangzhou.aliyuncs.com"
    params = _common_params(access_key_id)
    query = "&".join(f"{k}={_percent_encode(v)}" for k, v in sorted(params.items()))
    base = f"https://{host}/?{query}"
    sig = _compute_signature(base, access_key_secret, "POST")
    return base + "&Signature=" + _percent_encode(sig)


def _parse_recognize_response(body: str) -> Tuple[str, str]:
    try:
        outer = json.loads(body)
    except json.JSONDecodeError:
        return "", "aliyun_bad_json"
    if not isinstance(outer, dict):
        return "", "aliyun_bad_json"
    if outer.get("Code"):
        code = str(outer.get("Code", ""))
        msg = str(outer.get("Message", ""))
        logger.warning("ocr_aliyun_api_error code=%s message=%s", code, msg[:500])
        return "", f"aliyun_api_{code}"
    data_raw = outer.get("Data")
    if not isinstance(data_raw, str) or not data_raw.strip():
        return "", "aliyun_empty_data"
    try:
        inner = json.loads(data_raw)
    except json.JSONDecodeError:
        return "", "aliyun_data_not_json"
    if not isinstance(inner, dict):
        return "", "aliyun_data_not_json"
    content = str(inner.get("content") or "").strip()
    return content, "aliyun_recognize_general"


def recognize_general_image_bytes(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    调用 RecognizeGeneral：POST URL 带签名，body 为图片二进制（application/octet-stream）。
    """
    if not raw:
        return "", "empty"
    if len(raw) > _MAX_BODY_BYTES:
        logger.warning("ocr_aliyun_image_too_large bytes=%s file_name=%s", len(raw), file_name)
        return "", "aliyun_image_too_large"

    ak = (os.getenv("ALIYUN_OCR_ACCESS_KEY_ID") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID") or "").strip()
    sk = (os.getenv("ALIYUN_OCR_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or "").strip()
    if not ak or not sk:
        logger.warning("ocr_aliyun_missing_credentials file_name=%s", file_name)
        return "", "aliyun_not_configured"

    endpoint = (os.getenv("ALIYUN_OCR_ENDPOINT") or "ocr-api.cn-hangzhou.aliyuncs.com").strip()

    try:
        timeout = float(os.getenv("ALIYUN_OCR_TIMEOUT_SECONDS", "60").strip() or "60")
    except ValueError:
        timeout = 60.0
    timeout = max(5.0, min(timeout, 120.0))

    verify = os.getenv("ALIYUN_OCR_SSL_VERIFY", "true").strip().lower() not in {"0", "false", "no", "off"}
    import ssl

    ctx = None if verify else ssl._create_unverified_context()

    try:
        url = _build_signed_post_url(endpoint, ak, sk)
        req = request.Request(
            url,
            data=raw,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text_body = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        logger.warning("ocr_aliyun_http_error status=%s file_name=%s body_head=%s", exc.code, file_name, err_body[:300])
        return "", f"aliyun_http_{exc.code}"
    except error.URLError as exc:
        logger.warning("ocr_aliyun_network file_name=%s err=%s", file_name, exc)
        return "", "aliyun_network"
    except Exception as exc:
        logger.exception("ocr_aliyun_failed file_name=%s err=%s", file_name, exc)
        return "", "aliyun_error"

    return _parse_recognize_response(text_body)


def aliyun_credentials_configured() -> bool:
    ak = (os.getenv("ALIYUN_OCR_ACCESS_KEY_ID") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID") or "").strip()
    sk = (os.getenv("ALIYUN_OCR_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or "").strip()
    return bool(ak and sk)

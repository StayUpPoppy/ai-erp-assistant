from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List
from urllib import error, request

logger = logging.getLogger("ai_erp_api")


class LlmClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def llm_extract_enabled() -> bool:
    return os.getenv("LLM_EXTRACT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def llm_model_name() -> str:
    return (os.getenv("LLM_MODEL") or "deepseek-v4-pro").strip() or "deepseek-v4-pro"


def llm_prompt_version() -> str:
    return (os.getenv("LLM_PROMPT_VERSION") or "deepseek-order-preview-v2").strip() or "deepseek-order-preview-v2"


def llm_reasoning_effort() -> str:
    return (os.getenv("LLM_REASONING_EFFORT") or "high").strip() or "high"


def llm_temperature() -> float:
    raw = (os.getenv("LLM_TEMPERATURE") or "0").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def llm_max_tokens() -> int:
    raw = (os.getenv("LLM_MAX_TOKENS") or "8192").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8192


def _llm_timeout_seconds() -> float:
    raw = (os.getenv("LLM_TIMEOUT_SECONDS") or "90").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 90.0


def _llm_base_url() -> str:
    return (os.getenv("LLM_BASE_URL") or "https://api.deepseek.com").strip().rstrip("/")


def _llm_api_key() -> str:
    return (os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY") or "").strip()


def llm_base_url() -> str:
    return _llm_base_url()


def llm_api_key_configured() -> bool:
    return bool(_llm_api_key())


def llm_available() -> bool:
    return llm_extract_enabled() and bool(_llm_api_key())


def _is_anthropic_compatible_base(base_url: str) -> bool:
    return base_url.rstrip("/").endswith("/anthropic")


def _split_system_and_messages(messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, str]]]:
    system_parts: List[str] = []
    out: List[Dict[str, str]] = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        out.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return "\n\n".join(system_parts), out


def _chat_completion_openai(
    base_url: str,
    api_key: str,
    messages: List[Dict[str, str]],
    *,
    json_response: bool,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> str:
    api_key = _llm_api_key()
    if not api_key:
        raise LlmClientError("missing LLM API key")
    payload: Dict[str, Any] = {
        "model": llm_model_name(),
        "messages": messages,
        "stream": False,
        "temperature": llm_temperature(),
        "max_tokens": max_tokens or llm_max_tokens(),
    }
    if json_response:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    timeout = timeout_seconds or _llm_timeout_seconds()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        logger.warning("llm_http_error status=%s body_prefix=%s", exc.code, raw[:500])
        raise LlmClientError(f"LLM HTTP {exc.code}", status_code=exc.code, body=raw) from exc
    except error.URLError as exc:
        raise LlmClientError(f"LLM network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmClientError("LLM request timed out") from exc

    try:
        parsed = json.loads(raw)
        choice = parsed["choices"][0]
        content = str(choice["message"]["content"])
        if choice.get("finish_reason") == "length":
            raise LlmClientError("LLM response truncated by max_tokens", body=raw)
        return content
    except Exception as exc:
        logger.warning("llm_bad_response body_prefix=%s", raw[:500])
        raise LlmClientError("LLM response parse failed", body=raw) from exc


def _chat_completion_openai_stream(
    base_url: str,
    api_key: str,
    messages: List[Dict[str, str]],
) -> Iterator[str]:
    payload: Dict[str, Any] = {
        "model": llm_model_name(),
        "messages": messages,
        "stream": True,
        "temperature": llm_temperature(),
        "max_tokens": llm_max_tokens(),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "90") or "90")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("llm_stream_bad_json provider=openai body_prefix=%s", data[:500])
                    continue
                choices = parsed.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield str(text)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        logger.warning("llm_http_error provider=openai stream=true status=%s body_prefix=%s", exc.code, raw[:500])
        raise LlmClientError(f"LLM HTTP {exc.code}", status_code=exc.code, body=raw) from exc
    except error.URLError as exc:
        raise LlmClientError(f"LLM network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmClientError("LLM stream timed out") from exc


def _chat_completion_anthropic(
    base_url: str,
    api_key: str,
    messages: List[Dict[str, str]],
    *,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> str:
    system, anthropic_messages = _split_system_and_messages(messages)
    payload: Dict[str, Any] = {
        "model": llm_model_name(),
        "messages": anthropic_messages,
        "max_tokens": max_tokens or llm_max_tokens(),
        "temperature": llm_temperature(),
    }
    if system:
        payload["system"] = system
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url}/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
        },
    )
    timeout = timeout_seconds or _llm_timeout_seconds()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        logger.warning("llm_http_error provider=anthropic status=%s body_prefix=%s", exc.code, raw[:500])
        raise LlmClientError(f"LLM HTTP {exc.code}", status_code=exc.code, body=raw) from exc
    except error.URLError as exc:
        raise LlmClientError(f"LLM network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmClientError("LLM request timed out") from exc

    try:
        parsed = json.loads(raw)
        blocks = parsed.get("content") or []
        text_parts: List[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        if text_parts:
            text = "".join(text_parts)
            if parsed.get("stop_reason") == "max_tokens":
                raise LlmClientError("LLM response truncated by max_tokens", body=raw)
            return text
        if isinstance(parsed.get("error"), dict):
            message = str(parsed["error"].get("message") or parsed["error"])
            raise LlmClientError(f"LLM provider error: {message}", body=raw)
        raise KeyError("content[0].text")
    except Exception as exc:
        logger.warning("llm_bad_response provider=anthropic body_prefix=%s", raw[:500])
        raise LlmClientError("LLM response parse failed", body=raw) from exc


def _chat_completion_anthropic_stream(
    base_url: str,
    api_key: str,
    messages: List[Dict[str, str]],
) -> Iterator[str]:
    system, anthropic_messages = _split_system_and_messages(messages)
    payload: Dict[str, Any] = {
        "model": llm_model_name(),
        "messages": anthropic_messages,
        "max_tokens": llm_max_tokens(),
        "temperature": llm_temperature(),
        "stream": True,
    }
    if system:
        payload["system"] = system
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url}/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
        },
    )
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "90") or "90")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("llm_stream_bad_json provider=anthropic body_prefix=%s", data[:500])
                    continue
                if isinstance(parsed.get("error"), dict):
                    message = str(parsed["error"].get("message") or parsed["error"])
                    raise LlmClientError(f"LLM provider error: {message}", body=data)
                delta = parsed.get("delta") or {}
                text = delta.get("text")
                if text:
                    yield str(text)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        logger.warning("llm_http_error provider=anthropic stream=true status=%s body_prefix=%s", exc.code, raw[:500])
        raise LlmClientError(f"LLM HTTP {exc.code}", status_code=exc.code, body=raw) from exc
    except error.URLError as exc:
        raise LlmClientError(f"LLM network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmClientError("LLM stream timed out") from exc


def chat_completion_json(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> str:
    api_key = _llm_api_key()
    if not api_key:
        raise LlmClientError("missing LLM API key")
    base_url = _llm_base_url()
    if _is_anthropic_compatible_base(base_url):
        return _chat_completion_anthropic(
            base_url,
            api_key,
            messages,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    return _chat_completion_openai(
        base_url,
        api_key,
        messages,
        json_response=True,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )


def chat_completion_text(messages: List[Dict[str, str]]) -> str:
    api_key = _llm_api_key()
    if not api_key:
        raise LlmClientError("missing LLM API key")
    base_url = _llm_base_url()
    if _is_anthropic_compatible_base(base_url):
        return _chat_completion_anthropic(base_url, api_key, messages)
    return _chat_completion_openai(base_url, api_key, messages, json_response=False)


def chat_completion_text_stream(messages: List[Dict[str, str]]) -> Iterator[str]:
    api_key = _llm_api_key()
    if not api_key:
        raise LlmClientError("missing LLM API key")
    base_url = _llm_base_url()
    if _is_anthropic_compatible_base(base_url):
        yield from _chat_completion_anthropic_stream(base_url, api_key, messages)
        return
    yield from _chat_completion_openai_stream(base_url, api_key, messages)

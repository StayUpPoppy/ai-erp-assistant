"""
可切换 OCR：Tesseract（默认）/ HTTP 远程服务 / PaddleOCR（可选）/ 阿里云 RecognizeGeneral（直连）。

- ``OCR_ENGINE=http`` + ``OCR_HTTP_URL``：自建网关转发各云 OCR。
- ``OCR_ENGINE=aliyun``：直连阿里云通用文字识别（需 AK/SK，见 ``aliyun_ocr.py``）。
- ``OCR_ENGINE=paddle``：见 ``requirements-ocr-paddle.txt``。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

logger = logging.getLogger("ai_erp_api")

_paddle_ocr_lock = None
_paddle_ocr_instance: Any = None
_paddle_ocr_sig: Optional[Tuple[str, bool]] = None


def _paddle_lock():
    global _paddle_ocr_lock
    if _paddle_ocr_lock is None:
        import threading

        _paddle_ocr_lock = threading.Lock()
    return _paddle_ocr_lock

_tesseract_autodetect_logged = False

# 出现这些情况时 PDF 多页 OCR 无必要继续逐页尝试
OCR_FATAL_OCR_FORMATS = frozenset(
    {
        "tesseract_not_found",
        "paddleocr_not_installed",
        "ocr_http_not_configured",
        "aliyun_not_configured",
        "pillow_not_installed",
        "pytesseract_not_installed",
        "pytesseract_import_failed",
    },
)


def _normalize_ocr_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _windows_tesseract_static_paths() -> list[str]:
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    paths: list[str] = []
    if local:
        paths.append(os.path.join(local, "Programs", "Tesseract-OCR", "tesseract.exe"))
    paths.extend(
        [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ],
    )
    return paths


def resolve_tesseract_executable_path() -> tuple[str | None, str]:
    cmd = (os.getenv("TESSERACT_CMD") or "").strip()
    if cmd:
        if os.path.isfile(cmd):
            return cmd, "env"
        return None, "env_invalid"

    if platform.system() == "Windows":
        for path in _windows_tesseract_static_paths():
            if path and os.path.isfile(path):
                return path, "autodetect_windows"

    which = shutil.which("tesseract")
    if which:
        return which, "path"
    return None, "not_found"


def _configure_pytesseract_cmd() -> None:
    global _tesseract_autodetect_logged
    try:
        import pytesseract
    except Exception as exc:
        logger.warning("ocr_pytesseract_import_failed err=%s", exc)
        return

    cmd = (os.getenv("TESSERACT_CMD") or "").strip()
    if cmd and not os.path.isfile(cmd):
        logger.warning("ocr_tesseract_cmd_invalid cmd=%s", cmd)

    path, how = resolve_tesseract_executable_path()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path
        if how == "autodetect_windows" and not _tesseract_autodetect_logged:
            logger.info("ocr_tesseract_autodetected cmd=%s", path)
            _tesseract_autodetect_logged = True


def _ocr_tesseract(raw: bytes, file_name: str, lang_override: str | None = None) -> Tuple[str, str]:
    if not raw:
        return "", "empty"
    tesseract_cmd, _how = resolve_tesseract_executable_path()
    if not tesseract_cmd:
        logger.warning("ocr_tesseract_not_found file_name=%s", file_name)
        return "", "tesseract_not_found"
    lang = (lang_override or os.getenv("TESSERACT_OCR_LANG") or "chi_sim+eng").strip() or "chi_sim+eng"
    timeout_raw = (os.getenv("TESSERACT_TIMEOUT_SECONDS") or "120").strip()
    try:
        timeout = max(5.0, min(float(timeout_raw), 300.0))
    except ValueError:
        timeout = 120.0

    suffix = Path(file_name.split("#", 1)[0]).suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        suffix = ".png"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        proc = subprocess.run(
            [tesseract_cmd, tmp_path, "stdout", "-l", lang],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:500]
            logger.warning("ocr_tesseract_cli_failed file_name=%s code=%s stderr=%s", file_name, proc.returncode, err)
            return "", "ocr_tesseract_cli_failed"
        raw_text = proc.stdout or ""
        text = _normalize_ocr_text(raw_text)
        if not text:
            logger.info("ocr_tesseract_empty file_name=%s bytes=%s lang=%s", file_name, len(raw), lang)
        return text, f"ocr_tesseract_cli(lang={lang})"
    except subprocess.TimeoutExpired:
        logger.warning("ocr_tesseract_timeout file_name=%s timeout=%s", file_name, timeout)
        return "", "ocr_tesseract_timeout"
    except Exception as exc:
        en = type(exc).__name__
        msg = str(exc).lower()
        if en == "TesseractNotFoundError" or "tesseract" in msg and ("not installed" in msg or "not in your path" in msg):
            logger.warning("ocr_tesseract_not_found file_name=%s", file_name)
            return "", "tesseract_not_found"
        logger.exception("ocr_tesseract_failed file_name=%s bytes=%s", file_name, len(raw))
        return "", "ocr_error"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _json_get_text_field(payload: Any, path: str) -> str:
    if not path:
        return ""
    cur: Any = payload
    for p in path.split("."):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(p)
    if cur is None:
        return ""
    return str(cur).strip()


def _ocr_http(raw: bytes, file_name: str, lang_override: str | None = None) -> Tuple[str, str]:
    url = (os.getenv("OCR_HTTP_URL") or "").strip()
    if not url:
        return "", "ocr_http_not_configured"

    img_field = (os.getenv("OCR_HTTP_REQUEST_IMAGE_FIELD") or "image_base64").strip() or "image_base64"
    lang_field = (os.getenv("OCR_HTTP_REQUEST_LANG_FIELD") or "lang").strip() or "lang"
    lang = (lang_override or os.getenv("OCR_HTTP_LANG") or os.getenv("TESSERACT_OCR_LANG") or "chi_sim+eng").strip() or "chi_sim+eng"
    text_path = (os.getenv("OCR_HTTP_RESPONSE_TEXT_FIELD") or "text").strip() or "text"

    body: Dict[str, Any] = {
        img_field: base64.standard_b64encode(raw).decode("ascii"),
        lang_field: lang,
    }
    extra_raw = (os.getenv("OCR_HTTP_REQUEST_EXTRA_JSON") or "").strip()
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
            if isinstance(extra, dict):
                body.update(extra)
        except json.JSONDecodeError as exc:
            logger.warning("ocr_http_extra_json_invalid err=%s", exc)

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    hdr_raw = (os.getenv("OCR_HTTP_HEADERS_JSON") or "").strip()
    if hdr_raw:
        try:
            h2 = json.loads(hdr_raw)
            if isinstance(h2, dict):
                for k, v in h2.items():
                    if k and v is not None:
                        headers[str(k)] = str(v)
        except json.JSONDecodeError as exc:
            logger.warning("ocr_http_headers_json_invalid err=%s", exc)

    try:
        timeout = float(os.getenv("OCR_HTTP_TIMEOUT_SECONDS", "60").strip() or "60")
    except ValueError:
        timeout = 60.0
    timeout = max(3.0, min(timeout, 300.0))

    verify = os.getenv("OCR_HTTP_SSL_VERIFY", "true").strip().lower() not in {"0", "false", "no", "off"}
    ctx = None if verify else ssl._create_unverified_context()

    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        logger.warning("ocr_http_http_error file_name=%s status=%s", file_name, exc.code)
        return "", f"ocr_http_http_{exc.code}"
    except error.URLError as exc:
        logger.warning("ocr_http_url_error file_name=%s err=%s", file_name, exc)
        return "", "ocr_http_network"
    except Exception as exc:
        logger.exception("ocr_http_failed file_name=%s err=%s", file_name, exc)
        return "", "ocr_http_error"

    try:
        out = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("ocr_http_bad_json file_name=%s head=%s", file_name, raw_body[:200])
        return "", "ocr_http_bad_json"

    if not isinstance(out, dict):
        return "", "ocr_http_bad_json"

    text = _json_get_text_field(out, text_path)
    text = _normalize_ocr_text(text)
    if not text:
        logger.info("ocr_http_empty file_name=%s field=%s", file_name, text_path)
    return text, f"ocr_http(field={text_path})"


def _get_paddle_ocr_singleton(lang_override: str | None = None):
    """PaddleOCR 初始化较重，进程内复用同一实例（按 lang + use_gpu 维度）。"""
    global _paddle_ocr_instance, _paddle_ocr_sig
    from paddleocr import PaddleOCR  # type: ignore[import-not-found]

    lang = (lang_override or os.getenv("PADDLE_OCR_LANG") or "ch").strip() or "ch"
    use_gpu = os.getenv("PADDLE_OCR_USE_GPU", "false").strip().lower() in {"1", "true", "yes", "on"}
    sig = (lang, use_gpu)
    with _paddle_lock():
        if _paddle_ocr_instance is not None and _paddle_ocr_sig == sig:
            return _paddle_ocr_instance, lang
        kwargs: Dict[str, Any] = {"use_angle_cls": True, "lang": lang, "show_log": False}
        try:
            inst = PaddleOCR(**kwargs, use_gpu=use_gpu)
        except TypeError:
            inst = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        _paddle_ocr_instance = inst
        _paddle_ocr_sig = sig
        logger.info("ocr_paddle_engine_initialized lang=%s use_gpu=%s", lang, use_gpu)
        return inst, lang


def _paddle_collect_lines(result: Any) -> list[str]:
    """兼容不同版本 PaddleOCR 的返回结构。"""
    lines: list[str] = []
    if not result:
        return lines
    first = result[0]
    candidates: list[Any]
    if isinstance(first, list) and first and isinstance(first[0], (list, tuple)):
        candidates = first
    else:
        candidates = list(result) if isinstance(result, list) else []
    for item in candidates:
        if not item or not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        txt_cell = item[1]
        if isinstance(txt_cell, (list, tuple)) and len(txt_cell) >= 1:
            t = str(txt_cell[0] or "").strip()
            if t:
                lines.append(t)
    return lines


def _ocr_paddle(raw: bytes, file_name: str, lang_override: str | None = None) -> Tuple[str, str]:
    try:
        from paddleocr import PaddleOCR  # noqa: F401
    except ImportError:
        logger.warning("ocr_paddleocr_not_installed file_name=%s", file_name)
        return "", "paddleocr_not_installed"

    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return "", "pillow_not_installed"

    try:
        img = Image.open(BytesIO(raw)).convert("RGB")
        arr = np.array(img)
    except Exception as exc:
        logger.warning("ocr_paddle_image_decode_fail file_name=%s err=%s", file_name, exc)
        return "", "ocr_error"

    try:
        ocr, lang = _get_paddle_ocr_singleton(lang_override)
        result = ocr.ocr(arr, cls=True)
    except Exception as exc:
        logger.exception("ocr_paddle_run_failed file_name=%s err=%s", file_name, exc)
        return "", "ocr_error"

    lines = _paddle_collect_lines(result)
    text = _normalize_ocr_text("\n".join(lines))
    if not text:
        logger.info("ocr_paddle_empty file_name=%s", file_name)
    return text, f"ocr_paddle(lang={lang})"


def ocr_image_bytes(
    raw: bytes,
    file_name: str = "",
    *,
    engine_override: str | None = None,
    paddle_lang_override: str | None = None,
    tesseract_lang_override: str | None = None,
    http_lang_override: str | None = None,
    auto_fallback_override: bool | None = None,
) -> Tuple[str, str]:
    """
    按 OCR_ENGINE 选择引擎；支持自动回退到 Tesseract（OCR_ENGINE_AUTO_FALLBACK）。
    """
    if not raw:
        return "", "empty"

    engine = (engine_override or os.getenv("OCR_ENGINE") or "tesseract").strip().lower() or "tesseract"
    auto_fb = (
        auto_fallback_override
        if auto_fallback_override is not None
        else (os.getenv("OCR_ENGINE_AUTO_FALLBACK") or "true").strip().lower() not in {"0", "false", "no", "off"}
    )

    def _try_tesseract(reason: str) -> Tuple[str, str]:
        if tesseract_lang_override is None:
            t2, f2 = _ocr_tesseract(raw, file_name)
        else:
            t2, f2 = _ocr_tesseract(raw, file_name, lang_override=tesseract_lang_override)
        if t2:
            return t2, f"{reason}+fallback({f2})"
        return t2, f2

    if engine == "http":
        if not (os.getenv("OCR_HTTP_URL") or "").strip():
            logger.warning("ocr_engine_http_selected_but_missing_url")
            if auto_fb:
                return _try_tesseract("ocr_http_skipped_no_url")
            return "", "ocr_http_not_configured"
        if http_lang_override is None:
            text, fmt = _ocr_http(raw, file_name)
        else:
            text, fmt = _ocr_http(raw, file_name, lang_override=http_lang_override)
        if text:
            return text, fmt
        if auto_fb and fmt not in OCR_FATAL_OCR_FORMATS:
            return _try_tesseract(f"{fmt}_empty")
        return text, fmt

    if engine == "aliyun":
        from app.aliyun_ocr import recognize_general_image_bytes

        text, fmt = recognize_general_image_bytes(raw, file_name)
        if text:
            return text, fmt
        if auto_fb and (fmt == "aliyun_not_configured" or fmt not in OCR_FATAL_OCR_FORMATS):
            return _try_tesseract(fmt if fmt != "aliyun_not_configured" else "aliyun_skip_no_creds")
        return text, fmt

    if engine == "paddle":
        if paddle_lang_override is None:
            text, fmt = _ocr_paddle(raw, file_name)
        else:
            text, fmt = _ocr_paddle(raw, file_name, lang_override=paddle_lang_override)
        if text:
            return text, fmt
        if fmt == "paddleocr_not_installed" or not auto_fb:
            return text, fmt
        if fmt in OCR_FATAL_OCR_FORMATS:
            return text, fmt
        return _try_tesseract(f"{fmt}_empty")

    if tesseract_lang_override is None:
        return _ocr_tesseract(raw, file_name)
    return _ocr_tesseract(raw, file_name, lang_override=tesseract_lang_override)


def _tesseract_available_languages(tesseract_cmd: str | None) -> list[str]:
    if not tesseract_cmd:
        return []
    try:
        proc = subprocess.run(
            [tesseract_cmd, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        logger.warning("ocr_tesseract_list_langs_failed err=%s", exc)
        return []
    if proc.returncode != 0:
        logger.warning("ocr_tesseract_list_langs_nonzero code=%s stderr=%s", proc.returncode, (proc.stderr or "")[:300])
        return []
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    return [line for line in lines if not line.lower().startswith("list of available languages")]


def build_ocr_health_payload() -> dict[str, object]:
    """供 GET /health：Tesseract 探测 + 当前 OCR 策略摘要。"""
    out: dict[str, object] = {}
    engine = (os.getenv("OCR_ENGINE") or "tesseract").strip().lower() or "tesseract"
    out["ocr_engine"] = engine
    out["paddle_ocr_lang"] = (os.getenv("PADDLE_OCR_LANG") or "ch").strip() or "ch"
    out["paddle_ocr_use_gpu"] = os.getenv("PADDLE_OCR_USE_GPU", "false").strip().lower() in {"1", "true", "yes", "on"}
    out["tesseract_ocr_lang"] = (os.getenv("TESSERACT_OCR_LANG") or "chi_sim+eng").strip() or "chi_sim+eng"
    out["ocr_http_url_configured"] = bool((os.getenv("OCR_HTTP_URL") or "").strip())
    out["ocr_engine_auto_fallback"] = (os.getenv("OCR_ENGINE_AUTO_FALLBACK") or "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        import importlib.util

        out["paddleocr_importable"] = importlib.util.find_spec("paddleocr") is not None
        out["paddleocr_initializable"] = False
        out["paddleocr_init_status"] = "not_checked"
        if out["paddleocr_importable"] and os.getenv("OCR_HEALTH_INIT_PADDLE", "false").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                _get_paddle_ocr_singleton(str(out["paddle_ocr_lang"]))
                out["paddleocr_initializable"] = True
                out["paddleocr_init_status"] = "ok"
            except Exception as exc:
                out["paddleocr_initializable"] = False
                out["paddleocr_init_status"] = f"failed:{type(exc).__name__}"
                logger.warning("ocr_health_paddle_init_failed err=%s", exc)
    except Exception:
        out["paddleocr_importable"] = False
        out["paddleocr_initializable"] = False
        out["paddleocr_init_status"] = "import_probe_failed"

    try:
        from app.aliyun_ocr import aliyun_credentials_configured

        out["aliyun_ocr_configured"] = aliyun_credentials_configured()
    except Exception:
        out["aliyun_ocr_configured"] = False

    try:
        import importlib.util

        pytesseract_importable = importlib.util.find_spec("pytesseract") is not None
    except Exception:
        pytesseract_importable = False

    if not pytesseract_importable:
        out["tesseract_available"] = False
        out["tesseract_resolution"] = "pytesseract_not_installed"
        out["tesseract_cmd"] = None
        return out

    path, how = resolve_tesseract_executable_path()
    out["tesseract_available"] = path is not None
    out["tesseract_resolution"] = how
    out["tesseract_cmd"] = path
    languages = _tesseract_available_languages(path)
    out["tesseract_languages"] = languages
    out["tesseract_has_chi_sim"] = "chi_sim" in languages
    out["ocr_chinese_ready"] = bool(
        (engine == "paddle" and out.get("paddleocr_importable") and str(out.get("paddle_ocr_lang") or "").lower().startswith("ch"))
        or out["tesseract_has_chi_sim"]
        or (engine in {"http", "aliyun"} and (out.get("ocr_http_url_configured") or out.get("aliyun_ocr_configured")))
    )

    return out

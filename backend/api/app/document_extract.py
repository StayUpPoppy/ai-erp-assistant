"""
从上传二进制中提取可读文本（PDF 文本层、纯文本、.docx 正文、常见图片 OCR）。

说明：
- `.docx`：标准 OOXML zip，读取 `word/document.xml` 中 `w:t` 文本，无需额外 pip 包；
- 图片 / 扫描 PDF 页 OCR：默认 **Tesseract**；可改 ``OCR_ENGINE=http``（自建网关）、``OCR_ENGINE=aliyun``（直连阿里云 RecognizeGeneral）、``OCR_ENGINE=paddle``（需另装 PaddleOCR）；见 ``app/ocr_engine.py``、``app/aliyun_ocr.py`` 与根目录 ``.env.example``。
- 未安装或语言包缺失时返回空字符串 + 明确 format_label，不阻断工作流；
- 扫描 PDF：若文字层极少（默认少于 48 字符），尝试用 PyMuPDF 将首页渲染为图再走当前 OCR 引擎（无需 Poppler）；可用环境变量关闭或调阈值。
- **PDF 文字层**：优先 pypdf；若未安装则自动用 **PyMuPDF `get_text`** 抽取（与稀疏页 OCR 共用 fitz），避免仅因缺 pypdf 导致 0 字符。
- **CSV**：按表格解析为「单元格 | 单元格」行文本，便于下游规则抽取与审计区分 `csv_rows(编码)`。
- **XLSX**：读取共享字符串与各 sheet 单元格（inlineStr / 共享索引），无 openpyxl；纯数字格可能略少。
- **纯文本类**（txt/md/json/xml/log）与 **CSV 解码**：固定编码失败后尝试 **charset-normalizer**（pip 包 `charset-normalizer`）。
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import re
import zipfile
from io import BytesIO, StringIO
from time import perf_counter
from typing import Tuple
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

logger = logging.getLogger("ai_erp_api")


def resolve_tesseract_executable_path() -> tuple[str | None, str]:
    """委托 ``ocr_engine``（供脚本或诊断复用）。"""
    from app.ocr_engine import resolve_tesseract_executable_path as _resolve

    return _resolve()


def tesseract_health_payload() -> dict[str, object]:
    """供 GET /health：含 Tesseract 与 OCR_ENGINE 策略摘要（名称保留兼容旧客户端）。"""
    from app.ocr_engine import build_ocr_health_payload

    return build_ocr_health_payload()


# 常见单据扫描/拍照格式
_IMAGE_OCR_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp"})


def guess_extension(file_name: str) -> str:
    name = (file_name or "").strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


def object_key_to_display_name(object_key: str | None) -> str:
    """从 MinIO object key 还原原始文件名（与 storage_client.save_binary_file 规则一致）。"""
    if not object_key:
        return ""
    tail = object_key.rsplit("/", 1)[-1]
    if "-" in tail:
        return tail.split("-", 1)[1]
    return tail


def resolved_upload_file_name(object_key: str | None, source_file_name: str | None = None) -> str:
    """
    工作流分类/解析使用的逻辑文件名：优先从对象 key 还原；降级（无对象存储或未落 key）时用上传时记录的文件名。
    """
    from_key = object_key_to_display_name(object_key).strip()
    if from_key:
        return from_key
    return (source_file_name or "").strip()


def extract_text_from_image(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    图片 OCR：由 ``OCR_ENGINE`` 选择 Tesseract / HTTP / PaddleOCR（见 ``app/ocr_engine.py``）。
    """
    from app.ocr_engine import ocr_image_bytes

    return ocr_image_bytes(raw, file_name)


def _pixmap_to_png_bytes(pix) -> bytes:
    try:
        return pix.tobytes("png")
    except Exception:
        from PIL import Image

        tmp = BytesIO()
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(tmp, format="PNG")
        return tmp.getvalue()


def _ocr_pdf_pages_supplement(
    raw: bytes,
    file_name: str = "",
    max_pages_override: int | None = None,
    *,
    ocr_kwargs: dict[str, object] | None = None,
) -> Tuple[str, int]:
    """
    将 PDF 前 N 页逐页渲染为 PNG 后 OCR，拼接正文。
    页数上限：环境变量 PDF_OCR_MAX_PAGES（默认 3，最大 20）。
    返回 (合并后的 OCR 文本, 实际尝试的页数)。
    """
    from app.ocr_engine import OCR_FATAL_OCR_FORMATS, ocr_image_bytes

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.info("document_extract_pymupdf_missing skip_pdf_page_ocr")
        return "", 0

    if max_pages_override is not None:
        max_pages = max_pages_override
    else:
        try:
            max_pages = int(os.getenv("PDF_OCR_MAX_PAGES", "5").strip() or "5")
        except ValueError:
            max_pages = 3
    max_pages = max(1, min(max_pages, 20))

    doc = None
    chunks: list[str] = []
    pages_tried = 0
    started = perf_counter()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        n = min(doc.page_count, max_pages)
        mat = fitz.Matrix(2.0, 2.0)
        for i in range(n):
            pages_tried = i + 1
            try:
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_bytes = _pixmap_to_png_bytes(pix)
                ocr_text, occ_fmt = ocr_image_bytes(png_bytes, f"{file_name}#pdf_p{i + 1}.png", **(ocr_kwargs or {}))
                if occ_fmt in OCR_FATAL_OCR_FORMATS:
                    logger.warning(
                        "document_extract_pdf_page_ocr_fatal file_name=%s page=%s fmt=%s",
                        file_name,
                        i + 1,
                        occ_fmt,
                    )
                    break
                if ocr_text:
                    chunks.append(ocr_text)
            except Exception:
                logger.warning("document_extract_pdf_page_ocr_failed file_name=%s page=%s", file_name, i + 1)
        text = _normalize_text("\n\n".join(chunks))
        logger.info(
            "document_extract_pdf_page_ocr_done file_name=%s pages=%s chars=%s elapsed_ms=%s",
            file_name,
            pages_tried,
            len(text),
            int((perf_counter() - started) * 1000),
        )
        return text, pages_tried
    except Exception:
        logger.exception("document_extract_pdf_pages_ocr_failed file_name=%s", file_name)
        return "", pages_tried
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def _mineru_pdf_supplement(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    try:
        from app.mineru_client import MineruClientError, parse_pdf_bytes_with_mineru

        text, fmt = parse_pdf_bytes_with_mineru(raw, file_name or "document.pdf")
        if text:
            return _normalize_text(text), fmt
        return "", fmt
    except MineruClientError as exc:
        logger.warning("document_extract_mineru_failed file_name=%s err=%s", file_name, exc)
        return "", "mineru_error"
    except Exception as exc:
        logger.exception("document_extract_mineru_unexpected file_name=%s err=%s", file_name, exc)
        return "", "mineru_error"


def extract_pdf_text_with_forced_ocr(raw: bytes, file_name: str = "", max_pages: int = 3) -> Tuple[str, str]:
    """Extract a PDF text layer plus forced rendered-page OCR, ignoring sparse-text thresholds."""
    if not raw:
        return "", "empty"
    if guess_extension(file_name) != "pdf":
        return extract_text_from_bytes(raw, file_name)

    started = perf_counter()
    text, engine = _extract_pdf_text_layer(raw, file_name)
    if engine == "no_engine":
        return "", "pdf_no_text_engine"
    if engine == "pdf_error":
        return "", "pdf_error"

    extra, pages_done = _ocr_pdf_pages_supplement(raw, file_name, max_pages_override=max_pages)
    base = "pdf_text" if engine == "pypdf" else "pdf_text_pymupdf"
    if extra:
        merged = _normalize_text(text + "\n" + extra)
        suffix = "forced_ocr_first_page" if pages_done <= 1 else f"forced_ocr_pages_{pages_done}"
        logger.info(
            "document_extract_pdf_forced_ocr_done file_name=%s format=%s+%s chars=%s elapsed_ms=%s",
            file_name,
            base,
            suffix,
            len(merged),
            int((perf_counter() - started) * 1000),
        )
        return merged, f"{base}+{suffix}"

    logger.warning(
        "document_extract_pdf_forced_ocr_empty file_name=%s base=%s text_chars=%s pages=%s elapsed_ms=%s",
        file_name,
        base,
        len(text),
        pages_done,
        int((perf_counter() - started) * 1000),
    )
    return text, f"{base}+forced_ocr_empty"


def extract_pdf_text_with_forced_chinese_ocr(raw: bytes, file_name: str = "", max_pages: int = 3) -> Tuple[str, str]:
    """Extract PDF text plus a forced Chinese OCR pass over rendered pages."""
    if not raw:
        return "", "empty"
    if guess_extension(file_name) != "pdf":
        return extract_text_from_bytes(raw, file_name)

    started = perf_counter()
    text, engine = _extract_pdf_text_layer(raw, file_name)
    if engine == "no_engine":
        return "", "pdf_no_text_engine"
    if engine == "pdf_error":
        return "", "pdf_error"

    extra, pages_done = _ocr_pdf_pages_supplement(
        raw,
        file_name,
        max_pages_override=max_pages,
        ocr_kwargs={
            "engine_override": "paddle",
            "paddle_lang_override": "ch",
            "tesseract_lang_override": "chi_sim+eng",
            "auto_fallback_override": True,
        },
    )
    base = "pdf_text" if engine == "pypdf" else "pdf_text_pymupdf"
    if extra:
        merged = _normalize_text(text + "\n" + extra)
        suffix = "ocr_paddle_ch_first_page" if pages_done <= 1 else f"ocr_paddle_ch_pages_{pages_done}"
        logger.info(
            "document_extract_pdf_forced_chinese_ocr_done file_name=%s format=%s+%s chars=%s elapsed_ms=%s",
            file_name,
            base,
            suffix,
            len(merged),
            int((perf_counter() - started) * 1000),
        )
        return merged, f"{base}+{suffix}"

    logger.warning(
        "document_extract_pdf_forced_chinese_ocr_empty file_name=%s base=%s text_chars=%s pages=%s elapsed_ms=%s",
        file_name,
        base,
        len(text),
        pages_done,
        int((perf_counter() - started) * 1000),
    )
    return text, f"{base}+ocr_paddle_ch_empty"


def _extract_pdf_text_layer(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    PDF 文字层：优先 pypdf；未安装或整段失败时回退 PyMuPDF get_text（与 OCR 补全共用 fitz 依赖）。

    返回 (normalized_text, meta)，meta 为：
    - pypdf / pymupdf：成功取文字层（可为空串若扫描件无文字层）；
    - no_engine：pypdf 与 pymupdf 均不可用；
    - pdf_error：文件损坏或打开失败。
    """
    started = perf_counter()
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        parts: list[str] = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            parts.append(t)
        text = _normalize_text("\n".join(parts))
        logger.info(
            "document_extract_pdf_text_layer_done file_name=%s engine=pypdf chars=%s elapsed_ms=%s",
            file_name,
            len(text),
            int((perf_counter() - started) * 1000),
        )
        return text, "pypdf"
    except ImportError as exc:
        logger.info(
            "document_extract_pypdf_missing_try_pymupdf_text file_name=%s exe=%s err=%s",
            file_name,
            sys.executable,
            exc,
        )
    except Exception:
        logger.exception("document_extract_pypdf_failed_try_pymupdf_text file_name=%s", file_name)

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        logger.warning(
            "document_extract_pdf_no_text_engine file_name=%s exe=%s err=%s",
            file_name,
            sys.executable,
            exc,
        )
        return "", "no_engine"

    doc = None
    started = perf_counter()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        parts: list[str] = []
        for i in range(doc.page_count):
            try:
                page = doc.load_page(i)
                parts.append(page.get_text() or "")
            except Exception:
                logger.warning("document_extract_pymupdf_page_text_failed file_name=%s page=%s", file_name, i + 1)
                parts.append("")
        text = _normalize_text("\n".join(parts))
        logger.info(
            "document_extract_pdf_text_layer_done file_name=%s engine=pymupdf chars=%s elapsed_ms=%s",
            file_name,
            len(text),
            int((perf_counter() - started) * 1000),
        )
        return text, "pymupdf"
    except Exception:
        logger.exception("document_extract_pymupdf_text_layer_failed file_name=%s", file_name)
        return "", "pdf_error"
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_text_from_docx(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    从 .docx（OOXML）抽取可见文本：遍历 w:t，不引入 python-docx 依赖。
    """
    if not raw:
        return "", "empty"
    try:
        with zipfile.ZipFile(BytesIO(raw), "r") as zf:
            if "word/document.xml" not in zf.namelist():
                logger.info("document_extract_docx_no_document_xml file_name=%s", file_name)
                return "", "docx_no_document_xml"
            xml_content = zf.read("word/document.xml")
    except zipfile.BadZipFile:
        logger.info("document_extract_docx_bad_zip file_name=%s", file_name)
        return "", "docx_bad_zip"
    except Exception:
        logger.exception("document_extract_docx_open_failed file_name=%s", file_name)
        return "", "docx_error"
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        logger.warning("document_extract_docx_xml_parse_error file_name=%s", file_name)
        return "", "docx_xml_parse_error"
    chunks: list[str] = []
    for el in root.iter(f"{_W_NS}t"):
        if el.text:
            chunks.append(el.text)
        if el.tail:
            chunks.append(el.tail)
    text = _normalize_text(" ".join(chunks))
    return text, "docx_text"


def _decode_text_bytes_best_effort(raw: bytes, file_name: str, *, context: str) -> Tuple[str, str]:
    """
    将原始字节解为文本：先试常见编码，再试 charset_normalizer。
    返回 (decoded, label)；label 为编码名或 ``charset_normalizer:…``；失败返回 ("", "").
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(raw).best()
        if match is not None:
            text = str(match)
            if text.strip():
                enc_l = getattr(match.encoding, "name", None) or str(match.encoding)
                return text, f"charset_normalizer:{enc_l}"
    except ImportError:
        logger.debug("charset_normalizer_not_installed context=%s file_name=%s", context, file_name)
    except Exception:
        logger.exception("charset_normalizer_failed context=%s file_name=%s", context, file_name)
    return "", ""


def _extract_text_from_csv(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    将 CSV 展开为多行「列1 | 列2 | …」，便于 OCR 式正文与 structured_extract；
    分隔符由 csv.Sniffer 推断（逗号/分号/制表符常见导出）。
    """
    decoded, enc_used = _decode_text_bytes_best_effort(raw, file_name, context="csv")
    if not decoded:
        logger.warning("document_extract_csv_decode_failed file_name=%s bytes=%s", file_name, len(raw))
        return "", "csv_decode_failed"

    text = decoded.replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:8192] if len(text) > 8192 else text
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(StringIO(text), dialect)
    lines: list[str] = []
    for row in reader:
        if not row:
            continue
        cells = [(c or "").strip() for c in row]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    out = _normalize_text("\n".join(lines))
    if not out:
        logger.info("document_extract_csv_empty_rows file_name=%s", file_name)
    return out, f"csv_rows({enc_used})"


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_si_plain_text(si: Element) -> str:
    parts: list[str] = []
    for t in si.iter(f"{_XLSX_NS}t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts).strip()


def _extract_text_from_xlsx(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    从 .xlsx（OOXML）抽取可读文本：共享字符串表 + 各 sheet 中单元格（含 inlineStr、s 索引），无 openpyxl 依赖。
    数字型纯数值格会少量漏读，以字符串/内联串为主，满足单据导出场景检索与规则抽取。
    """
    try:
        zf = zipfile.ZipFile(BytesIO(raw), "r")
    except zipfile.BadZipFile:
        logger.info("document_extract_xlsx_bad_zip file_name=%s", file_name)
        return "", "xlsx_bad_zip"
    ss_list: list[str] = []
    cells: list[str] = []
    try:
        if "xl/sharedStrings.xml" in zf.namelist():
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall(f".//{_XLSX_NS}si"):
                    s = _xlsx_si_plain_text(si)
                    if s:
                        ss_list.append(s)
            except ET.ParseError:
                logger.warning("document_extract_xlsx_shared_strings_parse_error file_name=%s", file_name)

        sheet_names = sorted(
            n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        for sn in sheet_names:
            try:
                sroot = ET.fromstring(zf.read(sn))
            except ET.ParseError:
                continue
            for c in sroot.iter(f"{_XLSX_NS}c"):
                t_attr = (c.get("t") or "").lower()
                is_el = c.find(f"{_XLSX_NS}is")
                v_el = c.find(f"{_XLSX_NS}v")
                if t_attr == "inlinestr" and is_el is not None:
                    txt = _xlsx_si_plain_text(is_el)
                    if txt:
                        cells.append(txt)
                elif t_attr == "s" and v_el is not None and (v_el.text or "").strip() != "":
                    try:
                        idx = int(v_el.text.strip())
                        if 0 <= idx < len(ss_list):
                            cells.append(ss_list[idx])
                    except ValueError:
                        pass
                elif v_el is not None and (v_el.text or "").strip() != "" and not t_attr:
                    raw_v = v_el.text.strip()
                    if raw_v and not raw_v.replace(".", "", 1).isdigit():
                        cells.append(raw_v)
    except Exception:
        logger.exception("document_extract_xlsx_failed file_name=%s", file_name)
        return "", "xlsx_error"
    finally:
        zf.close()

    merged = ss_list + cells
    out = _normalize_text("\n".join(dict.fromkeys(merged)))
    if not out:
        logger.info("document_extract_xlsx_empty file_name=%s", file_name)
    return out, "xlsx_text"


def extract_text_from_bytes(raw: bytes, file_name: str = "") -> Tuple[str, str]:
    """
    返回 (text, format_label)。
    format_label 用于审计：pdf_text / plain_text / unsupported / empty
    """
    if not raw:
        return "", "empty"

    ext = guess_extension(file_name)
    if ext == "csv":
        return _extract_text_from_csv(raw, file_name)

    if ext in {"txt", "md", "json", "xml", "log"}:
        text, enc_label = _decode_text_bytes_best_effort(raw, file_name, context="plain")
        if text:
            return _normalize_text(text), f"plain_text({enc_label})"
        logger.warning("document_extract_plain_decode_failed file_name=%s bytes=%s", file_name, len(raw))
        return "", "plain_decode_failed"

    if ext == "docx":
        return _extract_text_from_docx(raw, file_name)

    if ext == "xlsx":
        return _extract_text_from_xlsx(raw, file_name)

    if ext == "pdf":
        started = perf_counter()
        text, engine = _extract_pdf_text_layer(raw, file_name)
        if engine == "no_engine":
            # 与 _extract_pdf_text_layer 一致：pypdf 与 pymupdf 均不可用（多为解释器/venv 未装依赖，而非仅缺 pypdf）
            return "", "pdf_no_text_engine"
        if engine == "pdf_error":
            return "", "pdf_error"

        def _pdf_format_base() -> str:
            return "pdf_text" if engine == "pypdf" else "pdf_text_pymupdf"

        try:
            threshold = int(os.getenv("PDF_SPARSE_TEXT_THRESHOLD", "48").strip() or "48")
        except ValueError:
            threshold = 48
        threshold = max(8, min(threshold, 5000))

        ocr_enabled = os.getenv("PDF_FIRST_PAGE_OCR", "true").strip().lower() not in {"0", "false", "no", "off"}
        if len(text) < threshold and ocr_enabled and (engine == "pypdf" or not text):
            logger.info(
                "document_extract_pdf_sparse_text chars=%s threshold=%s file_name=%s engine=%s try_pdf_page_ocr",
                len(text),
                threshold,
                file_name,
                engine,
            )
            mineru_enabled = os.getenv("MINERU_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
            if mineru_enabled:
                mineru_text, mineru_fmt = _mineru_pdf_supplement(raw, file_name)
                if mineru_text:
                    merged = _normalize_text(text + "\n" + mineru_text)
                    base = _pdf_format_base()
                    logger.info(
                        "document_extract_pdf_done file_name=%s format=%s+%s chars=%s threshold=%s ocr_used=mineru elapsed_ms=%s",
                        file_name,
                        base,
                        mineru_fmt,
                        len(merged),
                        threshold,
                        int((perf_counter() - started) * 1000),
                    )
                    return merged, f"{base}+{mineru_fmt}"
                logger.warning(
                    "document_extract_pdf_mineru_empty_or_failed file_name=%s fmt=%s fallback=page_ocr",
                    file_name,
                    mineru_fmt,
                )
            extra, pages_done = _ocr_pdf_pages_supplement(raw, file_name)
            if extra:
                merged = _normalize_text(text + "\n" + extra)
                base = _pdf_format_base()
                suffix = "ocr_first_page" if pages_done <= 1 else f"ocr_pages_{pages_done}"
                logger.info(
                    "document_extract_pdf_done file_name=%s format=%s+%s chars=%s threshold=%s ocr_used=true elapsed_ms=%s",
                    file_name,
                    base,
                    suffix,
                    len(merged),
                    threshold,
                    int((perf_counter() - started) * 1000),
                )
                return merged, f"{base}+{suffix}"
        logger.info(
            "document_extract_pdf_done file_name=%s format=%s chars=%s threshold=%s ocr_used=false elapsed_ms=%s",
            file_name,
            _pdf_format_base(),
            len(text),
            threshold,
            int((perf_counter() - started) * 1000),
        )
        return text, _pdf_format_base()

    if ext in _IMAGE_OCR_EXTENSIONS:
        return extract_text_from_image(raw, file_name)

    return "", f"unsupported_ext:{ext or 'none'}"


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def mapping_search_snippet(document_text: str, max_len: int = 80) -> str:
    """
    从解析正文中取一小段用于 ERP 主数据模糊查询（真实 ERP 上线后仍可用）。
    """
    t = (document_text or "").strip()
    if not t:
        return ""
    line = t.split("\n", 1)[0].strip()
    return line[:max_len]


def truncate_for_api(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def classify_doc_type_from_name(file_name: str) -> str | None:
    """根据文件名猜测 PO / GR / INV（启发式，可后续用模型替代）。"""
    n = (file_name or "").lower()
    inv_keys = (
        "invoice",
        "inv_",
        "_inv",
        "-inv-",
        ".inv.",
        "发票",
        "fp",
        "fapiao",
        "billing",
        "credit-note",
        "debit-note",
        "vat_inv",
        "vat-inv",
        "comm_inv",
        "commercial-inv",
    )
    if any(k in n for k in inv_keys):
        return "INV"
    gr_keys = (
        "grn",
        "goods_receipt",
        "goods-receipt",
        "receipt",
        "收货",
        "入库单",
        "_gr",
        "gr_",
        "migo",
        "receiving",
    )
    if any(k in n for k in gr_keys):
        return "GR"
    po_keys = (
        "po_",
        "_po",
        "-po-",
        "purchase",
        "采购",
        "订单",
        "pur-ord",
        "purord",
        "purchaseorder",
    )
    if any(k in n for k in po_keys):
        return "PO"
    if re.search(r"(?:^|[^a-z0-9])po[a-z0-9]{4,}", n):
        return "PO"
    return None


def _document_front(text: str, max_chars: int = 1600) -> str:
    return (text or "").strip().lower()[:max_chars]


def _has_invoice_header_signal(text: str) -> bool:
    front = _document_front(text, 1000)
    if not front:
        return False
    invoice_title = re.search(
        r"(?m)^\s*(?:tax\s+invoice|vat\s+invoice|commercial\s+invoice|proforma\s+invoice)\b",
        front,
    )
    if invoice_title:
        return True
    return bool(
        re.search(r"\binvoice\s*(?:no\.?|number|#)\b", front)
        and re.search(r"\b(?:invoice\s+date|date\s+of\s+invoice|bill\s+to|ship\s+to)\b", front)
    )


def _has_goods_receipt_header_signal(text: str) -> bool:
    front = _document_front(text, 1000)
    if not front:
        return False
    return bool(
        re.search(r"(?m)^\s*(?:goods\s+receipt|goods\s+receipt\s+note|stock\s+receipt|warehouse\s+receipt)\b", front)
        or "\u6536\u8d27\u5355" in front
        or "\u5165\u5e93\u51ed\u8bc1" in front
    )


def _has_purchase_order_header_signal(text: str) -> bool:
    front = _document_front(text)
    if not front:
        return False
    if re.search(r"\bpurchase\s+order\b", front) or "\u91c7\u8d2d\u8ba2\u5355" in front:
        return True
    has_order_no = bool(
        re.search(r"\border\s+no\.?\b|\border\s+number\b|\bpo\s*(?:no\.?|number)\b", front)
        or "\u5ba2\u6237\u91c7\u8d2d\u5355\u53f7" in front
        or "\u91c7\u8d2d\u8ba2\u5355\u53f7" in front
        or "\u8ba2\u5355\u53f7" in front
    )
    has_party = bool(
        re.search(r"\b(?:buyer|supplier|vendor|customer)\b", front)
        or "\u9700\u65b9" in front
        or "\u4f9b\u65b9" in front
        or "\u4f9b\u5e94\u5546" in front
        or "\u5ba2\u6237" in front
    )
    has_line_signal = bool(
        re.search(r"\b(?:material|material\s+code|qty|quantity|uom|unit\s+price|total\s+price|delivery\s+date)\b", front)
        or "\u7269\u6599" in front
        or "\u6570\u91cf" in front
        or "\u4ea4\u8d27" in front
    )
    return has_order_no and has_party and has_line_signal


def classify_doc_type_from_text(text: str) -> str | None:
    t = (text or "").lower()
    if _has_invoice_header_signal(t):
        return "INV"
    if _has_goods_receipt_header_signal(t):
        return "GR"
    if _has_purchase_order_header_signal(t):
        return "PO"

    inv_phrases = (
        "tax invoice",
        "vat invoice",
        "commercial invoice",
        "proforma invoice",
        "商业发票",
        "invoice no",
        "invoice number",
        "发票号码",
        "debit note",
        "credit note",
        "开票信息",
    )
    if any(p in t for p in inv_phrases):
        return "INV"
    gr_phrases = (
        "goods receipt",
        "goods receipt note",
        "收货单",
        "material document",
        "stock receipt",
        "warehouse receipt",
        "入库凭证",
    )
    if any(p in t for p in gr_phrases):
        return "GR"
    po_phrases = (
        "purchase order",
        "采购订单",
        "po number",
        "purchase order number",
        "supplier po",
        "order lines",
    )
    if any(p in t for p in po_phrases):
        return "PO"
    return None


def heuristic_vendor_code(text: str) -> dict[str, str]:
    """
    从正文中猜测 vendor_code（仅高置信模式：如 V001、显式「供应商编号:」）。
    """
    out: dict[str, str] = {}
    if not text:
        return out
    m = re.search(r"\b(V\d{2,6})\b", text, re.IGNORECASE)
    if m:
        out["vendor_code"] = m.group(1).upper()
        return out
    m2 = re.search(
        r"(?:供应商编号|供应商编码|供方代码|供应商代码|vendor\s*code|vendor_code)\s*[:：]\s*([A-Za-z0-9\-]{2,24})",
        text,
        re.IGNORECASE,
    )
    if m2:
        out["vendor_code"] = m2.group(1).strip().upper()
    return out


def heuristic_fill_fields(text: str) -> dict[str, str]:
    """
    从正文中猜测 doc_date / currency；vendor 见 heuristic_vendor_code。
    """
    out: dict[str, str] = {}
    if not text:
        return out

    # 优先带标签的单据/订单日期，避免误用文中首个日期（如行交货日、旧记录）。
    labeled_iso = re.search(
        r"(?:订单日期|制单日期|签订日期|单据日期|采购日期|订购日期)\s*[:：]\s*(20\d{2}-\d{2}-\d{2})",
        text,
    )
    if labeled_iso:
        out["doc_date"] = labeled_iso.group(1)
    if not out.get("doc_date"):
        labeled_slash = re.search(
            r"(?:订单日期|制单日期|签订日期|单据日期|采购日期|订购日期)\s*[:：]\s*"
            r"(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})",
            text,
        )
        if labeled_slash:
            y, mo, d = labeled_slash.group(1), int(labeled_slash.group(2)), int(labeled_slash.group(3))
            out["doc_date"] = f"{y}-{mo:02d}-{d:02d}"
    if not out.get("doc_date"):
        labeled_cn = re.search(
            r"(?:订单日期|制单日期|签订日期|单据日期|采购日期|订购日期)\s*[:：]\s*"
            r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
            text,
        )
        if labeled_cn:
            y, mo, d = labeled_cn.group(1), int(labeled_cn.group(2)), int(labeled_cn.group(3))
            out["doc_date"] = f"{y}-{mo:02d}-{d:02d}"
    # 无标签时再取首个 ISO 日期
    if not out.get("doc_date"):
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
        if m:
            out["doc_date"] = m.group(1)
    if not out.get("doc_date"):
        m_cn = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if m_cn:
            y, mo, d = m_cn.group(1), int(m_cn.group(2)), int(m_cn.group(3))
            out["doc_date"] = f"{y}-{mo:02d}-{d:02d}"

    for cur in ("CNY", "USD", "EUR", "HKD", "JPY"):
        if re.search(rf"\b{cur}\b", text):
            out["currency"] = cur
            break
    if not out.get("currency"):
        if re.search(r"人民币(?:元|整)?|（人民币）|\bRMB\b", text, re.IGNORECASE):
            out["currency"] = "CNY"
        elif re.search(r"美\s*元|美金", text):
            out["currency"] = "USD"
        elif re.search(r"欧\s*元", text):
            out["currency"] = "EUR"
        elif re.search(r"港\s*币", text):
            out["currency"] = "HKD"
        elif re.search(r"日\s*元", text):
            out["currency"] = "JPY"

    return out

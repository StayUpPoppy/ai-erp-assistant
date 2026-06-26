from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from io import BytesIO
from time import perf_counter
from typing import Any, Optional

logger = logging.getLogger("ai_erp_api")


@dataclass(frozen=True)
class TextBlock:
    page_number: int
    text: str
    bbox: tuple[float, float, float, float]
    source: str
    confidence: Optional[float] = None


@dataclass(frozen=True)
class TableBlock:
    page_number: int
    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[str, ...], ...]


@dataclass
class PageParseResult:
    page_number: int
    source: str
    text: str
    blocks: list[TextBlock] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    native_char_count: int = 0
    ocr_confidence: Optional[float] = None
    low_quality: bool = False
    elapsed_ms: int = 0


@dataclass
class DocumentParseResult:
    text: str = ""
    format_label: str = "empty"
    route: str = "empty"
    pages: list[PageParseResult] = field(default_factory=list)
    quality_score: float = 0.0
    fallback_reason: str = ""
    timings_ms: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float((os.getenv(name) or str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _normalize_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _usable_char_ratio(text: str) -> float:
    compact = [char for char in (text or "") if not char.isspace()]
    if not compact:
        return 0.0
    good = sum(
        1
        for char in compact
        if char.isalnum()
        or "\u3400" <= char <= "\u9fff"
        or char in "-_/.:,;()[]{}%+*=#@&'\"￥¥€$，。；：（）【】《》、"
    )
    return good / len(compact)


def _bbox(value: Any) -> tuple[float, float, float, float]:
    try:
        values = list(value)
        if len(values) >= 4:
            return tuple(float(values[index]) for index in range(4))  # type: ignore[return-value]
    except (TypeError, ValueError):
        pass
    return (0.0, 0.0, 0.0, 0.0)


def _inside_bbox(x: float, y: float, bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _escape_table_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("|", "\\|")).strip()


def _table_markdown(rows: tuple[tuple[str, ...], ...]) -> str:
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    if width <= 0:
        return ""
    normalized = [tuple(list(row) + [""] * (width - len(row))) for row in rows]
    header = normalized[0]
    body = normalized[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _extract_tables(page: Any, page_number: int) -> list[TableBlock]:
    tables: list[TableBlock] = []
    try:
        finder = page.find_tables()
    except Exception:
        logger.debug("pdf_table_detection_failed page=%s", page_number, exc_info=True)
        return tables
    for table in getattr(finder, "tables", []) or []:
        try:
            extracted = table.extract() or []
        except Exception:
            logger.debug("pdf_table_extract_failed page=%s", page_number, exc_info=True)
            continue
        rows: list[tuple[str, ...]] = []
        for raw_row in extracted:
            row = tuple(_escape_table_cell(cell) for cell in (raw_row or []))
            if any(row):
                rows.append(row)
        if rows:
            tables.append(TableBlock(page_number=page_number, bbox=_bbox(getattr(table, "bbox", ())), rows=tuple(rows)))
    return tables


def _native_page(page: Any, page_number: int, tables: list[TableBlock]) -> tuple[str, list[TextBlock]]:
    try:
        words = list(page.get_text("words", sort=True) or [])
    except Exception:
        words = []
    grouped: dict[tuple[int, int], list[Any]] = {}
    for word in words:
        if len(word) < 8:
            continue
        text = str(word[4] or "").strip()
        if not text:
            continue
        center_x = (float(word[0]) + float(word[2])) / 2
        center_y = (float(word[1]) + float(word[3])) / 2
        if any(_inside_bbox(center_x, center_y, table.bbox) for table in tables):
            continue
        grouped.setdefault((int(word[5]), int(word[6])), []).append(word)

    components: list[tuple[float, str]] = []
    blocks: list[TextBlock] = []
    for line_words in grouped.values():
        line_words.sort(key=lambda item: float(item[0]))
        text = " ".join(str(item[4]).strip() for item in line_words if str(item[4]).strip())
        if not text:
            continue
        bbox = (
            min(float(item[0]) for item in line_words),
            min(float(item[1]) for item in line_words),
            max(float(item[2]) for item in line_words),
            max(float(item[3]) for item in line_words),
        )
        blocks.append(TextBlock(page_number=page_number, text=text, bbox=bbox, source="native"))
        components.append((bbox[1], text))

    for index, table in enumerate(tables, start=1):
        markdown = _table_markdown(table.rows)
        if markdown:
            components.append((table.bbox[1], f"[TABLE {index}]\n{markdown}"))
    components.sort(key=lambda item: item[0])
    text = _normalize_text("\n".join(component[1] for component in components))
    if not text:
        try:
            text = _normalize_text(page.get_text("text", sort=True) or "")
        except Exception:
            text = ""
    return text, blocks


def _pixmap_png(pix: Any) -> bytes:
    try:
        return pix.tobytes("png")
    except Exception:
        from PIL import Image

        target = BytesIO()
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(target, format="PNG")
        return target.getvalue()


def _ocr_page(page: Any, page_number: int, file_name: str, dpi: int) -> tuple[str, list[TextBlock], Optional[float], str]:
    from app.ocr_engine import ocr_image_bytes_detailed

    import fitz

    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    detailed = ocr_image_bytes_detailed(
        _pixmap_png(pix),
        f"{file_name}#pdf_p{page_number}.png",
        engine_override="rapid",
    )
    blocks: list[TextBlock] = []
    for block in detailed.blocks:
        xs = [point[0] for point in block.box]
        ys = [point[1] for point in block.box]
        bbox = (
            min(xs, default=0.0) / scale,
            min(ys, default=0.0) / scale,
            max(xs, default=0.0) / scale,
            max(ys, default=0.0) / scale,
        )
        blocks.append(
            TextBlock(
                page_number=page_number,
                text=block.text,
                bbox=bbox,
                source="rapidocr",
                confidence=block.confidence,
            )
        )
    return _normalize_text(detailed.text), blocks, detailed.confidence, detailed.format_label


def parse_pdf_local(raw: bytes, file_name: str = "document.pdf") -> DocumentParseResult:
    started = perf_counter()
    if not raw:
        return DocumentParseResult()
    try:
        import fitz
    except ImportError:
        return DocumentParseResult(format_label="pdf_no_text_engine", route="error", warnings=["pymupdf_not_installed"])

    native_min_chars = _env_int("PDF_NATIVE_MIN_CHARS", 48, 8, 5000)
    min_confidence = _env_float("RAPIDOCR_MIN_CONFIDENCE", 0.78, 0.0, 1.0)
    dpi = _env_int("PDF_RENDER_DPI", 250, 96, 400)
    max_ocr_pages = _env_int("PDF_OCR_MAX_PAGES", 10, 1, 20)
    pages: list[PageParseResult] = []
    warnings: list[str] = []
    ocr_pages = 0
    doc = None
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        for index in range(doc.page_count):
            page_started = perf_counter()
            page_number = index + 1
            page = doc.load_page(index)
            tables = _extract_tables(page, page_number)
            try:
                raw_native_text = _normalize_text(page.get_text("text", sort=True) or "")
            except Exception:
                raw_native_text = ""
            native_chars = _non_space_chars(raw_native_text)
            try:
                has_page_images = bool(page.get_images(full=True))
            except Exception:
                has_page_images = False
            native_usable = (
                _usable_char_ratio(raw_native_text) >= 0.85
                and (
                    native_chars >= native_min_chars
                    or (native_chars >= 8 and not has_page_images)
                )
            )
            if native_usable:
                text, blocks = _native_page(page, page_number, tables)
                result = PageParseResult(
                    page_number=page_number,
                    source="native",
                    text=text,
                    blocks=blocks,
                    tables=tables,
                    native_char_count=native_chars,
                )
            elif ocr_pages < max_ocr_pages:
                ocr_pages += 1
                text, blocks, confidence, ocr_format = _ocr_page(page, page_number, file_name, dpi)
                low_quality = _non_space_chars(text) < native_min_chars or confidence is None or confidence < min_confidence
                result = PageParseResult(
                    page_number=page_number,
                    source="rapidocr" if "rapid" in ocr_format else "ocr_fallback",
                    text=text,
                    blocks=blocks,
                    native_char_count=native_chars,
                    ocr_confidence=confidence,
                    low_quality=low_quality,
                )
            else:
                warnings.append(f"page_{page_number}_ocr_skipped_max_pages")
                result = PageParseResult(
                    page_number=page_number,
                    source="unparsed",
                    text=raw_native_text,
                    native_char_count=native_chars,
                    low_quality=True,
                )
            result.elapsed_ms = int((perf_counter() - page_started) * 1000)
            pages.append(result)
    except Exception as exc:
        logger.exception("pdf_local_parse_failed file_name=%s err=%s", file_name, exc)
        return DocumentParseResult(
            format_label="pdf_error",
            route="error",
            timings_ms={"total": int((perf_counter() - started) * 1000)},
            warnings=[f"pdf_error:{type(exc).__name__}"],
        )
    finally:
        if doc is not None:
            doc.close()

    sources = {page.source for page in pages}
    if sources == {"native"}:
        route = "native"
        format_label = "pdf_native_pymupdf"
    elif sources <= {"rapidocr", "ocr_fallback"}:
        route = "rapidocr"
        format_label = f"pdf_rapidocr_{dpi}dpi"
    else:
        route = "hybrid"
        format_label = f"pdf_hybrid_pymupdf_rapidocr_{dpi}dpi"

    page_chunks = [f"[PAGE {page.page_number}]\n{page.text}" for page in pages if page.text]
    text = _normalize_text("\n\n".join(page_chunks))
    page_scores = [
        1.0 if page.source == "native" else (page.ocr_confidence if page.ocr_confidence is not None else 0.0)
        for page in pages
    ]
    quality = sum(page_scores) / len(page_scores) if page_scores else 0.0
    low_pages = [str(page.page_number) for page in pages if page.low_quality]
    fallback_reason = f"low_quality_pages:{','.join(low_pages)}" if low_pages else ""
    result = DocumentParseResult(
        text=text,
        format_label=format_label,
        route=route,
        pages=pages,
        quality_score=quality,
        fallback_reason=fallback_reason,
        timings_ms={"total": int((perf_counter() - started) * 1000)},
        warnings=warnings,
    )
    logger.info(
        "pdf_local_parse_done file_name=%s route=%s pages=%s native_pages=%s ocr_pages=%s chars=%s quality=%.3f elapsed_ms=%s",
        file_name,
        route,
        len(pages),
        sum(1 for page in pages if page.source == "native"),
        ocr_pages,
        len(text),
        quality,
        result.timings_ms["total"],
    )
    return result


_ORDER_SIGNAL_RE = re.compile(
    r"(?:purchase\s+order|order\s*(?:no\.?|number|#)|采购订单|订单(?:编号|号码|号))",
    re.IGNORECASE,
)


def _has_usable_detail_rows(result: DocumentParseResult) -> bool:
    for page in result.pages:
        for table in page.tables:
            for row in table.rows[1:]:
                populated = [cell for cell in row if cell.strip()]
                if len(populated) >= 3 and any(re.search(r"\d", cell) for cell in populated):
                    return True
    for line in result.text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|") if cell.strip() and cell.strip() != "---"]
        if len(cells) >= 3 and any(re.search(r"\d", cell) for cell in cells):
            return True
    return False


def local_quality_reason(result: DocumentParseResult) -> str:
    min_chars = _env_int("PDF_NATIVE_MIN_CHARS", 48, 8, 5000)
    if _non_space_chars(result.text) < min_chars:
        return "document_text_too_short"
    low_pages = [str(page.page_number) for page in result.pages if page.low_quality]
    if low_pages:
        return f"low_quality_pages:{','.join(low_pages)}"
    if _ORDER_SIGNAL_RE.search(result.text) and not _has_usable_detail_rows(result):
        return "purchase_order_without_usable_detail_rows"
    return ""


def parse_pdf_document(raw: bytes, file_name: str = "document.pdf") -> DocumentParseResult:
    """Run the local PDF parse path only.

    Cloud fallback has been removed from the production workflow.  We still
    calculate ``fallback_reason`` as a local quality hint for preview issues
    and diagnostics, but no external document parser request is made here.
    """
    result = parse_pdf_local(raw, file_name)
    reason = local_quality_reason(result)
    result.fallback_reason = reason
    if reason:
        logger.info(
            "pdf_local_parse_quality_hint file_name=%s reason=%s route=%s chars=%s quality=%.3f",
            file_name,
            reason,
            result.route,
            len(result.text),
            result.quality_score,
        )
    return result

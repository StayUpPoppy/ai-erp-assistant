"""
按单据类型（PO / GR / INV）的最小结构化字段：正文启发式抽取。

必填键名单一事实源：`erp_assistant_shared.contract.required_field_keys`（包 `backend/packages/shared`）。

租户级扩展见 `app.extraction_profile`（JSON 档案：额外必填 + 正则槽位）。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Dict, List, Optional

from erp_assistant_shared.contract import required_field_keys

if TYPE_CHECKING:
    from app.extraction_profile import ExtractionProfile

__all__ = ["required_field_keys", "extract_structured_fields", "extract_po_cn_layout_entities"]


def _first_match(text: str, pattern: str, flags: int = 0) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _norm_any_date(s: str) -> str:
    s = (s or "").strip()
    cn = re.match(r"^(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", s)
    if cn:
        return f"{cn.group(1)}-{int(cn.group(2)):02d}-{int(cn.group(3)):02d}"
    if re.match(r"^20\d{2}[/\-]\d{1,2}[/\-]\d{1,2}$", s):
        return _norm_slash_date(s)
    return s


def _norm_slash_date(s: str) -> str:
    s = (s or "").strip().replace("－", "-")
    parts = re.split(r"[/\-]", s)
    if len(parts) != 3:
        return s
    y, mo, d = parts[0], int(parts[1]), int(parts[2])
    if len(y) != 4 or not y.isdigit():
        return s
    return f"{y}-{mo:02d}-{d:02d}"


def _split_sap_drawing_qty(token: str) -> tuple[str, str]:
    """
    SAP SRM PDFs often concatenate drawing number and quantity, e.g. ``T04037172``
    means drawing ``T04037`` with quantity ``172``. Some GB/T disk spring drawings
    use one more digit, e.g. ``T2226444`` means drawing ``T222644`` with quantity
    ``4``. Keep this local to the SAP fallback parser so generic PO rules stay
    conservative.
    """
    token = (token or "").strip()
    if not re.match(r"^[A-Z]\d{5,}$", token):
        return token, ""
    if len(token) <= 6:
        return token, ""

    if len(token) >= 8 and token.startswith("T22264"):
        return token[:7], token[7:]

    if len(token) >= 8:
        qty_len6 = token[6:]
        qty_len7 = token[7:]
        try:
            if qty_len7 and int(qty_len6) > 999 and int(qty_len7) <= 999:
                return token[:7], qty_len7
        except ValueError:
            pass
        return token[:6], qty_len6

    return token[:6], token[6:]


def _parse_sap_srm_metric_block(block_lines: List[str]) -> Optional[Dict[str, str]]:
    block = " ".join(line.strip() for line in block_lines if line.strip())
    m = re.match(r"^([A-Z]\d{5,})(.*)$", block)
    if not m:
        return None
    drawing_token, rest = m.groups()
    drawing, qty = _split_sap_drawing_qty(drawing_token)
    rest = rest.strip()

    if not qty:
        q = re.match(r"^(\d+(?:\.\d+)?)\s+(.*)$", rest)
        if not q:
            return None
        qty, rest = q.groups()
        rest = rest.strip()

    p = re.match(r"^(\d+(?:\.\d+)?)(.*)$", rest)
    if not p:
        return None
    price, tail = p.groups()
    unit = ""
    amount = ""
    unit_amount_matches = re.findall(r"\b([A-Z])\s*(\d+(?:\.\d+)?)\b", tail.strip())
    if unit_amount_matches:
        unit, amount = unit_amount_matches[-1]

    return {
        "drawing_number": drawing,
        "quantity": qty,
        "unit_price_excl_tax": price,
        "unit": unit,
        "line_amount_excl_tax": amount,
    }


def _extract_sap_srm_po_line_items(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    row_starts: List[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^([1-9]\d{1,2})(\d{8})\s*(\D.+)$", line)
        if not m:
            continue
        try:
            line_no_int = int(m.group(1))
        except ValueError:
            continue
        if line_no_int % 10 != 0:
            continue
        row_starts.append((i, m))

    for pos, (line_index, m) in enumerate(row_starts):
        line_no, material, desc = m.groups()
        next_index = row_starts[pos + 1][0] if pos + 1 < len(row_starts) else len(lines)
        block_lines = lines[line_index + 1 : next_index]
        metric = _parse_sap_srm_metric_block(block_lines)
        if not metric:
            continue
        rows.append(
            {
                "line_no": line_no,
                "inventory_code": material,
                "name": desc.strip(),
                **metric,
            }
        )
    return rows


def _clean_english_po_cell(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip(" \t\r\n|:："))


def _norm_noisy_slash_date(value: str) -> str:
    raw = (value or "").strip()
    m = re.search(r"(20\d{2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{1,2})", raw)
    if not m:
        return ""
    year, month_raw, day_raw = m.groups()
    month = int(month_raw)
    day = int(day_raw)
    if month > 12 and len(month_raw) == 2:
        first_digit = int(month_raw[0])
        if 1 <= first_digit <= 12:
            month = first_digit
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year}-{month:02d}-{day:02d}"


def _first_english_po_date(text: str, label_pattern: str) -> str:
    m = re.search(label_pattern, text, re.IGNORECASE)
    if not m:
        return ""
    window = (text or "")[m.end() : m.end() + 180]
    for raw in re.findall(r"20\d{2}\s*[/\-]\s*\d{1,2}\s*[/\-]\s*\d{1,2}", window):
        normalized = _norm_noisy_slash_date(raw)
        if normalized:
            return normalized
    return ""


def _first_number(value: str) -> str:
    m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", value or "")
    return m.group(0).replace(",", "") if m else ""


def _numbers_in(value: str) -> List[str]:
    return [match.replace(",", "") for match in re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", value or "")]


def _format_decimal(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


_CJK_RE = re.compile(r"[一-龥]")
_CHINESE_COMPANY_RE = re.compile(
    r"([一-龥A-Za-z0-9（）()·\-]{2,80}?(?:有限责任公司|股份有限公司|有限公司|公司|工厂|厂))"
)
_CHINESE_ADDRESS_TOKENS = ("省", "市", "区", "县", "镇", "乡", "村", "路", "道", "街", "号", "段", "园", "楼")
_BUYER_LABEL_RE = re.compile(r"(?:客户名称|客户|需方|买方|采购方|采购商|收货方|购买方)\s*[:：]?\s*(.+)")
_SUPPLIER_LABEL_RE = re.compile(r"(?:供应商|供方|卖方|供货方|生产商|厂商)\s*[:：]")
_DELIVERY_ADDR_LABEL_RE = re.compile(r"(?:收货地址|送货地址|交货地址|交货地点|到货地址|Delivery\s*Address)\s*[:：]?\s*(.*)", re.IGNORECASE)


def _has_chinese(value: str) -> bool:
    return bool(_CJK_RE.search(value or ""))


def _clean_cn_candidate(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip(" \t\r\n|:：,，;；"))
    text = re.split(
        r"\s+(?:Order\s+No\.?|Issue\s+Date|Delivery\s+Terms|Payment\s+Terms|Tel|Fax)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" \t\r\n|:：,，;；")
    return text


def _looks_like_chinese_company(value: str) -> bool:
    text = _clean_cn_candidate(value)
    return bool(_CHINESE_COMPANY_RE.search(text))


def _extract_company_from_line(line: str) -> str:
    text = _clean_cn_candidate(line)
    labeled = _BUYER_LABEL_RE.search(text)
    if labeled:
        text = labeled.group(1)
    m = _CHINESE_COMPANY_RE.search(text)
    return _clean_cn_candidate(m.group(1)) if m else ""


def _looks_like_chinese_address(value: str) -> bool:
    text = _clean_cn_candidate(value)
    if not _has_chinese(text):
        return False
    token_count = sum(1 for token in _CHINESE_ADDRESS_TOKENS if token in text)
    return token_count >= 2 and len(text) >= 6


def _extract_address_from_line(line: str) -> str:
    text = _clean_cn_candidate(line)
    labeled = _DELIVERY_ADDR_LABEL_RE.search(text)
    if labeled:
        text = labeled.group(1)
    if not _looks_like_chinese_address(text):
        return ""
    return _clean_cn_candidate(text)


def _extract_preferred_chinese_po_header_fields(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    raw_lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in raw_lines if line]

    company_candidates: list[tuple[int, int, str]] = []
    address_candidates: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        clean = _clean_cn_candidate(line)
        if not clean or not _has_chinese(clean):
            continue

        if _SUPPLIER_LABEL_RE.search(clean):
            supplier_company = _extract_company_from_line(clean)
            if supplier_company and _BUYER_LABEL_RE.search(clean):
                company_candidates.append((idx, 80, supplier_company))
        else:
            company = _extract_company_from_line(clean)
            if company:
                score = 50
                if _BUYER_LABEL_RE.search(clean):
                    score += 80
                if idx <= 8:
                    score += 10
                company_candidates.append((idx, score, company))

        addr_match = _DELIVERY_ADDR_LABEL_RE.search(clean)
        if addr_match:
            same_line = _extract_address_from_line(addr_match.group(1))
            if same_line:
                address_candidates.append((idx, 120, same_line))
            for nxt in lines[idx + 1 : idx + 4]:
                next_addr = _extract_address_from_line(nxt)
                if next_addr:
                    address_candidates.append((idx, 110, next_addr))
                    break
        else:
            addr = _extract_address_from_line(clean)
            if addr:
                score = 40 + (10 if idx <= 12 else 0)
                address_candidates.append((idx, score, addr))

    if company_candidates:
        company_candidates.sort(key=lambda item: (-item[1], item[0]))
        out["customerName"] = company_candidates[0][2]
    if address_candidates:
        address_candidates.sort(key=lambda item: (-item[1], item[0]))
        out["deliveryAddr"] = address_candidates[0][2]
    return out


def _extract_global_set_pipe_po_entities(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    t = text or ""
    low = t.lower()
    if not ("global" in low and ("order no" in low or re.search(r"\bpo[a-z0-9]{6,}\b", low))):
        return out

    customer = _first_match(
        t,
        r"(?im)^\s*(global[\s\-]*set[^\n\r]{0,180}?(?:co\.?\s*,?\s*ltd\.?|ltd\.?))",
        re.IGNORECASE,
    )
    if customer:
        customer = re.split(r"\s+(?:address|tel|fax|order\s+no)\b", customer, maxsplit=1, flags=re.IGNORECASE)[0]
        out["customerName"] = _clean_english_po_cell(customer)

    order_no = _first_match(
        t,
        r"(?:order\s*no\.?|po\s*(?:no\.?|number))\s*[:#.\s]*([A-Z0-9][A-Z0-9\-_]{5,50})",
        re.IGNORECASE,
    )
    if not order_no:
        order_no = _first_match(t, r"\b(PO[A-Z0-9]{6,50})\b", re.IGNORECASE)
    if order_no:
        out["customerPoNo"] = order_no.upper()
        out.setdefault("order_no", order_no.upper())

    doc_date = _first_english_po_date(t, r"(?:issue\s*date|order\s*date|date)")
    if doc_date:
        out["doc_date"] = doc_date

    addr = _first_match(t, r"(?:address)\s*[:：]?\s*([^\n\r]{8,220})", re.IGNORECASE)
    if addr and re.search(r"\b(?:payment|delivery)\s+terms\b", addr, re.IGNORECASE) and not re.search(
        r"\b(?:province|city|road|highway|lane|town)\b", addr, re.IGNORECASE
    ):
        addr = ""
    if not addr:
        addr = _first_match(t, r"([^\n\r]{8,220}jiangsu\s+province[^\n\r]{0,80})", re.IGNORECASE)
    if addr:
        addr = re.split(
            r"\s+(?:tel|fax|order\s+no|issue\s+date|delivery\s+terms|payment\s+terms)\b",
            addr,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        out["deliveryAddr"] = _clean_english_po_cell(addr)

    if re.search(r"\b(?:cny|rmb)\b|¥|￥", t, re.IGNORECASE) or "jiangsu province" in low:
        out["currency"] = "CNY"

    rows: List[Dict[str, str]] = []
    for raw_line in t.replace("｜", "|").replace("¦", "|").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            cells = [_clean_english_po_cell(part) for part in line.split("|")]
            cells = [cell for cell in cells if cell]
        else:
            cells = [_clean_english_po_cell(part) for part in re.split(r"\s{2,}", line)]
        if len(cells) < 5 or not re.fullmatch(r"\d{1,3}", cells[0]):
            continue
        header_like = " ".join(cells).lower()
        if "qty" in header_like and ("item" in header_like or "description" in header_like):
            continue

        numeric_cells: List[str] = []
        for cell in cells[4:]:
            cleaned = re.sub(r"20\d{2}\s*[/\-]\s*\d{0,2}\s*[/\-]\s*\d{1,2}", " ", cell)
            numeric_cells.extend(_numbers_in(cleaned))

        delivery = ""
        for cell in reversed(cells):
            delivery = _norm_noisy_slash_date(cell)
            if delivery:
                break

        item: Dict[str, str] = {"line_no": cells[0], "inventory_code": cells[1]}
        if len(cells) > 2:
            item["name"] = cells[2]
        if len(cells) > 3:
            item["productSpec"] = cells[3]
        if numeric_cells:
            item["quantity"] = numeric_cells[0]
        tail_numbers = numeric_cells[1:]
        if len(tail_numbers) >= 2:
            item["unit_price_excl_tax"] = tail_numbers[-2]
            item["line_amount_excl_tax"] = tail_numbers[-1]
            try:
                qty = float(item["quantity"])
                amount = float(item["line_amount_excl_tax"])
                price = float(item["unit_price_excl_tax"])
                derived_price = amount / qty if qty else 0
                if qty and derived_price and abs(price * qty - amount) > 0.05:
                    item["unit_price_excl_tax"] = _format_decimal(derived_price)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        elif len(tail_numbers) == 1:
            item["unit_price_excl_tax"] = tail_numbers[0]
        if delivery:
            item["delivery_date"] = delivery
            out.setdefault("delivery_date", delivery)
        rows.append(item)

    if rows:
        default_delivery = out.get("delivery_date")
        if default_delivery:
            for row in rows:
                row.setdefault("delivery_date", default_delivery)
        out["line_items_json"] = json.dumps(rows, ensure_ascii=False)
    return out


def extract_po_cn_layout_entities(text: str) -> Dict[str, str]:
    """
    中文采购订单常见抬头 + 表格明细（支持多行）。

    与契约必填字段独立：产出 ``supplier_name`` / ``buyer_name`` / ``order_no`` /
    ``line_items_json``（JSON 数组字符串），供 ``GET /ingestions/{id}/document`` 展开为 ``line_items``。
    """
    out: Dict[str, str] = {}
    t = text or ""
    if not t.strip():
        return out

    english_po = _extract_global_set_pipe_po_entities(t)
    if english_po:
        out.update(english_po)

    sup = _first_match(t, r"供方\s*[:：]\s*([^\n\r]{2,120})")
    if not sup:
        sup = _first_match(t, r"供应商名称\s*([^\n\r]{2,120}?)(?:\s+采购商名称|\s*$)")
    if sup:
        out["supplier_name"] = sup.strip()
    buyer = _first_match(t, r"需方\s*[:：]\s*([^\n\r]{2,120})")
    if not buyer:
        buyer = _first_match(t, r"采购商名称\s*([^\n\r]{2,120})")
    if buyer:
        out["buyer_name"] = buyer.strip()
    ono = _first_match(t, r"(?:订单编号|订单号)\s*[:：]\s*([A-Za-z0-9]{6,40})")
    if not ono:
        ono = _first_match(t, r"订单号\s*([A-Za-z0-9]{6,40})")
    if ono:
        out["order_no"] = ono.strip()

    rows: List[Dict[str, str]] = []
    for m in re.finditer(
        r"(?m)^\s*(\d+)\s+"
        r"([A-Za-z0-9\-]{6,32})\s+"
        r"(.+?)\s+"
        r"(\d+)\s+"
        r"(件|个|台|套)\s+"
        r"([\d.]+)\s+"
        r"([\d.]+)\s+"
        r"([\d.]+)\s+"
        r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})",
        t,
    ):
        idx, inv, name, qty_s, unit, inc_s, excl_s, total_s, deliv = m.groups()
        rows.append(
            {
                "line_no": idx,
                "inventory_code": inv,
                "name": name.strip(),
                "quantity": qty_s,
                "unit": unit,
                "unit_price_incl_tax": inc_s,
                "unit_price_excl_tax": excl_s,
                "line_amount_incl_tax": total_s,
                "delivery_date": _norm_slash_date(deliv),
            },
        )
    if rows and "line_items_json" not in out:
        out["line_items_json"] = json.dumps(rows, ensure_ascii=False)
    elif "line_items_json" not in out:
        sap_rows = _extract_sap_srm_po_line_items(t)
        if sap_rows:
            out["line_items_json"] = json.dumps(sap_rows, ensure_ascii=False)
    out.update(_extract_preferred_chinese_po_header_fields(t))
    return out


def extract_structured_fields(
    text: str,
    doc_type: Optional[str],
    profile: Optional["ExtractionProfile"] = None,
) -> Dict[str, str]:
    """
    从正文抽取类型相关字段（不覆盖已有高置信 vendor_code 等，由调用方按空槽合并）。

    ``profile`` 非空时追加档案中的 ``extract_rules`` 命中结果。
    """
    out: Dict[str, str] = {}
    if not text:
        return out
    dt = (doc_type or "PO").strip().upper() or "PO"

    # 三类共用的物料号模式
    mat = _first_match(text, r"\b(M\d{3,12})\b", re.IGNORECASE) or _first_match(
        text,
        r"(?:物料(?:代码|编号|号)|料号|图号|material\s*code)\s*[:：]\s*([A-Za-z0-9\-\.]{2,32})",
        re.IGNORECASE,
    )
    if mat:
        out["material_code"] = mat.upper() if mat.upper().startswith("M") else mat

    wh = _first_match(
        text,
        r"(?:仓库|库房|收货仓|warehouse)\s*[:：#]?\s*([A-Z0-9][A-Z0-9\-]{1,15})",
        re.IGNORECASE,
    )
    if wh:
        out["warehouse_code"] = wh.upper()
    taxc = _first_match(
        text,
        r"(?:税码|税代码|tax\s*code)\s*[:：#]?\s*([A-Z0-9]{1,8})",
        re.IGNORECASE,
    )
    if taxc:
        out["tax_code"] = taxc.upper()

    if dt == "PO":
        customer = _first_match(
            text,
            r"(?:客户名称|客户名|客户|需方|采购方|买方|甲方|customer|buyer)\s*[:：]\s*([^\n\r]{2,120})",
            re.IGNORECASE,
        )
        if not customer:
            customer = _first_match(text, r"采购商名称\s*([^\n\r]{2,120})", re.IGNORECASE)
        if customer:
            out["customerName"] = customer

        order_no = _first_match(
            text,
            r"(?:客户采购单号|客户PO|客户订单号|采购订单号|订单编号|订单号|合同编号|Order\s*(?:No\.?|Number)|PO\s*(?:No\.?|Number)?)\s*[:：#.]?\s*([A-Za-z0-9\-_/]{4,50})",
            re.IGNORECASE,
        )
        if order_no:
            out["customerPoNo"] = order_no
            out.setdefault("order_no", order_no)

        delivery = _first_match(
            text,
            r"(?:交货日期|交期|约定交货日期|到货日期|delivery\s*date)\s*[:：]?\s*((?:20\d{2}[-/]\d{1,2}[-/]\d{1,2})|(?:20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?))",
            re.IGNORECASE,
        )
        if delivery:
            out["delivery_date"] = _norm_any_date(delivery)

        tax = _first_match(text, r"(?:税率|tax\s*rate)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%?", re.IGNORECASE)
        if tax:
            out["tax_rate"] = tax

        delivery_addr = _first_match(
            text,
            r"(?:收货地址|送货地址|交货地点|delivery\s*address)\s*[:：]\s*([^\n\r]{4,180})",
            re.IGNORECASE,
        )
        if delivery_addr:
            out["deliveryAddr"] = delivery_addr

        qty = _first_match(
            text,
            r"(?:数量|qty|quantity)\s*[:：]?\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ) or _first_match(text, r"\bQty\s*[:：]?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
        if not qty:
            qty = _first_match(
                text,
                r"(?:订购数量|采购数量)\s*[:：]?\s*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            )
        if not qty:
            qty = _first_match(
                text,
                r"(?:order|line)\s*qty\s*[:：.#\s]*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            )
        if not qty:
            # CSV 展开常见「… | Mxxx | 数量」尾列
            qty = _first_match(
                text,
                r"\bM\d{3,12}\b[^|\n]*\|\s*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            )
        if not qty:
            qty = _first_match(
                text,
                r"(?:合计数量|总数量|订购总数|数量合计)\s*[:：]\s*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            )
        if not qty:
            qty = _first_match(
                text,
                r"(?:数量|件数)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(?:件|台|套|个|支|条)?",
                re.IGNORECASE,
            )
        if qty:
            out["line_qty"] = qty

        unit_price_excl = _first_match(
            text,
            r"(?:不含税单价|未税单价|除税单价|price\s*without\s*tax)\s*[:：]?\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        )
        if unit_price_excl:
            out["unit_price_excl_tax"] = unit_price_excl
            out["price"] = unit_price_excl
        unit_price_incl = _first_match(
            text,
            r"(?:含税单价|价税合计单价|tax\s*price|price\s*with\s*tax)\s*[:：]?\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        )
        if unit_price_incl:
            out["unit_price_incl_tax"] = unit_price_incl
            out["taxPrice"] = unit_price_incl

    elif dt == "GR":
        # 先匹配带标签的整句，避免 loose 的「PO」吃到「PO Number」里的 NUMBER 或 Ref 行尾部的 PO。
        po = _first_match(
            text,
            r"(?:po\s*number|purchase\s*order\s*(?:number|no\.?))\s*[:#.\s]+([A-Z0-9\-]{3,24})",
            re.IGNORECASE,
        )
        if not po:
            po = _first_match(
                text,
                r"(?:reference|ref\.?)\s*(?:purchase\s*order|po)\s*[:#.\s]+([A-Z0-9\-]{3,24})",
                re.IGNORECASE,
            )
        if not po:
            po = _first_match(
                text,
                r"(?<![A-Za-z])PO(?!\s*number\b)\s*[#:\s.-]*([A-Z0-9]{3,20})",
                re.IGNORECASE,
            )
        if not po:
            po = _first_match(text, r"\b(PO\d{4,12})\b", re.IGNORECASE)
        if not po:
            po = _first_match(
                text,
                r"(?:采购订单|订单号|订单编号)\s*[:：#]\s*([A-Z0-9\-]{3,24})",
                re.IGNORECASE,
            )
        if po:
            out["po_no"] = po.upper()
        recv = _first_match(
            text,
            r"(?:收货数量|实收数量|received\s*qty|qty\s*received|received\s*quantity)\s*[:：]?\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ) or _first_match(text, r"(?:实收)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
        if not recv:
            recv = _first_match(text, r"(?:数量|qty)\s*[:：]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
        if recv:
            out["qty_received"] = recv

    elif dt == "INV":
        inv = _first_match(
            text,
            r"(?:invoice\s*(?:no\.?|number|#)|发票号码|发票编号|发票号|inv\.?\s*no)\s*[#:\s.:：]*([A-Z0-9\-]{4,36})",
            re.IGNORECASE,
        )
        if not inv:
            inv = _first_match(text, r"\b(INV(?:/\d{2,6}){2,6})\b", re.IGNORECASE)
        if not inv:
            inv = _first_match(text, r"\b(INV[-/]?\d{4,18})\b", re.IGNORECASE)
        if not inv:
            inv = _first_match(
                text,
                r"(?:bill\s*no\.?|billing\s*reference)\s*[#:\s.]*([A-Z0-9\-]{4,36})",
                re.IGNORECASE,
            )
        if inv:
            out["invoice_no"] = inv.upper()
        idate = _first_match(
            text,
            r"(?:invoice\s*date|date\s*of\s*invoice|开票日期|发票日期)\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})",
            re.IGNORECASE,
        )
        if idate:
            out["invoice_date"] = idate
        elif out.get("invoice_no"):
            # 仅有发票号时，用文中首个 ISO 日期兜底（常与 doc_date 相同）
            any_d = _first_match(text, r"(20\d{2}-\d{2}-\d{2})")
            if any_d:
                out["invoice_date"] = any_d

    if dt == "PO":
        cn_header = _extract_preferred_chinese_po_header_fields(text)
        if cn_header:
            out.update(cn_header)

    if profile is not None:
        from app.extraction_profile import apply_extract_rules

        out.update(apply_extract_rules(text, doc_type, profile))
    return out

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

    sup = _first_match(t, r"供方\s*[:：]\s*([^\n\r]{2,120})")
    if sup:
        out["supplier_name"] = sup.strip()
    buyer = _first_match(t, r"需方\s*[:：]\s*([^\n\r]{2,120})")
    if buyer:
        out["buyer_name"] = buyer.strip()
    ono = _first_match(t, r"(?:订单编号|订单号)\s*[:：]\s*([A-Za-z0-9]{6,40})")
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
    if rows:
        out["line_items_json"] = json.dumps(rows, ensure_ascii=False)
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
        if customer:
            out["customerName"] = customer

        order_no = _first_match(
            text,
            r"(?:客户采购单号|客户PO|客户订单号|采购订单号|订单编号|订单号|合同编号|PO\s*(?:No\.?|Number)?)\s*[:：#]?\s*([A-Za-z0-9\-_/]{4,50})",
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

    if profile is not None:
        from app.extraction_profile import apply_extract_rules

        out.update(apply_extract_rules(text, doc_type, profile))
    return out

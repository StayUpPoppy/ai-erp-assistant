"""
PO / GR / INV 扁平必填字段：与 Mock validate、extract 缺失、前端补全键一致。

单一事实源：API 通过本模块导出；前端 TS 常量须人工同步（见包内 README）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# 元组顺序即校验/展示顺序（稳定）
REQUIRED_FLAT_FIELDS_BY_DOC_TYPE: Dict[str, Tuple[str, ...]] = {
    "PO": ("vendor_code", "doc_date", "currency", "material_code", "line_qty"),
    "GR": ("vendor_code", "doc_date", "currency", "po_no", "material_code", "qty_received"),
    "INV": ("vendor_code", "doc_date", "currency", "invoice_no", "invoice_date"),
}


def required_field_keys(doc_type: Optional[str]) -> List[str]:
    dt = (doc_type or "PO").strip().upper() or "PO"
    row = REQUIRED_FLAT_FIELDS_BY_DOC_TYPE.get(dt)
    if row is None:
        row = REQUIRED_FLAT_FIELDS_BY_DOC_TYPE["PO"]
    return list(row)

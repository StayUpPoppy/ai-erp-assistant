from uuid import uuid4
from typing import Dict, List, Optional, Tuple

from app.structured_extract import required_field_keys


def validate_draft(
    doc_type: str,
    fields: Dict[str, str],
    required_keys: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    # 草稿创建前的最小字段校验（与 MockErpClient 对齐）。
    required = required_keys if required_keys is not None else required_field_keys(doc_type)
    missing = [k for k in required if not (fields.get(k) or "").strip()]
    return (len(missing) == 0, missing)


def create_draft(doc_type: str) -> Tuple[str, str]:
    # 生成 mock 草稿号与草稿链接，形态与未来 ERP 返回保持一致，
    # 这样前端和上层编排逻辑无需改动即可切换到真实 ERP。
    draft_no = f"{doc_type}-DRAFT-{uuid4().hex[:8].upper()}"
    draft_url = f"https://mock-erp.local/drafts/{draft_no}"
    return draft_no, draft_url

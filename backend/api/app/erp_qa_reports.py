"""
可配置「报表 / 只读查询」：按用户问题中的关键词匹配 JSON 配置，调用 ERP GET 取数，供 erp_qa 拼装回答。

不依赖大模型：意图由配置里的 substring 匹配；多配置同时命中时提示用户收紧说法。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import parse

logger = logging.getLogger("ai_erp_api")

_CACHE: Tuple[float, List[Dict[str, Any]]] | None = None


def invalidate_report_definitions_cache() -> None:
    global _CACHE
    _CACHE = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def reports_config_file() -> Optional[Path]:
    raw = os.getenv("ERP_QA_REPORTS_PATH", "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p if p.is_file() else None
    default = _repo_root() / "config" / "erp_qa_reports.json"
    return default if default.is_file() else None


def load_report_definitions() -> List[Dict[str, Any]]:
    """读取并校验报表定义；带 mtime 缓存。"""
    global _CACHE
    path = reports_config_file()
    if path is None:
        _CACHE = None
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if _CACHE is not None and _CACHE[0] == mtime:
        return _CACHE[1]
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("erp_qa_reports_load_failed path=%s err=%s", path, exc)
        _CACHE = (mtime, [])
        return []
    reports = data.get("reports")
    if not isinstance(reports, list):
        logger.warning("erp_qa_reports_invalid shape path=%s", path)
        _CACHE = (mtime, [])
        return []
    cleaned: List[Dict[str, Any]] = []
    for i, r in enumerate(reports):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if not rid:
            logger.warning("erp_qa_reports_skip_missing_id index=%s", i)
            continue
        http = r.get("http")
        if not isinstance(http, dict):
            continue
        p = str(http.get("path") or "").strip()
        if not p:
            continue
        method = str(http.get("method") or "GET").strip().upper()
        if method != "GET":
            logger.warning("erp_qa_reports_skip_non_get id=%s", rid)
            continue
        query = http.get("query")
        if query is not None and not isinstance(query, dict):
            continue
        resp = r.get("response") or {}
        if not isinstance(resp, dict):
            resp = {}
        r["http"] = {"method": "GET", "path": p, "query": dict(query) if isinstance(query, dict) else {}}
        r["response"] = {
            "datynk_envelope": bool(resp.get("datynk_envelope", True)),
            "records_path": str(resp.get("records_path") or "data.records").strip() or "data.records",
        }
        r["label"] = str(r.get("label") or rid).strip() or rid
        cleaned.append(r)
    _CACHE = (mtime, cleaned)
    logger.info("erp_qa_reports_loaded path=%s count=%s", path, len(cleaned))
    return cleaned


def erp_qa_reports_health_payload() -> Dict[str, Any]:
    n = len(load_report_definitions())
    return {"erp_qa_report_definitions_count": n}


def message_matches_report(message: str, match_cfg: Any) -> bool:
    if not isinstance(match_cfg, dict):
        return False
    any_ss = match_cfg.get("any_substrings") or []
    if not isinstance(any_ss, list) or not any_ss:
        return False
    if not any(isinstance(s, str) and s and s in message for s in any_ss):
        return False
    all_ss = match_cfg.get("all_substrings") or []
    if isinstance(all_ss, list):
        for s in all_ss:
            if isinstance(s, str) and s and s not in message:
                return False
    ex = match_cfg.get("exclude_substrings") or []
    if isinstance(ex, list):
        for s in ex:
            if isinstance(s, str) and s and s in message:
                return False
    return True


def matched_reports(definitions: List[Dict[str, Any]], message: str) -> List[Dict[str, Any]]:
    return [r for r in definitions if message_matches_report(message, r.get("match"))]


def resolve_query_templates(query: Dict[str, Any], org_id: str, keyword: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    o = org_id or ""
    kw = keyword or ""
    for k, v in query.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if v is None:
            continue
        s = str(v)
        s = s.replace("{{org_id}}", o)
        s = s.replace("{{keyword}}", kw)
        s = s.replace("{{keyword_uri}}", parse.quote(kw, safe=""))
        out[k.strip()] = s
    skip_empty = os.getenv("ERP_QA_REPORTS_SKIP_EMPTY_QUERY", "true").strip().lower() in ("1", "true", "yes", "on")
    if skip_empty:
        out = {k: v for k, v in out.items() if str(v).strip() != ""}
    return out


def format_report_disambiguation(labels: List[str]) -> str:
    body = "\n".join(f"- {x}" for x in labels if x)
    return (
        "您的问题可能对应**多种已配置的报表/查询**，请**再具体一点**（例如加上报表名称里的特征词），以便只命中一种：\n\n"
        f"{body}\n\n"
        "管理员可在 `backend/config/erp_qa_reports.json`（或环境变量 `ERP_QA_REPORTS_PATH`）里调整各报表的 `match.any_substrings` / `all_substrings`，减少交叉命中。"
    )


def format_report_section(label: str, rows: List[Dict[str, str]], max_rows: int = 20, max_keys: int = 10) -> List[str]:
    lines = [f"**{label}（配置化报表查询）**", ""]
    if not rows:
        lines.append("- （无数据或路径/字段与响应不一致）")
        lines.append("")
        return lines
    for row in rows[:max_rows]:
        keys = sorted(row.keys())[:max_keys]
        parts = [f"{k}={row.get(k, '')}" for k in keys]
        lines.append("- " + " | ".join(parts))
    if len(rows) > max_rows:
        lines.append(f"- … 共 {len(rows)} 条，此处仅展示前 {max_rows} 条")
    lines.append("")
    return lines

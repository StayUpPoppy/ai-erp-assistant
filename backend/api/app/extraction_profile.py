"""
按租户/客户的文档解析档案（Extraction Profile）。

解决「不同公司必填字段与正文槽位不同」：用 JSON 配置覆盖默认 PO/GR/INV 契约，
并附加基于正则的抽取规则（无需改 Python 即可上新字段）。

加载顺序（显式 id 优先）：
1) 请求体 extraction_profile_id 对应 {id}.json
2) 否则若存在 {org_id}.json
3) 否则若存在 default.json

目录：环境变量 EXTRACTION_PROFILES_DIR；未设置时为仓库根下 backend/config/extraction_profiles/。

JSON 可选字段：
- ``field_aliases``：将抽取到的中间键映射到 ERP/契约键（仅当目标键仍为空时写入），
  例如 ``{"supplier_code": "vendor_code"}``。
- ``extract_rules[].capture_group``：从 1 开始，默认 1；正则须有对应捕获组。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from erp_assistant_shared.contract import required_field_keys as default_required_field_keys

from app.schemas import CreateIngestionRequest, IngestionResponse

logger = logging.getLogger("ai_erp_api")


def profiles_directory() -> Path:
    raw = os.getenv("EXTRACTION_PROFILES_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    # backend/api/app/extraction_profile.py -> parents[2] = backend
    return Path(__file__).resolve().parents[2] / "config" / "extraction_profiles"


def profiles_directory_stats() -> Tuple[str, int]:
    """返回 (目录绝对路径, 其中 .json 文件数量)，供 /health 挂载。"""
    base = profiles_directory()
    try:
        if not base.is_dir():
            return str(base.resolve()), 0
        n = sum(1 for p in base.iterdir() if p.suffix.lower() == ".json" and p.is_file())
    except OSError:
        return str(base.resolve()), 0
    return str(base.resolve()), n


@dataclass
class ExtractRule:
    doc_types: Tuple[str, ...]
    field: str
    pattern: str
    flags: int = 0
    capture_group: int = 1


@dataclass
class ExtractionProfile:
    profile_id: str
    required_fields_by_doc_type: Dict[str, List[str]] = field(default_factory=dict)
    extra_required_fields_by_doc_type: Dict[str, List[str]] = field(default_factory=dict)
    extract_rules: Tuple[ExtractRule, ...] = ()
    # 客户侧字段名 / 中间槽位 -> 与契约一致的键（仅目标为空时填充）
    field_aliases: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractionProfilePick:
    """创建 ingestion 时选用的解析档案。"""

    profile_id: Optional[str]
    resolution: str
    requested_explicit: Optional[str]


_profile_cache: Dict[str, Tuple[float, Optional[ExtractionProfile]]] = {}


def _parse_flag_chars(s: str) -> int:
    fl = 0
    for ch in (s or "").lower():
        if ch == "i":
            fl |= re.IGNORECASE
        elif ch == "m":
            fl |= re.MULTILINE
        elif ch == "s":
            fl |= re.DOTALL
    return fl


def _load_profile_from_path(path: Path) -> Optional[ExtractionProfile]:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("extraction_profile_json_invalid path=%s err=%s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("profile_id") or path.stem).strip() or path.stem
    rmap = raw.get("required_fields_by_doc_type") or {}
    emap = raw.get("extra_required_fields_by_doc_type") or {}
    norm_r: Dict[str, List[str]] = {}
    norm_e: Dict[str, List[str]] = {}
    if isinstance(rmap, dict):
        for k, v in rmap.items():
            dt = str(k).strip().upper()
            if isinstance(v, list):
                norm_r[dt] = [str(x).strip() for x in v if str(x).strip()]
    if isinstance(emap, dict):
        for k, v in emap.items():
            dt = str(k).strip().upper()
            if isinstance(v, list):
                norm_e[dt] = [str(x).strip() for x in v if str(x).strip()]
    aliases: Dict[str, str] = {}
    amap = raw.get("field_aliases")
    if isinstance(amap, dict):
        for k, v in amap.items():
            sk = str(k).strip()
            sv = str(v).strip()
            if sk and sv and sk != sv:
                aliases[sk] = sv
    rules_out: List[ExtractRule] = []
    rules_raw = raw.get("extract_rules") or []
    if isinstance(rules_raw, list):
        for item in rules_raw:
            if not isinstance(item, dict):
                continue
            pat = str(item.get("pattern") or "").strip()
            fld = str(item.get("field") or "").strip()
            if not pat or not fld:
                continue
            try:
                re.compile(pat)
            except re.error as exc:
                logger.warning("extraction_profile_rule_compile_fail profile=%s field=%s err=%s", pid, fld, exc)
                continue
            dts_raw = item.get("doc_types")
            if dts_raw is None:
                dts: Tuple[str, ...] = ("PO", "GR", "INV")
            elif isinstance(dts_raw, list):
                dts = tuple(str(x).strip().upper() for x in dts_raw if str(x).strip())
            else:
                dts = ("PO", "GR", "INV")
            if not dts:
                dts = ("PO", "GR", "INV")
            flag_s = str(item.get("flags") or "")
            cg_raw = item.get("capture_group", 1)
            try:
                cg = int(cg_raw)
            except (TypeError, ValueError):
                cg = 1
            cg = max(1, min(cg, 32))
            rules_out.append(
                ExtractRule(doc_types=dts, field=fld, pattern=pat, flags=_parse_flag_chars(flag_s), capture_group=cg),
            )
    return ExtractionProfile(
        profile_id=pid,
        required_fields_by_doc_type=norm_r,
        extra_required_fields_by_doc_type=norm_e,
        extract_rules=tuple(rules_out),
        field_aliases=aliases,
    )


def get_profile(profile_id: Optional[str]) -> Optional[ExtractionProfile]:
    if not profile_id or not str(profile_id).strip():
        return None
    pid = str(profile_id).strip()
    base = profiles_directory()
    path = base / f"{pid}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _profile_cache.pop(pid, None)
        return None
    cached = _profile_cache.get(pid)
    if cached and cached[0] == mtime:
        return cached[1]
    prof = _load_profile_from_path(path)
    _profile_cache[pid] = (mtime, prof)
    if prof is None:
        logger.info("extraction_profile_not_loaded profile_id=%s path=%s", pid, path)
    else:
        logger.info(
            "extraction_profile_loaded profile_id=%s rules=%s aliases=%s",
            pid,
            len(prof.extract_rules),
            len(prof.field_aliases),
        )
    return prof


def resolve_extraction_profile(payload: CreateIngestionRequest) -> ExtractionProfilePick:
    """
    选择本次 ingestion 使用的档案 id，并标明解析方式（便于排障与多租户审计）。
    """
    base = profiles_directory()
    requested = (payload.extraction_profile_id or "").strip() or None
    if requested and (base / f"{requested}.json").is_file():
        return ExtractionProfilePick(profile_id=requested, resolution="explicit", requested_explicit=requested)
    if requested:
        logger.warning("extraction_profile_explicit_missing profile_id=%s dir=%s", requested, base)
    oid = (payload.org_id or "").strip()
    if oid and (base / f"{oid}.json").is_file():
        return ExtractionProfilePick(profile_id=oid, resolution="org_id", requested_explicit=requested)
    if (base / "default.json").is_file():
        return ExtractionProfilePick(profile_id="default", resolution="default", requested_explicit=requested)
    return ExtractionProfilePick(profile_id=None, resolution="none", requested_explicit=requested)


def resolve_stored_profile_id(payload: CreateIngestionRequest) -> Optional[str]:
    """兼容旧调用：仅返回生效的 profile id。"""
    return resolve_extraction_profile(payload).profile_id


def effective_required_field_keys(doc_type: Optional[str], profile: Optional[ExtractionProfile]) -> List[str]:
    """合并档案与 shared 契约后的必填键列表（顺序稳定）。"""
    dt = (doc_type or "PO").strip().upper() or "PO"
    if profile is None:
        return list(default_required_field_keys(doc_type))
    if profile.required_fields_by_doc_type.get(dt):
        return list(profile.required_fields_by_doc_type[dt])
    base = list(default_required_field_keys(doc_type))
    extra = profile.extra_required_fields_by_doc_type.get(dt) or []
    seen: set[str] = set()
    out: List[str] = []
    for k in base + extra:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def apply_extract_rules(text: str, doc_type: Optional[str], profile: Optional[ExtractionProfile]) -> Dict[str, str]:
    if not text or not profile or not profile.extract_rules:
        return {}
    dt = (doc_type or "PO").strip().upper() or "PO"
    out: Dict[str, str] = {}
    for rule in profile.extract_rules:
        if dt not in rule.doc_types:
            continue
        try:
            m = re.search(rule.pattern, text, rule.flags)
        except re.error as exc:
            logger.warning(
                "extraction_profile_rule_regex_error profile=%s field=%s err=%s",
                profile.profile_id,
                rule.field,
                exc,
            )
            continue
        if not m:
            continue
        cg = max(1, min(rule.capture_group, 32))
        val: Optional[str] = None
        try:
            if m.lastindex and cg <= m.lastindex:
                val = m.group(cg).strip()
            elif m.lastindex:
                val = m.group(1).strip()
            else:
                val = (m.group(0) or "").strip()
        except IndexError:
            val = None
        if val:
            out[rule.field] = val
    return out


def apply_field_aliases(hints: Dict[str, str], profile: Optional[ExtractionProfile]) -> None:
    """将客户字段名映射到契约键；仅在目标键当前为空时写入。"""
    if not profile or not profile.field_aliases:
        return
    for src, dst in profile.field_aliases.items():
        if not src or not dst or src == dst:
            continue
        v = (hints.get(src) or "").strip()
        if not v:
            continue
        if not (hints.get(dst) or "").strip():
            hints[dst] = v


def refresh_ingestion_required_keys(ing: IngestionResponse) -> None:
    """根据当前 doc_type_hint 与档案刷新 required_resolve_keys（不修改 resolved_fields）。"""
    prof = get_profile(ing.extraction_profile_id)
    dt = ing.doc_type_hint.value if ing.doc_type_hint else None
    ing.required_resolve_keys = effective_required_field_keys(dt, prof)

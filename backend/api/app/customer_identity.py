from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

logger = logging.getLogger("ai_erp_api")

CUSTOMER_OWN_COMPANY_ALIASES_ENV = "CUSTOMER_OWN_COMPANY_ALIASES_JSON"
CUSTOMER_OWN_COMPANY_KEYWORDS_ENV = "CUSTOMER_OWN_COMPANY_KEYWORDS_JSON"
DEFAULT_OWN_COMPANY_ALIASES: Tuple[str, ...] = (
    "浙江英科弹簧科技有限公司",
    "浙江英科弹簧科技",
    "英科",
)
DEFAULT_OWN_COMPANY_KEYWORDS: Tuple[str, ...] = (
    "yingke",
    "incospring",
    "浙江英科",
)
_last_config_log_signature: Tuple[bool, bool, int] | None = None
_last_keyword_config_log_signature: Tuple[bool, bool, int] | None = None


@dataclass(frozen=True)
class OwnCompanyAliasesConfig:
    aliases_by_org: Dict[str, Tuple[str, ...]]
    configured: bool
    valid: bool


@dataclass(frozen=True)
class OwnCompanyKeywordsConfig:
    keywords_by_org: Dict[str, Tuple[str, ...]]
    configured: bool
    valid: bool


@dataclass(frozen=True)
class CustomerCandidate:
    name: str
    source: str
    role: str = ""


@dataclass(frozen=True)
class CustomerIdentityResolution:
    customer_name: str
    resolution_source: str
    candidate_source: str = ""
    exact_erp_match: bool = False
    erp_lookup_failed: bool = False
    candidate_count: int = 0
    language_alias_count: int = 0
    exact_match_alias_count: int = 0
    selected_language: str = ""
    alias_match_summary: str = ""
    erp_conflict: bool = False


def normalize_company_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _deduplicate_aliases(values: Iterable[Any]) -> Tuple[str, ...]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = normalize_company_name(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return tuple(result)


def load_own_company_aliases_config() -> OwnCompanyAliasesConfig:
    raw = (os.getenv(CUSTOMER_OWN_COMPANY_ALIASES_ENV) or "").strip()
    if not raw:
        return OwnCompanyAliasesConfig(
            aliases_by_org={"*": DEFAULT_OWN_COMPANY_ALIASES},
            configured=False,
            valid=True,
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("top-level value must be an object")
        aliases_by_org: Dict[str, Tuple[str, ...]] = {}
        for raw_org, raw_aliases in payload.items():
            org = str(raw_org or "").strip()
            if not org or not isinstance(raw_aliases, list):
                raise ValueError("organization keys must be non-empty and values must be arrays")
            aliases = _deduplicate_aliases(raw_aliases)
            if aliases:
                aliases_by_org[org] = aliases
        return OwnCompanyAliasesConfig(
            aliases_by_org=aliases_by_org,
            configured=True,
            valid=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return OwnCompanyAliasesConfig(
            aliases_by_org={"*": DEFAULT_OWN_COMPANY_ALIASES},
            configured=True,
            valid=False,
        )


def _config_alias_count(config: OwnCompanyAliasesConfig) -> int:
    aliases: List[str] = [*DEFAULT_OWN_COMPANY_ALIASES]
    for configured_aliases in config.aliases_by_org.values():
        aliases.extend(configured_aliases)
    return len(_deduplicate_aliases(aliases))


def load_own_company_keywords_config() -> OwnCompanyKeywordsConfig:
    raw = (os.getenv(CUSTOMER_OWN_COMPANY_KEYWORDS_ENV) or "").strip()
    if not raw:
        return OwnCompanyKeywordsConfig(
            keywords_by_org={"*": DEFAULT_OWN_COMPANY_KEYWORDS},
            configured=False,
            valid=True,
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("top-level value must be an object")
        keywords_by_org: Dict[str, Tuple[str, ...]] = {}
        for raw_org, raw_keywords in payload.items():
            org = str(raw_org or "").strip()
            if not org or not isinstance(raw_keywords, list):
                raise ValueError("organization keys must be non-empty and values must be arrays")
            keywords = _deduplicate_aliases(raw_keywords)
            if keywords:
                keywords_by_org[org] = keywords
        return OwnCompanyKeywordsConfig(
            keywords_by_org=keywords_by_org,
            configured=True,
            valid=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return OwnCompanyKeywordsConfig(
            keywords_by_org={"*": DEFAULT_OWN_COMPANY_KEYWORDS},
            configured=True,
            valid=False,
        )


def _config_keyword_count(config: OwnCompanyKeywordsConfig) -> int:
    keywords: List[str] = [*DEFAULT_OWN_COMPANY_KEYWORDS]
    for configured_keywords in config.keywords_by_org.values():
        keywords.extend(configured_keywords)
    return len(_deduplicate_aliases(keywords))


def _log_config_status(config: OwnCompanyAliasesConfig) -> None:
    global _last_config_log_signature
    signature = (config.configured, config.valid, _config_alias_count(config))
    if signature == _last_config_log_signature:
        return
    _last_config_log_signature = signature
    log = logger.info if config.valid else logger.warning
    log(
        "customer_own_company_aliases_loaded configured=%s valid=%s alias_count=%s source=%s",
        int(config.configured),
        int(config.valid),
        signature[2],
        "environment" if config.configured and config.valid else "default_fallback",
    )


def _log_keyword_config_status(config: OwnCompanyKeywordsConfig) -> None:
    global _last_keyword_config_log_signature
    signature = (config.configured, config.valid, _config_keyword_count(config))
    if signature == _last_keyword_config_log_signature:
        return
    _last_keyword_config_log_signature = signature
    log = logger.info if config.valid else logger.warning
    log(
        "customer_own_company_keywords_loaded configured=%s valid=%s keyword_count=%s source=%s",
        int(config.configured),
        int(config.valid),
        signature[2],
        "environment" if config.configured and config.valid else "default_fallback",
    )


def own_company_aliases(org_id: str) -> Tuple[str, ...]:
    config = load_own_company_aliases_config()
    _log_config_status(config)
    configured = [*DEFAULT_OWN_COMPANY_ALIASES]
    configured.extend(config.aliases_by_org.get("*", ()))
    configured.extend(config.aliases_by_org.get(str(org_id or "").strip(), ()))
    return _deduplicate_aliases(configured)


def own_company_keywords(org_id: str) -> Tuple[str, ...]:
    config = load_own_company_keywords_config()
    _log_keyword_config_status(config)
    configured = [*DEFAULT_OWN_COMPANY_KEYWORDS]
    configured.extend(config.keywords_by_org.get("*", ()))
    configured.extend(config.keywords_by_org.get(str(org_id or "").strip(), ()))
    return _deduplicate_aliases(configured)


def customer_identity_health_payload() -> Dict[str, Any]:
    aliases_config = load_own_company_aliases_config()
    keywords_config = load_own_company_keywords_config()
    _log_config_status(aliases_config)
    _log_keyword_config_status(keywords_config)
    return {
        "customer_own_company_aliases_configured": aliases_config.configured,
        "customer_own_company_aliases_valid": aliases_config.valid,
        "customer_own_company_aliases_count": _config_alias_count(aliases_config),
        "customer_own_company_keywords_configured": keywords_config.configured,
        "customer_own_company_keywords_valid": keywords_config.valid,
        "customer_own_company_keywords_count": _config_keyword_count(keywords_config),
    }


def customer_identity_prompt_context(org_id: str) -> str:
    aliases = own_company_aliases(org_id)
    keywords = own_company_keywords(org_id)
    return (
        "本任务用于创建本销售组织的 ERP 销售订单。\n"
        f"当前销售组织：{str(org_id or '').strip() or '未提供'}\n"
        f"己方公司精确别名：{json.dumps(list(aliases), ensure_ascii=False)}\n"
        f"己方公司识别关键词（名称包含即视为己方）：{json.dumps(list(keywords), ensure_ascii=False)}\n"
        "purchaser_name 必须是向己方采购的外部客户，supplier_name 必须是销售方/供方。"
        "甲方和乙方只是合同标签，不能据此直接判断买卖角色；若一方命中己方别名或关键词，另一方才是客户候选。"
    )


def _candidate_list(
    purchaser_name: str,
    supplier_name: str,
    organization_candidates: Iterable[Any] | None = None,
) -> List[CustomerCandidate]:
    result: List[CustomerCandidate] = []
    seen: set[str] = set()
    raw_candidates: List[Tuple[Any, str, str]] = []
    # 英文增强已将 purchaser/supplier 作为结构化候选写入，避免同一公司因旧主字段
    # 被赋予错误角色而与 Buyer/Ship To 等候选产生假歧义。
    if organization_candidates is None:
        raw_candidates.extend(
            [
                (purchaser_name, "model_purchaser", "purchaser"),
                (supplier_name, "model_supplier", "supplier"),
            ]
        )
    for raw in organization_candidates or ():
        if isinstance(raw, dict):
            name = raw.get("name")
            role = str(raw.get("role") or "other").strip().lower() or "other"
            source_label = str(raw.get("source_label") or "").strip()
        else:
            name = getattr(raw, "name", "")
            role = str(getattr(raw, "role", "other") or "other").strip().lower() or "other"
            source_label = str(getattr(raw, "source_label", "") or "").strip()
        raw_candidates.append((name, f"model_{role}" + (f"_{source_label}" if source_label else ""), role))
    for name, source, role in raw_candidates:
        text = re.sub(r"\s+", " ", str(name or "")).strip()
        key = normalize_company_name(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(CustomerCandidate(name=text, source=source, role=role))
    return result


def _candidate_language(value: str) -> str:
    text = str(value or "")
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    if re.search(r"[a-z]", text, re.IGNORECASE):
        return "en"
    return "other"


def _split_bilingual_company_aliases(value: Any) -> Tuple[str, ...]:
    """只拆分明确的中英文并列名称，避免误拆纯英文换行公司名。"""
    text = str(value or "").strip()
    if not text:
        return ()
    raw_parts = re.split(r"(?:\r?\n+|[|｜]+)", text)
    parts = _deduplicate_aliases(
        re.sub(r"^[\s+|｜;；]+|[\s+|｜;；]+$", "", part)
        for part in raw_parts
    )
    languages = {_candidate_language(part) for part in parts}
    if len(parts) >= 2 and "zh" in languages and "en" in languages:
        return parts
    return (re.sub(r"\s+", " ", text).strip(),)


def _english_candidate_groups(
    purchaser_name: str,
    supplier_name: str,
    organization_candidates: Iterable[Any],
    *,
    own_alias_keys: set[str],
    own_keyword_keys: Tuple[str, ...],
) -> List[List[CustomerCandidate]]:
    raw_candidates: List[Tuple[Any, str, str]] = [
        (purchaser_name, "model_purchaser", "purchaser"),
        (supplier_name, "model_supplier", "supplier"),
    ]
    for raw in organization_candidates:
        if isinstance(raw, dict):
            name = raw.get("name")
            role = str(raw.get("role") or "other").strip().lower() or "other"
            source_label = str(raw.get("source_label") or "").strip()
        else:
            name = getattr(raw, "name", "")
            role = str(getattr(raw, "role", "other") or "other").strip().lower() or "other"
            source_label = str(getattr(raw, "source_label", "") or "").strip()
        raw_candidates.append((name, f"model_{role}" + (f"_{source_label}" if source_label else ""), role))

    candidates_by_key: Dict[str, CustomerCandidate] = {}
    linked_alias_sets: List[set[str]] = []
    for raw_name, source, role in raw_candidates:
        aliases = _split_bilingual_company_aliases(raw_name)
        alias_keys: set[str] = set()
        for alias in aliases:
            cleaned = re.sub(r"\s+", " ", str(alias or "")).strip()
            key = normalize_company_name(cleaned)
            if not key:
                continue
            alias_keys.add(key)
            candidates_by_key.setdefault(key, CustomerCandidate(name=cleaned, source=source, role=role))
        if len(alias_keys) >= 2:
            linked_alias_sets.append(alias_keys)

    external_keys = [
        key
        for key in candidates_by_key
        if key not in own_alias_keys and not any(keyword in key for keyword in own_keyword_keys)
    ]
    if not external_keys:
        return []

    parent = {key: key for key in external_keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    external_key_set = set(external_keys)
    for linked_keys in linked_alias_sets:
        available = [key for key in linked_keys if key in external_key_set]
        for key in available[1:]:
            union(available[0], key)

    grouped: Dict[str, List[CustomerCandidate]] = {}
    for key in external_keys:
        grouped.setdefault(find(key), []).append(candidates_by_key[key])
    return list(grouped.values())


def _row_customer_identity(row: Any, canonical_name: str) -> str:
    if isinstance(row, dict):
        for field in ("customerNumber", "customerId", "id"):
            value = row.get(field)
            if value is not None and str(value).strip():
                return f"{field}:{str(value).strip()}"
    return f"name:{normalize_company_name(canonical_name)}"


def _preferred_group_candidate(group: Sequence[CustomerCandidate]) -> CustomerCandidate:
    preferred_roles = {"buyer", "purchaser", "ordering_company"}
    preferred = [candidate for candidate in group if candidate.role in preferred_roles]
    candidates = preferred or list(group)
    return next(
        (candidate for candidate in candidates if _candidate_language(candidate.name) == "zh"),
        candidates[0],
    )


def _resolve_english_customer_identity(
    *,
    org_id: str,
    purchaser_name: str,
    supplier_name: str,
    erp: Any,
    organization_candidates: Iterable[Any],
    own_alias_keys: set[str],
    own_keyword_keys: Tuple[str, ...],
) -> CustomerIdentityResolution:
    groups = _english_candidate_groups(
        purchaser_name,
        supplier_name,
        organization_candidates,
        own_alias_keys=own_alias_keys,
        own_keyword_keys=own_keyword_keys,
    )
    alias_count = sum(len(group) for group in groups)
    if not groups:
        return CustomerIdentityResolution(
            customer_name="",
            resolution_source="ambiguous",
            candidate_count=0,
            language_alias_count=0,
        )

    exact_matches: List[Tuple[int, CustomerCandidate, str, str]] = []
    failed_group_indexes: set[int] = set()
    erp_lookup_failed = False
    outcomes: List[str] = []
    for group_index, group in enumerate(groups):
        for candidate in group:
            language = _candidate_language(candidate.name)
            try:
                rows: Sequence[Any] = erp.search_customers(org_id, candidate.name, 1, 20)
            except Exception as exc:
                erp_lookup_failed = True
                failed_group_indexes.add(group_index)
                outcomes.append(f"{language}:error")
                logger.warning(
                    "customer_identity_erp_lookup_failed org_id=%s candidate_source=%s language=%s error=%s",
                    org_id,
                    candidate.source,
                    language,
                    exc,
                )
                continue
            candidate_key = normalize_company_name(candidate.name)
            exact_rows = [
                row
                for row in rows
                if normalize_company_name(_row_customer_name(row)) == candidate_key
            ]
            outcomes.append(f"{language}:{'exact' if exact_rows else 'miss'}")
            for row in exact_rows:
                canonical_name = _row_customer_name(row) or candidate.name
                exact_matches.append(
                    (
                        group_index,
                        candidate,
                        canonical_name,
                        _row_customer_identity(row, canonical_name),
                    )
                )

    matches_by_identity: Dict[str, List[Tuple[int, CustomerCandidate, str, str]]] = {}
    for match in exact_matches:
        matches_by_identity.setdefault(match[3], []).append(match)
    summary = ",".join(outcomes)
    if len(matches_by_identity) > 1:
        identities_by_group: Dict[int, set[str]] = {}
        for identity, matches in matches_by_identity.items():
            for match in matches:
                identities_by_group.setdefault(match[0], set()).add(identity)
        bilingual_alias_conflict = any(len(identities) > 1 for identities in identities_by_group.values())
        return CustomerIdentityResolution(
            customer_name="",
            resolution_source="erp_conflict" if bilingual_alias_conflict else "ambiguous",
            erp_lookup_failed=erp_lookup_failed,
            candidate_count=len(groups),
            language_alias_count=alias_count,
            exact_match_alias_count=len(exact_matches),
            alias_match_summary=summary,
            erp_conflict=bilingual_alias_conflict,
        )
    if len(matches_by_identity) == 1:
        matches = next(iter(matches_by_identity.values()))
        matched_group_indexes = {match[0] for match in matches}
        unsafe_failed_groups = failed_group_indexes - matched_group_indexes
        if not unsafe_failed_groups:
            selected = next(
                (match for match in matches if _candidate_language(match[1].name) == "zh"),
                matches[0],
            )
            _, candidate, canonical_name, _ = selected
            return CustomerIdentityResolution(
                customer_name=canonical_name or candidate.name,
                resolution_source="erp_exact",
                candidate_source=candidate.source,
                exact_erp_match=True,
                erp_lookup_failed=erp_lookup_failed,
                candidate_count=len(groups),
                language_alias_count=alias_count,
                exact_match_alias_count=len(exact_matches),
                selected_language=_candidate_language(candidate.name),
                alias_match_summary=summary,
            )

    preferred_roles = {"buyer", "purchaser", "ordering_company"}
    preferred_groups = [
        group
        for group in groups
        if any(candidate.role in preferred_roles for candidate in group)
    ]
    if not exact_matches and len(preferred_groups) == 1:
        candidate = _preferred_group_candidate(preferred_groups[0])
        return CustomerIdentityResolution(
            customer_name=candidate.name,
            resolution_source="sole_external_buyer",
            candidate_source=candidate.source,
            erp_lookup_failed=erp_lookup_failed,
            candidate_count=len(groups),
            language_alias_count=alias_count,
            selected_language=_candidate_language(candidate.name),
            alias_match_summary=summary,
        )
    return CustomerIdentityResolution(
        customer_name="",
        resolution_source="ambiguous",
        erp_lookup_failed=erp_lookup_failed,
        candidate_count=len(groups),
        language_alias_count=alias_count,
        exact_match_alias_count=len(exact_matches),
        alias_match_summary=summary,
    )


def _row_customer_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("customerName") or row.get("name") or "").strip()


def resolve_customer_identity(
    *,
    org_id: str,
    purchaser_name: str,
    supplier_name: str,
    erp: Any,
    organization_candidates: Iterable[Any] | None = None,
) -> CustomerIdentityResolution:
    own_alias_keys = {normalize_company_name(alias) for alias in own_company_aliases(org_id)}
    own_keyword_keys = tuple(normalize_company_name(keyword) for keyword in own_company_keywords(org_id))
    if organization_candidates is not None:
        return _resolve_english_customer_identity(
            org_id=org_id,
            purchaser_name=purchaser_name,
            supplier_name=supplier_name,
            erp=erp,
            organization_candidates=organization_candidates,
            own_alias_keys=own_alias_keys,
            own_keyword_keys=own_keyword_keys,
        )
    external_candidates = [
        candidate
        for candidate in _candidate_list(purchaser_name, supplier_name, organization_candidates)
        if normalize_company_name(candidate.name) not in own_alias_keys
        and not any(keyword in normalize_company_name(candidate.name) for keyword in own_keyword_keys)
    ]
    if not external_candidates:
        return CustomerIdentityResolution(
            customer_name="",
            resolution_source="ambiguous",
            candidate_count=0,
        )

    exact_candidates: List[Tuple[CustomerCandidate, str]] = []
    erp_lookup_failed = False
    for candidate in external_candidates:
        try:
            rows: Sequence[Any] = erp.search_customers(org_id, candidate.name, 1, 20)
        except Exception as exc:
            erp_lookup_failed = True
            logger.warning(
                "customer_identity_erp_lookup_failed org_id=%s candidate_source=%s error=%s",
                org_id,
                candidate.source,
                exc,
            )
            continue
        candidate_key = normalize_company_name(candidate.name)
        exact_names = [
            _row_customer_name(row)
            for row in rows
            if normalize_company_name(_row_customer_name(row)) == candidate_key
        ]
        if exact_names:
            exact_candidates.append((candidate, exact_names[0]))

    if len(exact_candidates) == 1 and not (erp_lookup_failed and len(external_candidates) > 1):
        candidate, canonical_name = exact_candidates[0]
        return CustomerIdentityResolution(
            customer_name=canonical_name or candidate.name,
            resolution_source="erp_exact",
            candidate_source=candidate.source,
            exact_erp_match=True,
            erp_lookup_failed=erp_lookup_failed,
            candidate_count=len(external_candidates),
        )
    # 中文当前路径没有扩展候选，保留原有“唯一外部公司”行为，防止回归。
    if organization_candidates is None and not exact_candidates and len(external_candidates) == 1:
        candidate = external_candidates[0]
        return CustomerIdentityResolution(
            customer_name=candidate.name,
            resolution_source="sole_external",
            candidate_source=candidate.source,
            erp_lookup_failed=erp_lookup_failed,
            candidate_count=1,
        )
    return CustomerIdentityResolution(
        customer_name="",
        resolution_source="ambiguous",
        erp_lookup_failed=erp_lookup_failed,
        candidate_count=len(external_candidates),
    )

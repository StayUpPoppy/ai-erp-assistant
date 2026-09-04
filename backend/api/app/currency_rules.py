from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


DEFAULT_CURRENCY = "CNY"
PRESET_CURRENCY_RATES: dict[str, float] = {
    "CNY": 1.0,
    "USD": 7.2,
    "EUR": 7.8,
    "JPY": 0.048,
}

_CURRENCY_ALIASES: dict[str, str] = {
    "cny": "CNY",
    "rmb": "CNY",
    "renminbi": "CNY",
    "chineseyuan": "CNY",
    "人民币": "CNY",
    "人民币元": "CNY",
    "usd": "USD",
    "usdollar": "USD",
    "usdollars": "USD",
    "unitedstatesdollar": "USD",
    "美元": "USD",
    "美金": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "欧元": "EUR",
    "jpy": "JPY",
    "yen": "JPY",
    "japaneseyen": "JPY",
    "日元": "JPY",
}

_LABELED_CURRENCY_RE = re.compile(r"(?i:currency(?:\s+code)?|币别|币种|货币)\s*[:：]?\s*")
_SUPPORTED_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:CNY|RMB)\b", re.IGNORECASE), "CNY"),
    (re.compile(r"人民币(?:元)?", re.IGNORECASE), "CNY"),
    (re.compile(r"\bUSD\b|\bUS\s*Dollars?\b", re.IGNORECASE), "USD"),
    (re.compile(r"美\s*元|美金"), "USD"),
    (re.compile(r"\bEUR\b|\bEuros?\b", re.IGNORECASE), "EUR"),
    (re.compile(r"欧\s*元"), "EUR"),
    (re.compile(r"\bJPY\b|\bJapanese\s+Yen\b", re.IGNORECASE), "JPY"),
    (re.compile(r"日\s*元"), "JPY"),
)
_COMMON_OTHER_CURRENCY_CODES = {"GBP", "HKD", "AUD", "CAD", "CHF", "SGD"}


@dataclass(frozen=True)
class CurrencyResolution:
    currency: str
    rate: Optional[float]
    source: str
    conflict: bool = False
    evidence: tuple[str, ...] = ()


def normalize_currency(value: object) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return ""
    compact = re.sub(r"[\s._-]+", "", raw).casefold()
    alias = _CURRENCY_ALIASES.get(compact)
    if alias:
        return alias
    if re.fullmatch(r"[A-Za-z]{3}", raw):
        return raw.upper()
    return ""


def preset_currency_rate(currency: object) -> Optional[float]:
    return PRESET_CURRENCY_RATES.get(normalize_currency(currency))


def _currencies_in_fragment(fragment: str, *, include_other_codes: bool) -> set[str]:
    found = {currency for pattern, currency in _SUPPORTED_TEXT_PATTERNS if pattern.search(fragment)}
    if include_other_codes:
        found.update(code for code in re.findall(r"\b[A-Z]{3}\b", fragment) if code in _COMMON_OTHER_CURRENCY_CODES)
    return found


def _labeled_currencies(text: str) -> set[str]:
    found: set[str] = set()
    for line in (text or "").splitlines():
        for match in _LABELED_CURRENCY_RE.finditer(line):
            # Only inspect the value area immediately following the label. This
            # avoids treating legal prose such as "currency stipulated..." as a code.
            value_area = line[match.end() : match.end() + 48]
            found.update(_currencies_in_fragment(value_area, include_other_codes=True))
            first_code = re.match(r"\s*([A-Z]{3})\b", value_area, re.IGNORECASE)
            if first_code:
                found.add(first_code.group(1).upper())
    return found


def _monetary_currencies(text: str) -> set[str]:
    found: set[str] = set()
    for line in (text or "").splitlines():
        if not re.search(r"\d", line):
            continue
        supported = _currencies_in_fragment(line, include_other_codes=False)
        if supported:
            found.update(supported)
        for code in _COMMON_OTHER_CURRENCY_CODES:
            if re.search(rf"(?:\d[\d.,]*)\s*{code}\b|\b{code}\s*\d", line):
                found.add(code)
    return found


def resolve_order_currency(document_text: str, model_currency: object = "") -> CurrencyResolution:
    labeled = _labeled_currencies(document_text)
    monetary = _monetary_currencies(document_text)
    explicit = labeled | monetary
    evidence = tuple(sorted(explicit))
    if len(explicit) > 1:
        return CurrencyResolution(
            currency="",
            rate=None,
            source="explicit_conflict",
            conflict=True,
            evidence=evidence,
        )
    if explicit:
        currency = next(iter(explicit))
        source = "labeled_text" if currency in labeled else "monetary_text"
        return CurrencyResolution(
            currency=currency,
            rate=preset_currency_rate(currency),
            source=source,
            evidence=evidence,
        )

    currency = normalize_currency(model_currency)
    if currency:
        return CurrencyResolution(
            currency=currency,
            rate=preset_currency_rate(currency),
            source="model",
            evidence=(currency,),
        )

    return CurrencyResolution(
        currency=DEFAULT_CURRENCY,
        rate=PRESET_CURRENCY_RATES[DEFAULT_CURRENCY],
        source="default",
    )

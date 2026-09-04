from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.currency_rules import normalize_currency, resolve_order_currency


@pytest.mark.parametrize(
    ("raw", "expected_currency", "expected_rate"),
    [
        ("CNY", "CNY", 1.0),
        ("RMB", "CNY", 1.0),
        ("人民币", "CNY", 1.0),
        ("usd", "USD", 7.2),
        ("US Dollar", "USD", 7.2),
        ("美元", "USD", 7.2),
        ("EUR", "EUR", 7.8),
        ("Euro", "EUR", 7.8),
        ("欧元", "EUR", 7.8),
        ("JPY", "JPY", 0.048),
        ("Japanese Yen", "JPY", 0.048),
        ("日元", "JPY", 0.048),
    ],
)
def test_model_currency_aliases_use_fixed_rates(raw: str, expected_currency: str, expected_rate: float) -> None:
    result = resolve_order_currency("", raw)

    assert result.currency == expected_currency
    assert result.rate == expected_rate
    assert result.source == "model"


def test_explicit_currency_header_wins_over_model_and_sets_fixed_rate() -> None:
    text = "Purchase Order\nCurrency USD\nNet price 6,00 USD"

    result = resolve_order_currency(text, "CNY")

    assert result.currency == "USD"
    assert result.rate == 7.2
    assert result.source == "labeled_text"
    assert result.conflict is False


@pytest.mark.parametrize(
    ("text", "currency", "rate"),
    [
        ("币别：人民币", "CNY", 1.0),
        ("Currency: US Dollar", "USD", 7.2),
        ("币种：欧元", "EUR", 7.8),
        ("Currency Japanese Yen", "JPY", 0.048),
    ],
)
def test_labeled_currency_names_are_detected(text: str, currency: str, rate: float) -> None:
    result = resolve_order_currency(text)

    assert result.currency == currency
    assert result.rate == rate


def test_currency_next_to_amount_is_detected_without_header() -> None:
    result = resolve_order_currency("Unit Price 12.50 EUR\nTotal 25.00 EUR")

    assert result.currency == "EUR"
    assert result.rate == 7.8
    assert result.source == "monetary_text"


def test_missing_currency_evidence_defaults_to_cny() -> None:
    result = resolve_order_currency("采购订单\n数量 10\n单价 20")

    assert result.currency == "CNY"
    assert result.rate == 1.0
    assert result.source == "default"


def test_bare_yen_symbol_does_not_select_jpy() -> None:
    result = resolve_order_currency("合计：￥100.00")

    assert result.currency == "CNY"
    assert result.rate == 1.0
    assert result.source == "default"


def test_conflicting_explicit_currencies_block_automatic_selection() -> None:
    result = resolve_order_currency("Currency USD\nAlternative amount 100 EUR")

    assert result.currency == ""
    assert result.rate is None
    assert result.conflict is True
    assert result.source == "explicit_conflict"
    assert result.evidence == ("EUR", "USD")


def test_non_preset_currency_is_preserved_without_rate() -> None:
    result = resolve_order_currency("Currency gbp")

    assert result.currency == "GBP"
    assert result.rate is None
    assert result.conflict is False


def test_normalize_currency_rejects_non_currency_words() -> None:
    assert normalize_currency("unknown currency") == ""


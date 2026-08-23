from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.customer_identity import (
    customer_identity_health_payload,
    customer_identity_prompt_context,
    normalize_company_name,
    own_company_aliases,
    own_company_keywords,
    resolve_customer_identity,
)


class CustomerErp:
    def __init__(self, records=None, *, fail_names=None):
        self.records = records or {}
        self.fail_names = set(fail_names or ())
        self.calls = []

    def search_customers(self, org_id, keyword, page_num, page_size):
        self.calls.append((org_id, keyword, page_num, page_size))
        if keyword in self.fail_names:
            raise RuntimeError("customer master unavailable")
        return self.records.get(keyword, [])


def test_default_aliases_and_health_do_not_expose_names(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_KEYWORDS_JSON", raising=False)

    aliases = own_company_aliases("英科1厂")
    keywords = own_company_keywords("英科1厂")
    health = customer_identity_health_payload()

    assert aliases == ("浙江英科弹簧科技有限公司", "浙江英科弹簧科技")
    assert keywords == ("yingke", "incospring")
    assert health == {
        "customer_own_company_aliases_configured": False,
        "customer_own_company_aliases_valid": True,
        "customer_own_company_aliases_count": 2,
        "customer_own_company_keywords_configured": False,
        "customer_own_company_keywords_valid": True,
        "customer_own_company_keywords_count": 2,
    }
    assert "浙江英科" not in str(health)
    assert "yingke" not in str(health)


def test_org_aliases_merge_with_defaults_and_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv(
        "CUSTOMER_OWN_COMPANY_ALIASES_JSON",
        '{"*":["英科集团"],"英科1厂":["英科一厂有限公司"]}',
    )
    assert own_company_aliases("英科1厂") == (
        "浙江英科弹簧科技有限公司",
        "浙江英科弹簧科技",
        "英科集团",
        "英科一厂有限公司",
    )

    monkeypatch.setenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", "not-json")
    health = customer_identity_health_payload()
    assert health["customer_own_company_aliases_configured"] is True
    assert health["customer_own_company_aliases_valid"] is False
    assert own_company_aliases("英科1厂") == ("浙江英科弹簧科技有限公司", "浙江英科弹簧科技")


def test_org_keywords_merge_with_defaults_and_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv(
        "CUSTOMER_OWN_COMPANY_KEYWORDS_JSON",
        '{"*":["global brand"],"英科1厂":["factory-brand"]}',
    )
    assert own_company_keywords("英科1厂") == (
        "yingke",
        "incospring",
        "global brand",
        "factory-brand",
    )

    monkeypatch.setenv("CUSTOMER_OWN_COMPANY_KEYWORDS_JSON", "not-json")
    health = customer_identity_health_payload()
    assert health["customer_own_company_keywords_configured"] is True
    assert health["customer_own_company_keywords_valid"] is False
    assert own_company_keywords("英科1厂") == ("yingke", "incospring")


def test_company_normalization_handles_unicode_spaces_and_punctuation():
    assert normalize_company_name(" 浙江英科（弹簧）科技有限公司 ") == normalize_company_name(
        "浙江英科(弹簧)科技有限公司"
    )
    assert normalize_company_name("ＡＣＭＥ，ＬＴＤ.") == "acmeltd"


def test_own_purchaser_is_excluded_and_external_supplier_becomes_erp_customer(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    external = "江苏明通福路流体控制设备有限公司"
    erp = CustomerErp({external: [{"customerName": external}]})

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="浙江英科弹簧科技有限公司",
        supplier_name=external,
        erp=erp,
    )

    assert result.customer_name == external
    assert result.resolution_source == "erp_exact"
    assert result.candidate_source == "model_supplier"
    assert result.exact_erp_match is True


def test_external_purchaser_is_kept_and_name_containing_yingke_is_not_fuzzy_excluded(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    external = "江苏英科阀门有限公司"
    erp = CustomerErp({external: [{"customerName": external}]})

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name=external,
        supplier_name="浙江英科弹簧科技",
        erp=erp,
    )

    assert result.customer_name == external
    assert result.candidate_source == "model_purchaser"
    assert result.resolution_source == "erp_exact"


def test_english_own_company_keywords_filter_case_spacing_punctuation_and_suffixes(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_KEYWORDS_JSON", raising=False)
    external = "外部客户有限公司"
    own_company_variants = (
        "YingKe Holding Co., Ltd.",
        "YING-KE INDUSTRIAL GROUP",
        "Inco Spring (Zhejiang) Co., Ltd.",
        "Shanghai INCOSPRING Manufacturing",
    )

    for own_company in own_company_variants:
        erp = CustomerErp({external: [{"customerName": external}]})
        result = resolve_customer_identity(
            org_id="英科1厂",
            purchaser_name=own_company,
            supplier_name=external,
            erp=erp,
        )

        assert result.customer_name == external
        assert result.resolution_source == "erp_exact"
        assert result.candidate_source == "model_supplier"
        assert erp.calls == [("英科1厂", external, 1, 20)]


def test_english_own_company_keywords_leave_customer_blank_when_every_candidate_is_own(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_KEYWORDS_JSON", raising=False)
    erp = CustomerErp()

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="Yingke Holding Co Ltd",
        supplier_name="Incospring (Zhejiang) Co Ltd",
        erp=erp,
    )

    assert result.customer_name == ""
    assert result.resolution_source == "ambiguous"
    assert result.candidate_count == 0
    assert erp.calls == []


def test_sole_external_is_used_with_non_exact_result(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    erp = CustomerErp()

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="外部客户有限公司",
        supplier_name="浙江英科弹簧科技有限公司",
        erp=erp,
    )

    assert result.customer_name == "外部客户有限公司"
    assert result.resolution_source == "sole_external"
    assert result.exact_erp_match is False


def test_multiple_external_candidates_without_unique_erp_match_are_ambiguous(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    erp = CustomerErp(
        {
            "候选客户一有限公司": [{"customerName": "候选客户一有限公司"}],
            "候选客户二有限公司": [{"customerName": "候选客户二有限公司"}],
        }
    )

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="候选客户一有限公司",
        supplier_name="候选客户二有限公司",
        erp=erp,
    )

    assert result.customer_name == ""
    assert result.resolution_source == "ambiguous"
    assert result.candidate_count == 2


def test_failed_lookup_for_other_candidate_prevents_false_unique_match(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    erp = CustomerErp(
        {"候选客户一有限公司": [{"customerName": "候选客户一有限公司"}]},
        fail_names={"候选客户二有限公司"},
    )

    result = resolve_customer_identity(
        org_id="英科1厂",
        purchaser_name="候选客户一有限公司",
        supplier_name="候选客户二有限公司",
        erp=erp,
    )

    assert result.customer_name == ""
    assert result.resolution_source == "ambiguous"
    assert result.erp_lookup_failed is True


def test_prompt_context_includes_org_and_own_aliases(monkeypatch):
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_ALIASES_JSON", raising=False)
    monkeypatch.delenv("CUSTOMER_OWN_COMPANY_KEYWORDS_JSON", raising=False)
    context = customer_identity_prompt_context("英科1厂")
    assert "英科1厂" in context
    assert "浙江英科弹簧科技有限公司" in context
    assert "yingke" in context
    assert "incospring" in context
    assert "甲方和乙方只是合同标签" in context

"""共享包 `erp_assistant_shared` 与 ingestion 契约一致。"""

from erp_assistant_shared.contract import REQUIRED_FLAT_FIELDS_BY_DOC_TYPE, required_field_keys


def test_required_field_keys_matches_documented_order():
    assert required_field_keys(None) == list(REQUIRED_FLAT_FIELDS_BY_DOC_TYPE["PO"])
    assert required_field_keys("gr") == list(REQUIRED_FLAT_FIELDS_BY_DOC_TYPE["GR"])
    assert required_field_keys("INV") == list(REQUIRED_FLAT_FIELDS_BY_DOC_TYPE["INV"])
    assert required_field_keys("UNKNOWN") == list(REQUIRED_FLAT_FIELDS_BY_DOC_TYPE["PO"])

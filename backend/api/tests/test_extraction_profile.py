import json
from pathlib import Path

from app.extraction_profile import (
    apply_extract_rules,
    apply_field_aliases,
    effective_required_field_keys,
    get_profile,
    resolve_extraction_profile,
    resolve_stored_profile_id,
)
from app.schemas import CreateIngestionRequest


def test_effective_required_merge_extra(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "p1.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "p1",
                "extra_required_fields_by_doc_type": {"PO": ["project_code"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prof = get_profile("p1")
    assert prof is not None
    keys = effective_required_field_keys("PO", prof)
    assert "project_code" in keys
    assert keys.index("vendor_code") < keys.index("project_code")


def test_effective_required_full_replace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "p2.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "p2",
                "required_fields_by_doc_type": {"PO": ["vendor_code", "custom_ref"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prof = get_profile("p2")
    assert effective_required_field_keys("PO", prof) == ["vendor_code", "custom_ref"]


def test_apply_extract_rules(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "p3.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "p3",
                "extract_rules": [
                    {"doc_types": ["PO"], "field": "project_code", "pattern": r"项目编号[:：]\s*(\S+)"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prof = get_profile("p3")
    text = "抬头\n项目编号：PRJ-001\n"
    got = apply_extract_rules(text, "PO", prof)
    assert got.get("project_code") == "PRJ-001"


def test_resolve_stored_profile_id_explicit_then_org(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    (tmp_path / "acme.json").write_text('{"profile_id": "acme"}', encoding="utf-8")

    req = CreateIngestionRequest(
        file_id="f",
        file_hash="h" * 64,
        user_id="u",
        org_id="other",
        extraction_profile_id="acme",
    )
    assert resolve_stored_profile_id(req) == "acme"
    p = resolve_extraction_profile(req)
    assert p.resolution == "explicit" and p.requested_explicit == "acme"

    req2 = CreateIngestionRequest(
        file_id="f",
        file_hash="h" * 65,
        user_id="u",
        org_id="acme",
    )
    assert resolve_stored_profile_id(req2) == "acme"
    p2 = resolve_extraction_profile(req2)
    assert p2.resolution == "org_id" and p2.requested_explicit is None

    req3 = CreateIngestionRequest(
        file_id="f",
        file_hash="h" * 66,
        user_id="u",
        org_id="nope",
        extraction_profile_id="missing-file",
    )
    assert resolve_stored_profile_id(req3) is None
    p3 = resolve_extraction_profile(req3)
    assert p3.resolution == "none" and p3.requested_explicit == "missing-file"


def test_resolve_explicit_miss_falls_back_org(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    (tmp_path / "acme.json").write_text('{"profile_id": "acme"}', encoding="utf-8")
    req = CreateIngestionRequest(
        file_id="f",
        file_hash="h" * 67,
        user_id="u",
        org_id="acme",
        extraction_profile_id="nope",
    )
    pick = resolve_extraction_profile(req)
    assert pick.profile_id == "acme"
    assert pick.resolution == "org_id"
    assert pick.requested_explicit == "nope"


def test_apply_field_aliases_fills_target_when_empty() -> None:
    from app.extraction_profile import ExtractionProfile

    prof = ExtractionProfile(profile_id="x", field_aliases={"supplier_code": "vendor_code"})
    hints = {"supplier_code": "S001", "vendor_code": ""}
    apply_field_aliases(hints, prof)
    assert hints["vendor_code"] == "S001"


def test_apply_field_aliases_skips_when_target_nonempty() -> None:
    from app.extraction_profile import ExtractionProfile

    prof = ExtractionProfile(profile_id="x", field_aliases={"supplier_code": "vendor_code"})
    hints = {"supplier_code": "S001", "vendor_code": "V9"}
    apply_field_aliases(hints, prof)
    assert hints["vendor_code"] == "V9"


def test_invalid_regex_rule_skipped_at_load(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "badre.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "badre",
                "extract_rules": [
                    {"field": "a", "pattern": "(?P<x>[invalid"},
                    {"field": "b", "pattern": "(\\d+)"},
                ],
            },
        ),
        encoding="utf-8",
    )
    prof = get_profile("badre")
    assert prof is not None
    assert len(prof.extract_rules) == 1
    assert prof.extract_rules[0].field == "b"


def test_capture_group_second(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "cg.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "cg",
                "extract_rules": [
                    {
                        "doc_types": ["PO"],
                        "field": "ref_no",
                        "pattern": r"REF\s*(\w+)\s+(\w+)",
                        "capture_group": 2,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    prof = get_profile("cg")
    assert prof is not None
    out = apply_extract_rules("REF AA BB", "PO", prof)
    assert out.get("ref_no") == "BB"


def test_structured_extract_with_profile(monkeypatch, tmp_path: Path) -> None:
    from app.structured_extract import extract_structured_fields

    monkeypatch.setenv("EXTRACTION_PROFILES_DIR", str(tmp_path))
    p = tmp_path / "p4.json"
    p.write_text(
        json.dumps(
            {
                "profile_id": "p4",
                "extract_rules": [
                    {"doc_types": ["PO"], "field": "project_code", "pattern": r"项目编号[:：]\s*(\S+)"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prof = get_profile("p4")
    text = "物料号：M001\n项目编号：X-9\n"
    out = extract_structured_fields(text, "PO", prof)
    assert out.get("project_code") == "X-9"
    assert out.get("material_code")


def test_datynk_dev_profile_shipped_in_repo(monkeypatch) -> None:
    monkeypatch.delenv("EXTRACTION_PROFILES_DIR", raising=False)
    prof = get_profile("datynk-dev")
    assert prof is not None
    keys = effective_required_field_keys("PO", prof)
    assert "org" in keys
    assert "customerName" in keys

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from norway_company_agent.evidence import evidence
from norway_company_agent.refresh import classify_refresh_failures, diff_profile
from norway_company_agent.synthesis import build_synthesis, validate_synthesis
from test_synthesis import profile


def changed(old, new):
    changes = diff_profile(old, new)
    # Classification is safe both directly and through the production batch wrapper.
    assert classify_refresh_failures(old, new, copy.deepcopy(changes)) == changes
    new["changes"] = changes
    synthesis = build_synthesis(new, changes=changes, refresh={
        "previous_snapshot_supplied": True, "company_in_previous_snapshot": True,
    })
    assert validate_synthesis(synthesis, new)["passed"]
    return changes, synthesis


@pytest.mark.parametrize("module", ["roles", "financials", "locations", "registry_live", "nav_jobs", "site_signals"])
@pytest.mark.parametrize("status", ["source_error", "blocked"])
def test_failed_sources_never_report_material_removals(module, status):
    old, new = profile(), profile()
    if module == "nav_jobs":
        old["evidence"][module].update(status="available", value={"postings": [{"uuid": "old-job"}]})
    new["evidence"][module] = evidence(module, status, "official", "https://example.test/source", note="timeout")
    changes, synthesis = changed(old, new)
    affected = [c for c in changes if c["source_module"] == module]
    assert affected
    assert all(c["material"] is False for c in affected)
    assert synthesis["what_changed"]["count"] == 0
    assert "incomplete" in synthesis["what_changed"]["text"]
    text = " ".join(s["text"] for s in synthesis["statements"] if s["section"] == "what_changed")
    assert "removed" not in text
    assert "previously verified website" not in text


def test_confirmed_empty_roles_is_a_real_removal_but_http_404_is_not():
    old, new = profile(), profile()
    new["evidence"]["roles"]["value"]["roles"] = []
    changes, synthesis = changed(old, new)
    assert len(changes) == 1 and changes[0]["material"] is True
    assert any("removed" in s["text"] for s in synthesis["statements"] if s["section"] == "what_changed")
    new["evidence"]["roles"] = evidence("roles", "not_found", "official", "https://example.test/roles")
    changes, synthesis = changed(old, new)
    assert len(changes) == 1 and changes[0]["material"] is False


def test_source_recovery_is_not_a_new_director_appointment():
    old, new = profile(), profile()
    old["evidence"]["roles"] = evidence("roles", "source_error", "official", "https://example.test/roles")
    changes, synthesis = changed(old, new)
    assert changes[0]["change_type"] == "previous_source_unavailable"
    assert synthesis["what_changed"]["count"] == 0


def test_live_employees_change_with_frozen_bulk_has_live_provenance():
    old, new = profile(), profile()
    old["employees"] = new["employees"] = 10
    old["evidence"]["registry_live"]["value"]["employees"] = 10
    new["evidence"]["registry_live"]["value"]["employees"] = 20
    new["evidence"]["registry_live"]["content_sha256"] = "f" * 64
    changes, synthesis = changed(old, new)
    assert len(changes) == 1
    assert changes[0]["field"] == "registry.employees"
    assert (changes[0]["old_value"], changes[0]["new_value"]) == (10, 20)
    assert changes[0]["source_module"] == "registry_live"
    assert changes[0]["new_content_sha256"] == "f" * 64
    assert synthesis["what_changed"]["count"] == 1
    assert any("10 → 20" in s["text"] for s in synthesis["statements"])
    assert diff_profile(new, copy.deepcopy(new)) == []


@pytest.mark.parametrize("field,old_value,new_value", [
    ("name", "ALPHA AS", "BETA AS"),
    ("legal_form", "AS", "ASA"),
    ("website", "old.example", "new.example"),
    ("latest_submitted_accounts", "2024", "2025"),
    ("municipality", "MOSS", "BERGEN"),
])
def test_other_live_registry_fields_use_current_values(field, old_value, new_value):
    old, new = profile(), profile()
    if field == "municipality":
        old["evidence"]["registry_live"]["value"]["business_address"]["kommune"] = old_value
        new["evidence"]["registry_live"]["value"]["business_address"]["kommune"] = new_value
    else:
        old["evidence"]["registry_live"]["value"][field] = old_value
        new["evidence"]["registry_live"]["value"][field] = new_value
    changes, synthesis = changed(old, new)
    assert len(changes) == 1 and changes[0]["field"] == f"registry.{field}"
    assert changes[0]["new_value"] == new_value
    assert synthesis["what_changed"]["count"] == 1


def test_null_employee_count_is_unknown_not_zero_or_removal():
    old, new = profile(), profile()
    new["evidence"]["registry_live"]["value"]["employees"] = None
    changes, synthesis = changed(old, new)
    assert changes[0]["new_value"] is None
    assert changes[0]["material"] is False
    assert synthesis["what_changed"]["count"] == 0


def test_bulk_only_change_cites_bulk_not_nonexistent_live_record():
    old, new = profile(), profile()
    del old["evidence"]["registry_live"]
    del new["evidence"]["registry_live"]
    old["employees"], new["employees"] = 10, 20
    changes, synthesis = changed(old, new)
    assert changes[0]["source_module"] == "registry"
    assert any("10 → 20" in s["text"] for s in synthesis["statements"])


def test_switch_from_live_to_old_bulk_does_not_invent_employee_drop():
    old, new = profile(), profile()
    old["employees"] = new["employees"] = 10
    old["evidence"]["registry_live"]["value"]["employees"] = 20
    del new["evidence"]["registry_live"]
    changes, synthesis = changed(old, new)
    assert all(c["material"] is False for c in changes)
    assert changes[0]["change_type"] == "source_changed"
    assert synthesis["what_changed"]["count"] == 0


@pytest.mark.parametrize("module,field,key,extra", [
    ("nav_jobs", "posting_ids", "postings", {"rejected_candidates": [{"reason": "detail fetch failed: timeout"}]}),
    ("nav_jobs", "posting_ids", "postings", {"candidates_total": 30, "candidates_checked": 25}),
    ("nav_jobs", "posting_ids", "postings", {"collection_complete": False}),
    ("site_signals", "news_post_urls", "news_posts", {"errors": [{"error": "news timeout"}]}),
    ("site_signals", "news_post_urls", "news_posts", {"errors": [{"error": "request budget reached"}]}),
])
@pytest.mark.parametrize("status", ["available", "not_found"])
def test_partial_collections_do_not_confirm_missing_items(module, field, key, extra, status):
    old, new = profile(), profile()
    old["evidence"][module].update(status="available", value={key: [{"uuid": "old", "url": "https://example.test/old"}]})
    new["evidence"][module].update(status=status, value={key: [], **extra})
    changes, synthesis = changed(old, new)
    change = next(c for c in changes if c["field"] == f"{module}.{field}")
    assert change["material"] is False
    assert synthesis["what_changed"]["count"] == 0


def test_nav_detail_failures_mark_collection_incomplete_even_if_error_list_truncated():
    from norway_company_agent.nav_jobs import collect_nav_jobs

    class Feed:
        status, synced_at, window_days, token_source = "synced", "t", 183, "test"
        requests, bytes = 0, 0

        def candidates(self, company):
            return ([{"uuid": str(i), "url": "/" + str(i)} for i in range(22)], {company["organisation_number"]}, ["example"])

        def _get(self, path):
            if path == "/21":
                raise TimeoutError("injected")
            return 200, {"status": "ACTIVE", "ad_content": {"employer": {"orgnr": "wrong"}}}, b"{}"

    record, _ = collect_nav_jobs(profile(), Feed())
    assert record["value"]["detail_failures"] == 1
    assert record["value"]["collection_complete"] is False
    assert not any("failed" in r["reason"] for r in record["value"]["rejected_candidates"])

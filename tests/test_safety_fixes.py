from __future__ import annotations

from norway_company_agent.batch import terminal_envelope, validate_envelopes, AVAILABILITY_STATES
from norway_company_agent.evidence import evidence
from norway_company_agent.identity import assess_website_identity, competing_legal_names
from norway_company_agent.website_discovery import page_identity_signals


def _profile(name: str, title: str, text: str = "") -> dict:
    return {
        "organisation_number": "999999999",
        "name": name,
        "evidence": {
            "website": {
                "source_url": "https://consto.no/",
                "value": {"final_url": "https://consto.no/", "title": title, "main_text_excerpt": text or ("Vi leverer tjenester. " * 10)},
            }
        },
    }


def test_competing_legal_name_detected():
    assert competing_legal_names("CONSTO AS", ["Consto Bergen AS"]) == ["Consto Bergen AS"]
    assert competing_legal_names("CONSTO AS", ["Velkommen til Consto AS"]) == []
    assert competing_legal_names("NORDIC FISH AS", ["Nordic Fish AS, org.nr 999 999 999"]) == []


def test_sister_entity_title_is_not_exact_company():
    result = assess_website_identity(_profile("CONSTO AS", "Consto Bergen AS"))
    assert result["status"] != "exact"
    assert result["publishable"] is False
    assert result["competing_legal_names"] == ["Consto Bergen AS"]


def test_exact_legal_name_still_accepted():
    result = assess_website_identity(_profile("CONSTO AS", "Consto AS"))
    assert result["status"] == "exact"


def test_address_proof_rejected_when_sister_entity_named():
    from norway_company_agent.website_discovery import assess_org_proof

    hints = {"organisation_number": "999999999", "legal_name": "CONSTO AS", "legal_name_words": ["consto", "as"],
             "postcode": "5003", "street_words": ["strandgaten"]}
    html = "<html><title>Consto Bergen AS</title><body>Consto AS og Consto Bergen AS, Strandgaten 1, 5003 Bergen</body></html>"
    signals = page_identity_signals(html, hints)["signals"]
    assert signals["competing_legal_names"] == ["Consto Bergen AS"]
    proof = assess_org_proof([{"url": "https://consto.no/", "identity_signals": signals}])
    assert proof.get("publishable") is not True


def test_envelope_has_contract_fields_and_six_states():
    profile = {
        "organisation_number": "999999999",
        "name": "X AS",
        "evidence": {
            "registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/x", value={"a": 1}),
            "website": evidence("website", "source_error", "registry_linked_company_website", "https://data.brreg.no/x", note="boom"),
            "group": evidence("group", "not_found", "official_group_structure", "https://data.brreg.no/x"),
        },
    }
    envelope = terminal_envelope(profile, run_id="r", modules=["registry", "website", "group", "roles"], started_at="s", completed_at="c")
    for key in ("run", "claims", "evidence", "errors", "operations"):
        assert key in envelope
    states = {m: s["availability"] for m, s in envelope["modules"].items()}
    assert states == {"registry": "available", "website": "failed", "group": "not_available", "roles": "failed"}
    assert all(s in AVAILABILITY_STATES for s in states.values())
    assert {e["field"] for e in envelope["errors"]} >= {"website", "roles"}
    checks = validate_envelopes([envelope], 1)["checks"]
    assert checks["contract_fields_present"] and checks["all_availability_states_valid"]

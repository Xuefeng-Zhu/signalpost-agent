"""Offline regressions for the review's identity and registry-source findings."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import tldextract

from norway_company_agent.identity import competing_legal_names
from norway_company_agent.official import normalize_entity
from norway_company_agent.registry_state import registry_boolean, registry_field
from norway_company_agent.website_discovery import resolve_official_website

ORG = "985589003"


def owner():
    return {"organisation_number": ORG, "name": "CONSTO AS", "legal_form": "AS",
            "website": "https://bergenselskap.no/", "evidence": {
                "registry": {"status": "available", "source_url": "https://example.no/bulk",
                             "value": {"organisasjonsnummer": ORG, "navn": "CONSTO AS", "organisasjonsform.kode": "AS",
                                       "konkurs": "false", "underAvvikling": "false"}},
                "registry_live": {"status": "available", "source_url": "https://example.no/live",
                                  "value": {"organisation_number": ORG, "name": "NEW NAME ASA", "legal_form": "ASA",
                                            "bankrupt": True, "liquidating": True}}}}


@pytest.mark.parametrize("title", ["Consto Bergen", "Consto", "Velkommen"])
@pytest.mark.parametrize("proof", [False, True])
@pytest.mark.parametrize("identity_html", [
    "<h1>Bergen Consto AS</h1>",
    "<footer>Bergen Consto AS</footer>",
    "<footer><p><span>Bergen</span> <span>Consto AS</span></p><nav>Home About</nav></footer>",
    '<span itemprop="legalName">Bergen Consto AS</span>',
    '<script type="application/ld+json">{"@type":"Organization","legalName":"Bergen Consto AS"}</script>',
])
def test_real_html_fetch_never_publishes_related_entity_without_org_proof(title, proof, identity_html):
    p = owner()
    raw = (f"<html><head><title>{title}</title></head><body>{identity_html}<p>"
           + "Vi leverer lokale entreprenørtjenester av høy kvalitet. " * 10
           + f"</p><footer>{'Org.nr ' + ORG if proof else ''}</footer></body></html>").encode()
    class Response:
        status = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n): return raw[:n]
        def geturl(self): return p["website"]
    # No DNS, HTTP, robots requests or PSL downloads are permitted in this test.
    with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website._robots_allowed", return_value=True), patch("norway_company_agent.website.SAFE_OPENER.open", return_value=Response()), patch("tldextract.extract", tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)):
        result, _ = resolve_official_website(p, discovery=False)
    assert result["value"]["identity_assessment"]["publishable"] is proof
    assert bool(result.get("official_website")) is proof


def test_title_corroborates_prefix_without_requiring_repetition():
    assert competing_legal_names("CONSTO AS", ["Consto Bergen"], ["Kontakt Bergen Consto AS"]) == ["Bergen Consto AS"]


@pytest.mark.parametrize("field", ["name", "legal_form", "bankrupt", "liquidating"])
def test_available_entity_matched_live_fields_win(field):
    p = owner()
    selected = registry_field(p, field)
    assert selected["value"] == p["evidence"]["registry_live"]["value"][field]
    assert selected["module"] == "registry_live"
    assert not selected["fallback"]


@pytest.mark.parametrize("change", ["wrong_org", "missing_org", "unavailable", "missing_field"])
def test_live_requires_org_status_and_present_field(change):
    p = owner()
    live = p["evidence"]["registry_live"]
    if change == "wrong_org": live["value"]["organisation_number"] = "888567232"
    if change == "missing_org": live["value"].pop("organisation_number")
    if change == "unavailable": live["status"] = "source_error"
    if change == "missing_field": live["value"].pop("bankrupt")
    result = registry_field(p, "bankrupt")
    assert result["module"] == "registry"
    assert result["fallback"] and "snapshot" in result["note"]
    assert result["value"] == "false"


def test_missing_flags_are_unknown_not_false_and_live_false_is_preserved():
    p = owner()
    assert registry_field(p, "forced_dissolution")["value"] is None
    assert registry_boolean(None) is None
    assert registry_boolean("no") is None
    p["evidence"]["registry_live"]["value"]["bankrupt"] = False
    assert registry_field(p, "bankrupt")["value"] is False
    assert registry_field(p, "bankrupt")["module"] == "registry_live"
    assert normalize_entity({"underTvangsavviklingEllerTvangsopplosning": True})["forced_dissolution"] is True


def test_site_uses_live_names_forms_flags_and_labels_unknowns():
    spec = importlib.util.spec_from_file_location("build_site_registry_test", Path(__file__).resolve().parents[1] / "scripts/build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = owner()
    p["synthesis"] = {"statements": [], "unknowns": []}
    slim = module.slim(p, "test")
    assert slim["name"] == "NEW NAME ASA"
    assert slim["form"] == "ASA"
    facts = {item["l"]: item for item in slim["facts"]}
    assert facts["Legal name"]["src"]["url"].endswith("/live")
    flags = facts["Register status flags"]
    assert "not bankrupt" not in flags["v"]
    assert "bankrupt; in liquidation" in flags["v"]
    assert "forced dissolution: unknown" in flags["v"]
    assert flags["src"]["fields"][0]["url"].endswith("/live")


@pytest.mark.parametrize("name,text", [
    ("EGIL ULVAN REDERI AS", "HELE SAKEN Egil Ulvan Rederi AS Fjordgata 50"),
    ("STAVRUM BYGG AS", "Sec-CH-UA-Model Stavrum Bygg AS"),
    ("WILCOM AS", "Renholdsavdeling Wilcom AS"),
])
def test_menu_prefix_does_not_overrule_real_homepage(name, text):
    from norway_company_agent.identity import assess_website_identity
    p = owner()
    p["name"] = name
    p["evidence"]["website"] = {"status": "available", "value": {
        "title": name, "main_text_excerpt": text + " Vi leverer tjenester til bedrifter." * 8,
        "identity_text_excerpt": text}}
    assert assess_website_identity(p)["publishable"]


def test_mismatched_or_unavailable_bulk_cannot_establish_negative_status():
    p = owner()
    p["evidence"]["registry_live"]["value"].pop("bankrupt")
    p["evidence"]["registry"]["value"]["organisasjonsnummer"] = "888567232"
    assert registry_field(p, "bankrupt")["value"] is None
    p["evidence"]["registry"]["value"]["organisasjonsnummer"] = ORG
    p["evidence"]["registry"]["status"] = "source_error"
    assert registry_field(p, "bankrupt")["value"] is None


@pytest.mark.parametrize("name,prefix", [("EGIL ULVAN REDERI AS", "HELE SAKEN"), ("WILCOM AS", "Renholdsavdeling")])
def test_repeated_body_menu_prefix_remains_noise(name, prefix):
    assert not competing_legal_names(name, [name], [(prefix + " " + name + " ") * 5])


def test_html_identity_statements_keep_footer_navigation_separate():
    from bs4 import BeautifulSoup
    from norway_company_agent.identity import html_identity_statements
    from norway_company_agent.website_discovery import identity_hints, page_identity_signals
    p = owner()
    html = '<title>Consto AS</title><h1>Consto AS</h1><nav>Bergen</nav><footer><nav>Bergen</nav><p>Consto AS</p><ul><li>Bergen</li></ul></footer>'
    statements = html_identity_statements(BeautifulSoup(html, "lxml"))
    assert statements == ["Consto AS"]
    assert not page_identity_signals(html, identity_hints(p))["signals"]["competing_legal_names"]


def test_site_rebuilds_old_cached_synthesis_and_does_not_reassert_historical_changes():
    spec = importlib.util.spec_from_file_location("build_site_old_cache_test", Path(__file__).resolve().parents[1] / "scripts/build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = owner()
    old = {"method": "evidence_template_synthesis_v2", "headline": "CONSTO AS is not bankrupt.",
           "statements": [{"id": "old", "text": "CONSTO AS is not bankrupt.", "citations": ["S1"]}],
           "sections": [{"title": "Identity", "key": "what_it_does", "statement_ids": ["old"]}],
           "llm_polish": {"status": "accepted", "narrative": "CONSTO AS is not bankrupt. [S1]"}}
    p["synthesis"] = old
    envelope = {"synthesis": old, "refresh": {"company_in_previous_snapshot": True},
                "changes": [{"field": "site_signals.news_posts", "old_value": ["old article"], "new_value": [], "material": True}]}
    result = module.slim(p, "test", envelope)
    import json
    serialized = json.dumps(result["syn"])
    assert "CONSTO AS is not bankrupt" not in serialized
    assert "NEW NAME ASA" in serialized
    assert result["syn"]["narrative"] is None
    assert result["syn"]["changed"]["status"] == "historical_changes_not_revalidated"
    assert result["changes"] == []


def test_site_preserves_valid_current_batch_changes():
    from norway_company_agent.synthesis import build_synthesis
    spec = importlib.util.spec_from_file_location("build_site_current_test", Path(__file__).resolve().parents[1] / "scripts/build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = owner()
    changes = [{"field": "registry.name", "old_value": "OLD NAME AS", "new_value": "NEW NAME ASA", "material": True, "source_module": "registry_live"}]
    refresh = {"previous_snapshot_supplied": True, "company_in_previous_snapshot": True}
    current = build_synthesis(p, changes=changes, refresh=refresh)
    assert current["validation"]["passed"]
    result = module.slim(p, "test", {"synthesis": current, "changes": changes, "refresh": refresh})
    assert result["syn"]["changed"]["count"] == 1
    assert len(result["changes"]) == 1
    assert any("OLD NAME AS" in statement["x"] for section in result["syn"]["sections"] for statement in section["st"])

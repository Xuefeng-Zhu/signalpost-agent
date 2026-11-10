"""Identity contradictions must survive discovery and the final publication gate."""
from __future__ import annotations

import pytest

from norway_company_agent.identity import assess_website_identity, competing_legal_names
from norway_company_agent.website_discovery import assess_org_proof, identity_hints, page_identity_signals


ORG = "985589003"
OTHER = "888567232"
VARIANTS = ("Consto Bergen as", "Consto Bergen A/S", "Bergen Consto AS", "CONSTO ASA", "bergen consto as")


def profile(name="CONSTO AS", **value):
    return {
        "organisation_number": ORG,
        "name": name,
        "evidence": {
            "registry": {"value": {"forretningsadresse.adresse": "Strandgaten 1", "forretningsadresse.postnummer": "5003"}},
            "website": {"status": "available", "value": {"title": name, "main_text_excerpt": "Vi leverer tjenester. " * 10, **value}},
        },
    }


def page(owner, html, url="https://example.no/"):
    return {"url": url, "identity_signals": page_identity_signals(html, identity_hints(owner))["signals"]}


@pytest.mark.parametrize("with_address", [False, True])
def test_wrong_org_number_is_a_conflict_despite_exact_name(with_address):
    owner = profile("Alpha AS")
    address = "Strandgaten 1, 5003" if with_address else ""
    pages = [page(owner, f"<title>Alpha AS</title><footer>Alpha AS Org.nr {OTHER} {address}</footer>")]
    proof = assess_org_proof(pages)
    assert proof["status"] == "conflict"
    assert not proof["publishable"]
    owner["evidence"]["website"]["value"]["pages"] = pages
    result = assess_website_identity(owner)
    assert not result["publishable"]
    assert OTHER in " ".join(result["reasons"])


def test_conflicting_number_in_legacy_excerpt_is_not_ignored():
    owner = profile("Alpha AS", identity_text_excerpt=f"Alpha AS Org.nr {OTHER}")
    assert not assess_website_identity(owner)["publishable"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_competing_legal_name_variants(variant):
    assert competing_legal_names("CONSTO AS", [variant]) == [variant]
    assert not assess_website_identity(profile(title=variant))["publishable"]


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("separate_page", [False, True])
def test_shared_address_cannot_override_competing_legal_name(variant, separate_page):
    owner = profile()
    address = "CONSTO AS, Strandgaten 1, 5003 Bergen"
    pages = [page(owner, f"<title>{variant}</title><body>{'' if separate_page else address}</body>")]
    if separate_page:
        pages.append(page(owner, f"<body>{address}</body>", "https://example.no/contact"))
    owner["evidence"]["website"]["value"]["pages"] = pages
    assert assess_org_proof(pages)["status"] == "conflict"
    assert not assess_website_identity(owner)["publishable"]


@pytest.mark.parametrize("title", ["Consto AS", "consto as", "Consto A/S", "Velkommen til Consto AS"])
def test_ordinary_exact_names_are_preserved(title):
    assert not competing_legal_names("CONSTO AS", [title])
    assert assess_website_identity(profile(title=title))["publishable"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_exact_org_proof_remains_stronger_than_related_names(variant):
    owner = profile(title=variant)
    owner["evidence"]["website"]["value"]["pages"] = [page(owner, f"<title>{variant}</title><footer>Org.nr {ORG}</footer>")]
    assert assess_website_identity(owner)["publishable"]


def test_registered_address_proof_without_contradictions_remains_exact():
    owner = profile()
    proof = assess_org_proof([page(owner, "<body>CONSTO AS, Strandgaten 1, 5003 Bergen</body>")])
    assert proof["publishable"]
    assert proof["proof"] == "legal_name_and_registered_address"

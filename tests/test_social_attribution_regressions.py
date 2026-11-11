"""Structured social links must identify the researched company, not a guest."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent import site_signals as ss

PROFILE = {"organisation_number": "916340257", "name": "EKSEMPEL RØR AS"}
SITE = "https://eksempelror.no/"
SOCIAL = "https://linkedin.com/company/123456"


def page(node, links="", url=SITE):
    return ss._parse_page({"raw": ('<script type="application/ld+json">' + json.dumps(node) + '</script>' + links).encode(),
                           "final_url": url, "retrieved_at": "t", "content_sha256": "h"})


def extract(node):
    return ss.extract_social([page(node)], ss.brand_tokens(PROFILE, SITE))


def org(**kwargs):
    return {"@type": "Organization", "sameAs": [SOCIAL], **kwargs}


def test_external_article_author_remains_candidate():
    published, candidates = extract({"@type": "Article", "author": org(name="External Agency AS")})
    assert not published
    assert [item["url"] for item in candidates] == [SOCIAL]


def test_identity_bound_author_is_accepted():
    published, _ = extract({"@type": "Article", "author": org(name="Eksempel Rør AS")})
    assert published[0]["confidence"] == 0.97


def test_graph_selects_target_not_other_organizations():
    published, candidates = extract({"@graph": [
        {"@type": "WebSite", "publisher": {"@id": SITE + "#organization"}},
        org(name="Eksempel Rør AS", **{"@id": SITE + "#organization"}),
        org(name="External Agency AS", sameAs=["https://instagram.com/agency"]) ]})
    assert [item["url"] for item in published] == [SOCIAL]
    assert [item["url"] for item in candidates] == ["https://instagram.com/agency"]


def test_verified_homepage_supports_unnamed_and_brand_nodes():
    for identity in ({"url": SITE}, {"name": "Eksempelror", "url": "https://www.eksempelror.no/"}):
        published, _ = extract(org(**identity))
        assert published[0]["url"] == SOCIAL


def test_page_id_or_same_domain_path_does_not_bind_identity():
    for identity in ({"@id": SITE + "#organization"}, {"url": SITE + "partners/vendor"},
                     {"name": "External Agency AS", "url": SITE}, {"url": "https://eksempelror.no.evil.com/"}):
        published, candidates = extract(org(**identity))
        assert not published
        assert candidates


def test_exact_org_number_and_contradictory_number():
    published, _ = extract(org(name="Trading brand", vatID="NO 916 340 257 MVA"))
    assert published
    published, candidates = extract(org(name=PROFILE["name"], taxID="999999999"))
    assert not published
    assert candidates


def test_child_identifier_does_not_identify_parent():
    published, candidates = extract(org(name="Agency", department={"taxID": "916340257"}))
    assert not published
    assert candidates


def test_legal_name_conflict_is_not_rescued_by_brand():
    published, candidates = extract(org(name="Eksempelror", legalName="Other AS", url=SITE))
    assert not published
    assert candidates


def test_unbound_declaration_cannot_use_handle_fallback_on_other_page():
    url = "https://linkedin.com/company/eksempelror-partner"
    pages = [page({}, '<a href="' + url + '">Partner</a>'),
             page(org(name="Other AS", sameAs=[url]), url=SITE + "news/")]
    published, candidates = ss.extract_social(pages, ss.brand_tokens(PROFILE, SITE))
    assert not published
    assert [item["url"] for item in candidates] == [url]


def test_plain_brand_link_still_published():
    published, _ = ss.extract_social([page({}, '<a href="https://instagram.com/eksempelror">Follow</a>')],
                                     ss.brand_tokens(PROFILE, SITE))
    assert published[0]["match"] == "handle_matches_brand_or_domain"


def test_malformed_homepage_is_only_a_candidate():
    for url in ("https://[invalid", "https://eksempelror.no:bad/"):
        published, candidates = extract(org(url=url))
        assert not published
        assert candidates


def test_handle_substrings_and_partial_names_are_not_identity():
    tokens = ss.brand_tokens({"name": "ACME AS"}, "https://acme.no/")
    for handle in ("notacme", "acme-partner", "acmeholdings", "acmeasa"):
        published, candidates = ss.extract_social([
            page({}, f'<a href="https://linkedin.com/company/{handle}">Partner</a>')], tokens)
        assert not published
        assert candidates
    multi = ss.brand_tokens({"name": "ALPHA PLUMBING AS"}, "https://alphaplumbing.no/")
    assert not ss.handle_matches("alpha", multi)
    assert not ss.handle_matches("alphaplumbingcompetitor", multi)


def test_whole_brand_handle_conventions_remain_valid():
    tokens = ss.brand_tokens({"name": "ACME AS"}, "https://acme.no/")
    for handle in ("acme", "acme-as", "acmeofficial", "Acme-As-100064576124376"):
        assert ss.handle_matches(handle, tokens)


def test_structured_legal_forms_are_not_interchangeable():
    tokens = ss.brand_tokens({"name": "ALPHA AS"}, "https://alpha.no/")
    for name in ("Alpha ASA", "Alpha ANS", "Alpha NUF", "Alpha"):
        published, candidates = ss.extract_social([page(org(name=name))], tokens)
        assert not published
        assert candidates
    published, _ = ss.extract_social([page(org(name="Alpha AS"))], tokens)
    assert published
    published, _ = ss.extract_social([page(org(name="Alpha", url="https://alpha.no/"))], tokens)
    assert published

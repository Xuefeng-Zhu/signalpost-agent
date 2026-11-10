from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import website_discovery as wd  # noqa: E402
from norway_company_agent.batch import evidence_terminal_state, validate_envelopes, terminal_envelope  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_website_identity  # noqa: E402

ORG = "916340257"  # valid mod-11 number
OTHER = "920990029"


def registry_profile(name="WYSSEN NORGE AS", org=ORG, email="", street="Trolladalen 30", postcode="6856"):
    return {
        "organisation_number": org,
        "name": name,
        "website": "",
        "evidence": {"registry": {"value": {"epostadresse": email, "forretningsadresse.adresse": street, "forretningsadresse.postnummer": postcode}}},
    }


def page(url, body, hints):
    return {"url": url, "content_sha256": "x", "retrieved_at": "t", "identity_signals": wd.page_identity_signals(body, hints)["signals"]}


class OrgNumberDetectionTests(unittest.TestCase):
    def test_checksum(self):
        self.assertTrue(wd.valid_org_number(ORG))
        self.assertFalse(wd.valid_org_number("916340258"))

    def test_common_norwegian_formats_are_found(self):
        for text in (f"Org.nr: {ORG}", "Org.nr. 916 340 257", "NO 916 340 257 MVA", "NO916340257MVA", "org nr 916.340.257", "Foretaksregisteret 916\u00a0340\u00a0257"):
            self.assertEqual([item["digits"] for item in wd.find_org_numbers(text)], [ORG], text)

    def test_number_inside_longer_digit_run_is_not_a_match(self):
        self.assertEqual(wd.find_org_numbers("Tlf 22 91 63 40 25 71 Konto 1916340257"), [])

    def test_identity_gate_no_longer_matches_org_digits_as_substring(self):
        profile = {"organisation_number": ORG, "name": "Something Else AS", "evidence": {"website": {"status": "available", "value": {"title": "Shop", "main_text_excerpt": "Ring 9163402571 for tilbud"}}}}
        self.assertFalse(assess_website_identity(profile)["publishable"])


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.hints = wd.identity_hints(registry_profile())

    def test_org_number_in_homepage_footer_is_exact(self):
        pages = [page("https://wyssen.no/", f"<html><title>Wyssen</title><body><main>Snøskred</main><footer>Wyssen Norge AS · Org.nr {ORG}</footer></body></html>", self.hints)]
        proof = wd.assess_org_proof(pages)
        self.assertTrue(proof["publishable"])
        self.assertEqual(proof["where"], "homepage")
        self.assertIn("916340257", proof["claim_span"])

    def test_org_number_on_contact_page_is_exact_without_conflicts(self):
        pages = [
            page("https://wyssen.no/", "<html><title>Wyssen Norge</title><body>Velkommen</body></html>", self.hints),
            page("https://wyssen.no/kontakt", f"<html><body>Kontakt oss. Organisasjonsnummer: 916 340 257</body></html>", self.hints),
        ]
        proof = wd.assess_org_proof(pages)
        self.assertTrue(proof["publishable"])
        self.assertEqual(proof["where"], "identity_subpage")

    def test_group_site_listing_subsidiary_number_on_contact_page_is_not_published(self):
        pages = [
            page("https://group.no/", "<html><title>Konsern - Group</title><body>Our group</body></html>", self.hints),
            page("https://group.no/kontakt", f"<html><body>Wyssen Norge AS org.nr {ORG}</body></html>", self.hints),
        ]
        proof = wd.assess_org_proof(pages)
        self.assertFalse(proof["publishable"])
        self.assertEqual(proof["status"], "conflict")

    def test_site_with_competing_org_numbers_is_ambiguous(self):
        pages = [page("https://parent.no/", f"<html><body>Våre selskaper: Wyssen Norge AS {ORG}, Parent AS {OTHER}</body></html>", self.hints)]
        proof = wd.assess_org_proof(pages)
        self.assertFalse(proof["publishable"])
        self.assertIn(OTHER, " ".join(proof["evidence_against"]))

    def test_legal_name_plus_registered_address_is_secondary_proof(self):
        pages = [page("https://wyssen.no/", "<html><body>Wyssen Norge AS, Trolladalen 30, 6856 Sogndal</body></html>", self.hints)]
        proof = wd.assess_org_proof(pages)
        self.assertTrue(proof["publishable"])
        self.assertEqual(proof["proof"], "legal_name_and_registered_address")

    def test_name_without_org_or_address_is_not_published(self):
        pages = [page("https://wyssen.no/", "<html><title>Wyssen Norge AS</title><body>Wyssen Norge AS leverer snøskredsikring.</body></html>", self.hints)]
        self.assertFalse(wd.assess_org_proof(pages)["publishable"])

    def test_parked_page_is_not_published(self):
        pages = [page("https://wyssen.no/", "<html><body>Wyssen Norge AS Trolladalen 30 6856 - This domain is for sale</body></html>", self.hints)]
        self.assertFalse(wd.assess_org_proof(pages)["publishable"])


class CandidateTests(unittest.TestCase):
    def test_domain_guesses_strip_legal_form_and_fold_norwegian_letters(self):
        guesses = wd.guess_domains("ØVRE VIKDALSELVA KRAFTVERK AS")
        self.assertEqual(guesses[0], "ovrevikdalselvakraftverk.no")
        self.assertIn("ovre-vikdalselva-kraftverk.no", guesses)
        self.assertIn("oevrevikdalselvakraftverk.no", guesses)
        self.assertTrue(all(not g.endswith("as.no") for g in guesses))
        self.assertIn("wyssen.no", wd.guess_domains("WYSSEN NORGE AS"))
        self.assertEqual(wd.guess_domains("AS MARKVEIEN 6")[0], "markveien6.no")

    def test_email_domain_skips_free_mail_and_isps(self):
        self.assertEqual(wd.email_domain("post@arkjv.no"), "arkjv.no")
        self.assertEqual(wd.email_domain("x@mail.firma.no"), "firma.no")
        for value in ("a@gmail.com", "b@online.no", "c@lyse.net", "", None):
            self.assertIsNone(wd.email_domain(value))

    def test_candidates_put_email_lead_first_and_skip_directories(self):
        profile = registry_profile(email="post@proff.no")
        self.assertFalse(any(c["domain"] == "proff.no" for c in wd.build_candidates(profile)))
        profile = registry_profile(email="post@wyssen-norge.no")
        first = wd.build_candidates(profile)[0]
        self.assertEqual(first, {"domain": "wyssen-norge.no", "lead_source": "brreg_email_domain"})


def fake_site(pages):
    def _get(url, budget, timeout, max_bytes):
        budget.take()
        if url in pages:
            final, body = pages[url] if isinstance(pages[url], tuple) else (url, pages[url])
            return {"ok": True, "status": 200, "requested_url": url, "final_url": final, "redirect_chain": [url], "raw": body.encode(), "retrieved_at": "t"}
        return {"ok": False, "status": 404, "requested_url": url, "error": "HTTP 404"}
    return _get


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.hints = wd.identity_hints(registry_profile())

    def test_probe_follows_contact_link_and_verifies(self):
        site = fake_site({
            "https://wyssen.no/": '<html><title>Wyssen Norge</title><body><a href="/kontakt">Kontakt</a></body></html>',
            "https://wyssen.no/kontakt": f"<html><body>Org nr {ORG}</body></html>",
        })
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            outcome = wd.probe_site("wyssen.no", self.hints, wd._Budget(10, 30))
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(outcome["proof"]["source_url"], "https://wyssen.no/kontakt")

    def test_redirect_to_directory_is_rejected(self):
        site = fake_site({"https://wyssen.no/": ("https://www.proff.no/selskap/wyssen", f"<html><body>{ORG}</body></html>")})
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            outcome = wd.probe_site("wyssen.no", self.hints, wd._Budget(10, 30))
        self.assertEqual(outcome["status"], "rejected_host")

    def test_request_cap_is_enforced(self):
        site = fake_site({"https://wyssen.no/": '<html><title>Wyssen</title><body>' + "".join(f'<a href="/kontakt{i}">Kontakt</a>' for i in range(10)) + "</body></html>"})
        budget = wd._Budget(3, 30)
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            wd.probe_site("wyssen.no", self.hints, budget)
        self.assertLessEqual(budget.requests, 3)

    def test_discovery_abstains_when_nothing_resolves(self):
        with patch.object(wd, "_resolves", lambda host: False):
            found = wd.discover_official_website(registry_profile())
        self.assertEqual(found["status"], "not_found")
        self.assertEqual(found["metrics"]["requests"], 0)
        self.assertTrue(all(c["outcome"] == "dns_not_resolved" for c in found["candidates"]))


class GateAndEnvelopeTests(unittest.TestCase):
    def test_unverified_fetched_site_becomes_ambiguous_not_available(self):
        website = evidence("website", "available", "registry_linked_company_website", "https://parent.test", value={"title": "Parent Group", "main_text_excerpt": "Parent"})
        gated = apply_website_identity_gate({"organisation_number": ORG, "name": "Exact Subsidiary AS"}, website)["website"]
        self.assertEqual(gated["status"], "ambiguous")
        self.assertEqual(evidence_terminal_state(gated), "ambiguous")

    def test_ambiguous_is_a_valid_terminal_state(self):
        profile = {"organisation_number": ORG, "evidence": {"website": {"status": "ambiguous"}}}
        envelope = terminal_envelope(profile, run_id="r", modules=["website"], started_at="s", completed_at="c")
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])

    def test_official_claim_carries_proof_source_and_retrieval_time(self):
        hints = wd.identity_hints(registry_profile())
        home = page("https://wyssen.no/", f"<html><body>Org.nr {ORG}</body></html>", hints)
        record = evidence("website", "available", "discovered_company_website", "https://wyssen.no/", value={"final_url": "https://wyssen.no/", "title": "Wyssen", "pages": [home]})
        gated = apply_website_identity_gate(registry_profile(), record)["website"]
        claim = wd._official_claim(gated)
        self.assertEqual(claim["value"], "https://wyssen.no/")
        self.assertEqual(claim["evidence"]["source_url"], "https://wyssen.no/")
        self.assertEqual(claim["evidence"]["retrieved_at"], "t")
        self.assertIn(ORG, claim["evidence"]["claim_span"])


if __name__ == "__main__":
    unittest.main()

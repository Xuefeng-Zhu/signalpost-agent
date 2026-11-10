"""Recall levers added in revision 2: extra website leads, wider identity pages, JS-render fallback,
Nav cache robustness. Every test also checks that the identity gate is not weakened."""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import js_render  # noqa: E402
from norway_company_agent import website_discovery as wd  # noqa: E402
from norway_company_agent.nav_jobs import NavJobsFeed  # noqa: E402
from norway_company_agent.official import normalize_locations  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402

ORG = "916340257"
OTHER = "920990029"


def profile(name="ASVO MÅLØY AS", email="", nav=None, web_leads=None, street="Gate 1", postcode="6700"):
    evidence = {"registry": {"value": {"epostadresse": email, "forretningsadresse.adresse": street, "forretningsadresse.postnummer": postcode}}}
    if nav is not None:
        evidence["nav_jobs"] = {"status": "available", "value": {"postings": nav}}
    if web_leads is not None:
        evidence["locations"] = {"status": "available", "value": {"locations": [], "web_leads": web_leads}}
    return {"organisation_number": ORG, "name": name, "website": "", "evidence": evidence}


def fake_site(pages):
    def _get(url, budget, timeout, max_bytes):
        budget.take()
        if url in pages:
            return {"ok": True, "status": 200, "requested_url": url, "final_url": url, "redirect_chain": [url], "raw": pages[url].encode(), "retrieved_at": "t"}
        return {"ok": False, "status": 404, "requested_url": url, "error": "HTTP 404"}
    return _get


class LeadTests(unittest.TestCase):
    def test_brand_guesses_use_leading_and_trailing_name_runs(self):
        self.assertIn("jeistad.no", wd.extended_guess_domains("JEISTAD ENTREPRENØR AS"))
        self.assertIn("sigurdlima.no", wd.extended_guess_domains("RØRLEGGERFIRMA SIGURD LIMA AS"))
        self.assertIn("rimestad.no", wd.extended_guess_domains("BYGGEFIRMA RIMESTAD AS"))

    def test_brand_guesses_never_emit_a_bare_trade_word(self):
        guesses = wd.extended_guess_domains("BYGGEFIRMA RIMESTAD AS") + wd.extended_guess_domains("SUM YEE RESTAURANT &BAR AS")
        for generic in ("byggefirma.no", "restaurant.no", "bar.no", "sum.no"):
            self.assertNotIn(generic, guesses)

    def test_nav_homepage_subunit_and_email_variant_are_leads_after_registry_email(self):
        p = profile(
            email="post@asvo.com",
            nav=[{"employer_homepage": "https://asvomaloy.no", "employer_orgnr": ORG}],
            web_leads=[{"kind": "subunit_hjemmeside", "value": "www.asvo-bakeri.no"}, {"kind": "subunit_email", "value": "x@gmail.com"}],
        )
        candidates = wd.build_candidates(p)
        sources = [(c["domain"], c["lead_source"]) for c in candidates]
        self.assertEqual(sources[0], ("asvo.com", "brreg_email_domain"))
        self.assertIn(("asvomaloy.no", "nav_employer_homepage"), sources)
        self.assertIn(("asvo-bakeri.no", "brreg_subunit_hjemmeside"), sources)
        self.assertIn(("asvo.no", "brreg_email_domain_variant"), sources)
        self.assertFalse(any(d == "gmail.com" for d, _ in sources))

    def test_subunit_web_leads_do_not_change_the_locations_refresh_diff(self):
        body = {"_embedded": {"underenheter": [{"organisasjonsnummer": "999999999", "navn": "AVD", "hjemmeside": "www.x.no"}]}}
        value = normalize_locations(body)
        self.assertEqual(value["web_leads"][0]["value"], "www.x.no")
        self.assertNotIn("hjemmeside", value["locations"][0])
        old = {"organisation_number": ORG, "evidence": {"locations": {"value": {"locations": value["locations"]}}}}
        new = {"organisation_number": ORG, "evidence": {"locations": {"value": value}}}
        self.assertFalse([c for c in diff_profile(old, new) if c["field"].startswith("locations")])


class ProbeTests(unittest.TestCase):
    def test_name_inside_joined_host_label_unlocks_identity_subpages(self):
        hints = wd.identity_hints(profile())
        site = fake_site({
            "https://asvomaloy.no/": "<html><title>Eit springbrett for arbeid</title><body>Velkommen</body></html>",
            "https://asvomaloy.no/kontakt": f"<html><body>ASVO Måløy AS, org.nr. {ORG}</body></html>",
        })
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(10, 30))
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(outcome["proof"]["source_url"], "https://asvomaloy.no/kontakt")

    def test_brand_guess_with_subpage_only_proof_is_not_published(self):
        p = profile(name="INSBO SIRNES FARGE & INTERIØR AS")
        site = fake_site({
            "https://insbo.no/": '<html><title>Insbo</title><body><a href="/kontakt/insbo-sirnes">Kontakt</a></body></html>',
            "https://insbo.no/kontakt/insbo-sirnes": f"<html><body>Insbo Sirnes Org.nr.: {ORG}MVA</body></html>",
        })
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: host in {"insbo.no", "www.insbo.no"}):
            found = wd.discover_official_website(p)
        self.assertNotEqual(found["status"], "verified")
        insbo = next(c for c in found["candidates"] if c["domain"] == "insbo.no")
        self.assertEqual(insbo["lead_source"], "legal_name_brand_guess")
        self.assertEqual(insbo["outcome"], "ambiguous")

    def test_fallback_paths_pad_a_site_with_unhelpful_links(self):
        hints = wd.identity_hints(profile())
        site = fake_site({
            "https://asvomaloy.no/": '<html><title>ASVO</title><body><a href="/om-oss">Om oss</a></body></html>',
            "https://asvomaloy.no/om-oss": "<html><body>Om oss</body></html>",
            "https://asvomaloy.no/kontakt-oss": f"<html><body>Org nr {ORG}</body></html>",
        })
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(12, 30))
        self.assertEqual(outcome["status"], "verified")

    def test_unrelated_brand_guess_host_gets_no_subpage_requests(self):
        hints = wd.identity_hints(profile(name="JEISTAD ENTREPRENØR AS"))
        site = fake_site({"https://example-bygg.no/": "<html><title>Noe annet</title><body>Hei</body></html>"})
        budget = wd._Budget(12, 30)
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True):
            outcome = wd.probe_site("example-bygg.no", hints, budget, require_name_for_subpages=True)
        self.assertEqual(outcome["status"], "unverified")
        self.assertEqual(budget.requests, 2)  # robots + homepage only

    def test_js_shell_is_rendered_and_gated_like_any_page(self):
        hints = wd.identity_hints(profile())
        shell = '<html><title>ASVO Måløy</title><head><script src="/a.js"></script></head><body><div id="root"></div></body></html>'
        site = fake_site({"https://asvomaloy.no/": shell})
        rendered = {"final_url": "https://asvomaloy.no/", "html": f"<html><body>ASVO Måløy AS · Org.nr {ORG}</body></html>", "links": [], "elapsed_ms": 5}
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True), \
                patch.object(js_render, "available", lambda: True), patch.object(js_render, "render", lambda url, **kw: rendered):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(20, 60))
        self.assertEqual(outcome["status"], "verified")
        self.assertTrue(any(page.get("rendered") for page in outcome["pages"]))

    def test_rendered_page_with_a_competing_org_number_stays_unpublished(self):
        hints = wd.identity_hints(profile())
        shell = '<html><title>ASVO Måløy</title><script src="/a.js"></script><body><div id="root"></div></body></html>'
        site = fake_site({"https://asvomaloy.no/": shell})
        rendered = {"final_url": "https://asvomaloy.no/", "html": f"<html><body>Org.nr {ORG} og {OTHER}</body></html>", "links": [], "elapsed_ms": 5}
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True), \
                patch.object(js_render, "available", lambda: True), patch.object(js_render, "render", lambda url, **kw: rendered):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(20, 60))
        self.assertEqual(outcome["status"], "ambiguous")

    def test_render_to_another_domain_is_ignored(self):
        hints = wd.identity_hints(profile())
        shell = '<html><title>ASVO</title><script src="/a.js"></script><body></body></html>'
        site = fake_site({"https://asvomaloy.no/": shell})
        rendered = {"final_url": "https://someone-else.no/", "html": f"<html><body>{ORG}</body></html>", "links": [], "elapsed_ms": 5}
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True), \
                patch.object(js_render, "available", lambda: True), patch.object(js_render, "render", lambda url, **kw: rendered), \
                patch.object(wd, "assert_public_url", lambda url: None):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(20, 60))
        self.assertNotEqual(outcome["status"], "verified")

    def test_missing_playwright_skips_render_gracefully(self):
        hints = wd.identity_hints(profile())
        shell = '<html><title>ASVO</title><script src="/a.js"></script><body></body></html>'
        site = fake_site({"https://asvomaloy.no/": shell})
        with patch.object(wd, "_get", site), patch.object(wd, "_resolves", lambda host: True), patch.object(js_render, "available", lambda: False):
            outcome = wd.probe_site("asvomaloy.no", hints, wd._Budget(20, 60))
        self.assertEqual(outcome["status"], "unverified")

    def test_js_shell_detection(self):
        self.assertTrue(js_render.looks_like_js_shell(b'<div id="root"></div><script src=x></script>', 0))
        self.assertFalse(js_render.looks_like_js_shell(b"<p>plain static page</p>", 3000))

    def test_private_hosts_are_blocked_in_the_renderer(self):
        self.assertTrue(js_render._private_host("http://127.0.0.1/x"))
        self.assertTrue(js_render._private_host("http://localhost:8080/"))
        self.assertTrue(js_render._private_host("http://10.0.0.5/"))
        self.assertFalse(js_render._private_host("https://example.no/app.js"))


class RobustnessTests(unittest.TestCase):
    def test_role_change_with_list_valued_entity_name_is_described(self):
        from norway_company_agent.synthesis import _describe_change

        change = {"field": "roles.roles", "old_value": [], "new_value": [{"role_code": "REVI", "name": ["REVISJON", "AS"]}]}
        self.assertIn("REVISJON AS", _describe_change(change))


class NavCacheTests(unittest.TestCase):
    def test_cache_write_uses_unique_temp_and_survives_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav.jsonl.gz"
            feed = NavJobsFeed(cache_path=path)
            feed.headers = {"u1": {"uuid": "u1", "status": "ACTIVE", "sistEndret": "2026-11-01T00:00:00+00:00"}}
            feed._save_cache(datetime(2026, 11, 10, tzinfo=timezone.utc))
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                meta = json.loads(handle.readline())
            self.assertTrue(str(meta["synced_until"]).startswith("Tue 2026-11-10") or meta["synced_until"].startswith("2026-11-10T00:00:00"))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

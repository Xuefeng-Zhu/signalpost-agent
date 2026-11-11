from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import site_signals as ss  # noqa: E402
from norway_company_agent import nav_jobs as nj  # noqa: E402


def page(html: str, url: str = "https://example.no/") -> dict:
    return ss._parse_page({"raw": html.encode(), "final_url": url, "retrieved_at": "t", "content_sha256": "h"})


PROFILE = {"organisation_number": "916340257", "name": "EKSEMPEL RØR AS", "evidence": {"registry": {"value": {"epostadresse": "eier@gmail.com"}}}}


class DateParsingTests(unittest.TestCase):
    def test_norwegian_and_iso_dates(self):
        today = date(2026, 11, 10)
        self.assertEqual(ss.parse_date("Publisert 12. mars 2025", today=today), "2025-03-12")
        self.assertEqual(ss.parse_date("12.03.2025", today=today), "2025-03-12")
        self.assertEqual(ss.parse_date("2024-10-30T10:00", today=today), "2024-10-30")
        self.assertIsNone(ss.parse_date("01.01.2030", today=today))  # future dates are rejected
        self.assertIsNone(ss.parse_date("tlf 22 33 44 55", today=today))


class SocialTests(unittest.TestCase):
    def test_share_links_vendor_handles_and_unmatched_handles_are_not_published(self):
        html = """<a href="https://www.facebook.com/sharer/sharer.php?u=x">del</a>
        <a href="https://facebook.com/eksempelror">fb</a>
        <a href="https://instagram.com/magezonvn">theme</a>
        <a href="https://www.facebook.com/elementor">vendor</a>
        <a href="https://linkedin.com/company/123456">li</a>"""
        p = page(html)
        published, candidates = ss.extract_social([p], ss.brand_tokens(PROFILE, "https://eksempelror.no/"))
        self.assertEqual([item["url"] for item in published], ["https://facebook.com/eksempelror"])
        self.assertEqual({item["url"] for item in candidates}, {"https://instagram.com/magezonvn", "https://linkedin.com/company/123456"})

    def test_facebook_p_slug_handle(self):
        self.assertEqual(ss._social_handle("https://facebook.com/p/Austevoll-Auto-As-100064576124376"), "Austevoll-Auto-As-100064576124376")

    def test_jsonld_same_as_is_published(self):
        html = """<script type="application/ld+json">{"@type":"Organization","name":"EKSEMPEL RØR AS","sameAs":["https://www.linkedin.com/company/123456/"]}</script>"""
        published, _ = ss.extract_social([page(html)], ss.brand_tokens(PROFILE, "https://eksempelror.no/"))
        self.assertEqual(published[0]["match"], "site_jsonld_organization_sameAs")


class ContactTests(unittest.TestCase):
    def test_only_site_domain_role_mailboxes_and_valid_norwegian_phones(self):
        html = """<a href="mailto:post@eksempelror.no">post</a> ola@eksempelror.no webmaster@byraa-agency.no
        <a href="tel:+4722334455">ring</a> Telefon: 912 34 567 Org.nr 916 340 257"""
        emails, phones = ss.extract_contacts(page(html, "https://eksempelror.no/"), "eksempelror.no", None)
        self.assertEqual([e["value"] for e in emails], ["post@eksempelror.no"])
        self.assertEqual(sorted(p["value"] for p in phones), ["+47 22 33 44 55", "+47 91 23 45 67"])


class NewsTests(unittest.TestCase):
    def test_feed_with_default_welcome_post_is_distrusted(self):
        rss = b"""<rss><channel><item><title>Hei, verden!</title><link>https://a.no/hei/</link><pubDate>Mon, 11 Dec 2017 10:00:00 +0000</pubDate></item>
        <item><title>Transitions In UX Design</title><link>https://a.no/ux/</link><pubDate>Fri, 04 Oct 2013 10:00:00 +0000</pubDate></item></channel></rss>"""
        self.assertEqual(ss.parse_feed(rss, "a.no"), [])

    def test_feed_posts_on_other_domains_are_dropped(self):
        rss = b"""<rss><channel><item><title>Ny avtale</title><link>https://a.no/ny-avtale/</link><pubDate>Mon, 02 Nov 2026 10:00:00 +0000</pubDate></item>
        <item><title>Annet</title><link>https://other.no/x/</link></item></channel></rss>"""
        posts = ss.parse_feed(rss, "a.no")
        self.assertEqual([(p["url"], p["published_at"]) for p in posts], [("https://a.no/ny-avtale/", "2026-11-02")])

    def test_index_posts_skip_assets_and_take_time_elements(self):
        html = """<article><h2><a href="/nyheter/ny-avtale-med-kommunen">Ny avtale med kommunen</a></h2><time datetime="2026-10-01">1. okt</time></article>
        <a href="/content/uploads/2026/03/logo.png">Codex logo – blå bakgrunn variant</a>"""
        posts = ss.extract_index_posts(page(html, "https://a.no/nyheter/"), "https://a.no/nyheter/", "a.no")
        self.assertEqual([(p["url"], p["published_at"]) for p in posts], [("https://a.no/nyheter/ny-avtale-med-kommunen", "2026-10-01")])


class OwnershipTests(unittest.TestCase):
    def test_host_site_naming_the_entity_is_not_owned(self):
        profile = {"organisation_number": "916340257", "name": "ARENDAL SERVERING AS"}
        claim = {"identity_method": "legal_name_and_registered_address", "value": "https://www.arendalkulturhus.no/", "evidence": {"source_url": "https://www.arendalkulturhus.no/kontakt/"}}
        home = {"url": "https://www.arendalkulturhus.no/", "title": "Arendal kulturhus", "headings": ["Program"], "text": "Velkommen"}
        self.assertFalse(ss.site_ownership(profile, claim, home)["owned"])

    def test_brand_domain_and_acronym_are_owned(self):
        claim = {"identity_method": "organisation_number", "value": "https://hvf.no/", "evidence": {"source_url": "https://hvf.no/kontakt"}}
        home = {"url": "https://hvf.no/", "title": "Folkehøgskole", "headings": [], "text": ""}
        self.assertTrue(ss.site_ownership({"organisation_number": "1", "name": "HURDAL VERK FOLKEHØGSKOLE AS"}, claim, home)["owned"])
        home2 = {"url": "https://www.eksempelror.no/", "title": "Eksempel Rør", "headings": [], "text": ""}
        self.assertTrue(ss.site_ownership(PROFILE, {**claim, "value": "https://www.eksempelror.no/"}, home2)["owned"])


class CareersTests(unittest.TestCase):
    def test_word_boundary_terms_do_not_match_product_slugs(self):
        links = [("https://moo.no/produkter/chiic-cow-jobbveske/", "Jobbveske"), ("https://moo.no/ledige-stillinger/", "Ledige stillinger")]
        self.assertEqual(ss._pick_link(links, ss.CAREER_TERMS, "moo.no", set()), "https://moo.no/ledige-stillinger/")

    def test_postings_from_jsonld_and_ats(self):
        html = """<script type="application/ld+json">{"@type":"JobPosting","title":"Rørlegger","datePosted":"2026-10-01"}</script>
        <a href="https://eksempel.teamtailor.com/jobs/123-servicetekniker">Servicetekniker Oslo</a>"""
        postings, none_open = ss.extract_job_postings(page(html, "https://a.no/karriere/"), "https://a.no/karriere/", "a.no")
        self.assertEqual({p["method"] for p in postings}, {"jsonld_jobposting", "ats_job_link"})
        self.assertFalse(none_open)


class NavTests(unittest.TestCase):
    def test_name_normalization(self):
        self.assertEqual(nj.normalize_name("Ro Catering Og Eventer AS"), nj.normalize_name("RO CATERING OG EVENTER AS"))
        self.assertEqual(nj.normalize_name("Måløy Sjø AS"), "maloy sjo")

    def test_orgnr_gate_rejects_namesakes(self):
        feed = nj.NavJobsFeed()
        feed.status = "synced"
        feed.synced_at = "t"
        header = {"uuid": "u1", "status": "ACTIVE", "businessName": "Veidekke Entreprenør AS", "url": "/api/v1/feedentry/u1", "sistEndret": "2026-11-01"}
        feed.by_name = {nj.normalize_name("Veidekke Entreprenør AS"): [header]}
        feed.by_token = {}
        detail = {"status": "ACTIVE", "ad_content": {"uuid": "u1", "title": "Anleggsleder", "employer": {"name": "VEIDEKKE INDUSTRI AS", "orgnr": "974288702"}}}
        feed._get = lambda path, **kw: (200, detail, b"{}")
        profile = {"organisation_number": "984024290", "name": "VEIDEKKE ENTREPRENØR AS", "evidence": {"locations": {"value": {"locations": [{"organisation_number": "999999999", "name": "VEIDEKKE ENTREPRENØR AS AVD OSLO"}]}}}}
        record, _ = nj.collect_nav_jobs(profile, feed)
        self.assertEqual(record["status"], "not_found")
        detail["ad_content"]["employer"]["orgnr"] = "999999999"
        record, _ = nj.collect_nav_jobs(profile, feed)
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"]["postings"][0]["matched_via"], "subunit_orgnr")
        self.assertNotIn("contactList", str(record))

    def test_unsynced_feed_is_a_terminal_source_error(self):
        record, _ = nj.collect_nav_jobs({"organisation_number": "1", "name": "X"}, None)
        self.assertEqual(record["status"], "source_error")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from norway_company_agent.batch import terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.synthesis import build_synthesis, check_narrative, polish_with_llm, validate_synthesis  # noqa: E402

ORG = "931015737"
T = "2026-11-10T17:51:00Z"


def profile() -> dict:
    raw = {
        "organisasjonsnummer": ORG, "navn": "EKSEMPEL BEMANNING AS", "organisasjonsform.kode": "AS",
        "organisasjonsform.beskrivelse": "Aksjeselskap", "naeringskode1.kode": "78.200",
        "naeringskode1.beskrivelse": "Utleie av arbeidskraft", "forretningsadresse.poststed": "MOSS",
        "vedtektsfestetFormaal": "Rekruttering og utleie av arbeidskraft.", "aktivitet": "Utleie av arbeidskraft.",
        "stiftelsesdato": "2022-12-28", "registreringsdatoenhetsregisteret": "2023-03-13",
        "harRegistrertAntallAnsatte": "true", "antallAnsatte": "71", "registreringsdatoAntallAnsatteEnhetsregisteret": "2026-09-14",
        "konkurs": "false", "underAvvikling": "false",
    }
    site_ev = {"source_url": "https://eksempel.no/", "retrieved_at": T, "content_sha256": "c" * 64, "claim_span": "Org nr: 931 015 737"}
    return {
        "organisation_number": ORG, "name": raw["navn"], "legal_form": "AS",
        "evidence": {
            "registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv", value=raw, retrieved_at=T, content_sha256="a" * 64),
            "registry_live": evidence("registry_live", "available", "official_registry_live", f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}", retrieved_at=T, content_sha256="b" * 64,
                                      value={"organisation_number": ORG, "employees": 71, "business_address": {"adresse": ["Årvollskogen 103"], "postnummer": "1529", "poststed": "MOSS", "kommune": "MOSS"}}),
            "financials": evidence("financials", "available", "official_annual_accounts", f"https://data.brreg.no/regnskapsregisteret/regnskap/{ORG}", retrieved_at=T, content_sha256="d" * 64, value={"records": [
                {"account_type": "SELSKAP", "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "currency": "NOK", "revenue": 3383867.0, "operating_result": 206752.0, "annual_result": 155789.0, "assets": 792691.0, "equity": 412792.0, "debt": 379900.0},
                {"account_type": "SELSKAP", "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "currency": "NOK", "revenue": None, "operating_result": 88957.0, "annual_result": -69694.0, "assets": 844291.0, "equity": 482486.0, "debt": 361806.0},
            ]}),
            "roles": evidence("roles", "available", "official_roles", f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}/roller", retrieved_at=T, content_sha256="e" * 64, value={"roles": [
                {"name": "Kari Nordmann", "organisation_number": None, "role_code": "DAGL", "role": "Daglig leder", "group": "Daglig leder", "last_changed": "2023-03-13", "inactive": False},
                {"name": "Ola Nordmann", "organisation_number": None, "role_code": "LEDE", "role": "Styrets leder", "group": "Styre", "last_changed": "2026-02-07", "inactive": False},
                {"name": "REVISOR AS", "organisation_number": "999999999", "role_code": "REVI", "role": "Revisor", "group": "Revisor", "last_changed": "2023-03-13", "inactive": False},
            ]}),
            "locations": evidence("locations", "available", "official_subunits", "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=931015737&size=1000", retrieved_at=T, content_sha256="f" * 64,
                                  value={"locations": [{"organisation_number": "931050710", "name": "AVD MOSS", "address": {"poststed": "MOSS"}, "employees": 71}]}),
            "group": evidence("group", "not_found", "official_group_structure", f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{ORG}", note="HTTP 404", retrieved_at=T),
            "website": {**evidence("website", "available", "discovered_company_website", "https://eksempel.no/", retrieved_at=T, value={}),
                        "official_website": {"value": "https://eksempel.no/", "identity_method": "organisation_number", "lead_source": "legal_name_domain_guess", "evidence": site_ev}},
            "site_signals": evidence("site_signals", "available", "company_owned_site_signals", "https://eksempel.no/", retrieved_at=T, value={
                "field_states": {"social_profiles": "available", "news_posts": "available", "careers": "not_found", "job_postings": "not_found", "contact": "available", "business_description": "available"},
                "social_profiles": [{"platform": "facebook", "url": "https://facebook.com/eksempel", "evidence": {**site_ev, "claim_span": "link -> facebook.com/eksempel"}}],
                "news_posts": [{"title": "Vi åpner kontor i Bergen", "url": "https://eksempel.no/2024/08/09/bergen/", "published_at": "2024-08-09", "evidence": {**site_ev, "source_url": "https://eksempel.no/feed/"}}],
                "contact": {"emails": [{"value": "post@eksempel.no", "evidence": site_ev}], "phones": []},
                "business_description": {"meta_description": "Bemanning for helse og barnehage.", "evidence": site_ev},
                "services": {"headings": ["Tjenester", "Helsevikarer", "Kontakt oss", "Barnehage"], "evidence": {**site_ev, "source_url": "https://eksempel.no/tjenester/"}},
            }),
            "nav_jobs": evidence("nav_jobs", "not_found", "nav_job_vacancy_feed", "https://pam-stilling-feed.nav.no/api/v1/feed", retrieved_at=T, value={"postings": []}),
        },
    }


class SynthesisTests(unittest.TestCase):
    def test_every_statement_is_cited_and_supported(self):
        p = profile()
        s = build_synthesis(p)
        self.assertTrue(s["validation"]["passed"], s["validation"]["dropped"])
        self.assertGreaterEqual(len(s["statements"]), 10)
        ids = {x["id"] for x in s["sources"]}
        for st in s["statements"]:
            self.assertTrue(st["citations"])
            self.assertTrue(set(st["citations"]) <= ids)
        self.assertTrue(validate_synthesis(s, p)["passed"])
        text = " ".join(x["text"] for x in s["statements"])
        self.assertIn("Rekruttering og utleie av arbeidskraft.", text)  # original Norwegian purpose quoted
        self.assertIn("“Helsevikarer”; “Barnehage”", text)  # boilerplate headings removed
        self.assertNotIn("Kontakt oss", text)

    def test_missing_revenue_is_not_zero_and_period_is_stated(self):
        s = build_synthesis(profile())
        fin = [x["text"] for x in s["statements"] if x["section"] == "financials"]
        self.assertIn("2025-01-01 to 2025-12-31", fin[0])
        self.assertIn("Not reported in that filing: revenue", fin[0])
        self.assertNotIn("revenue NOK 0", fin[0])
        self.assertIn("NOK -69,694", fin[0])

    def test_tampered_number_or_pointer_fails_the_support_check(self):
        p = profile()
        s = build_synthesis(p)
        bad = copy.deepcopy(s)
        target = next(x for x in bad["statements"] if x["section"] == "financials")
        target["text"] = target["text"].replace("88,957", "88,958")
        self.assertFalse(validate_synthesis(bad, p)["passed"])
        bad = copy.deepcopy(s)
        bad["statements"][0]["citations"] = ["S99"]
        self.assertFalse(validate_synthesis(bad, p)["passed"])
        bad = copy.deepcopy(s)
        bad["statements"][0]["facts"][0]["value"] = "ANNET NAVN AS"
        self.assertFalse(validate_synthesis(bad, p)["passed"])

    def test_unknowns_list_every_missing_information_type(self):
        s = build_synthesis(profile())
        unknown = {u["info_type"]: u["state"] for u in s["unknowns"]}
        self.assertEqual(unknown["group"], "not_available")
        self.assertEqual(unknown["careers"], "not_available")
        self.assertEqual(unknown["job_postings"], "not_available")
        self.assertNotIn("official_website", unknown)
        self.assertEqual(s["availability"]["official_website"]["state"], "available")

    def test_no_verified_site_marks_site_signals_not_applicable(self):
        p = profile()
        p["evidence"]["website"] = evidence("website", "ambiguous", "registry_linked_company_website", "https://other.no/", retrieved_at=T)
        p["evidence"]["site_signals"] = evidence("site_signals", "not_applicable", "company_owned_site_signals", "https://other.no/", retrieved_at=T)
        s = build_synthesis(p)
        unknown = {u["info_type"]: u["state"] for u in s["unknowns"]}
        self.assertEqual(unknown["official_website"], "ambiguous")
        self.assertEqual(unknown["news_posts"], "not_applicable")
        self.assertFalse(any("other.no" in x["text"] for x in s["statements"]))

    def test_what_changed_uses_previous_snapshot(self):
        old, new = profile(), profile()
        new["evidence"]["roles"]["value"]["roles"][1]["name"] = "Per Hansen"
        new["evidence"]["nav_jobs"] = evidence("nav_jobs", "available", "nav_job_vacancy_feed", "https://pam-stilling-feed.nav.no/api/v1/feed", retrieved_at=T, value={"postings": [{"uuid": "u1", "title": "Vikar"}]})
        changes = diff_profile(old, new)
        self.assertEqual({c["field"] for c in changes}, {"roles.roles", "nav_jobs.posting_ids"})
        self.assertEqual(diff_profile(new, copy.deepcopy(new)), [])  # idempotent
        new["changes"] = changes
        s = build_synthesis(new, changes=changes, refresh={"previous_snapshot_supplied": True, "company_in_previous_snapshot": True, "previous": {"path": "prev.jsonl"}})
        self.assertEqual(s["what_changed"]["status"], "compared")
        texts = [x["text"] for x in s["statements"] if x["section"] == "what_changed" and "previous snapshot" in x["text"]]
        self.assertTrue(any("added Per Hansen as chair of the board" in t for t in texts), texts)
        self.assertTrue(any("Active Nav job ads: 1 added" in t for t in texts), texts)
        self.assertTrue(validate_synthesis(s, new)["passed"])
        none = build_synthesis(profile())
        self.assertEqual(none["what_changed"]["status"], "no_previous_snapshot")

    def test_unreachable_verified_site_is_a_failed_recheck_not_a_change(self):
        from norway_company_agent.refresh import classify_refresh_failures
        old, new = profile(), profile()
        new["evidence"]["website"] = {**evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", retrieved_at=T),
                                      "discovery": {"candidates": [{"domain": "eksempel.no", "outcome": "unreachable"}]}}
        changes = classify_refresh_failures(old, new, diff_profile(old, new))
        web = [c for c in changes if c["field"] == "website.official_website"]
        self.assertTrue(web and web[0]["material"] is False and web[0]["change_type"] == "source_unreachable")
        new["changes"] = changes
        s = build_synthesis(new, changes=changes, refresh={"previous_snapshot_supplied": True, "company_in_previous_snapshot": True})
        self.assertEqual(s["what_changed"]["count"], 0)
        self.assertEqual(s["what_changed"]["refresh_failures"], 1)
        self.assertTrue(any("could not be reached in this run" in x["text"] for x in s["statements"]))
        self.assertTrue(validate_synthesis(s, new)["passed"])

    def test_llm_polish_is_post_checked_and_off_by_default(self):
        s = build_synthesis(profile())
        self.assertEqual(polish_with_llm(copy.deepcopy(s), config=None)["llm_polish"]["status"], "not_configured")
        config = {"model": "fake", "api_key": "x", "base_url": "http://unused", "timeout": "1"}
        first = s["statements"][0]
        good = f"{first['text']} " + "".join(f"[{c}]" for c in first["citations"])
        self.assertEqual(polish_with_llm(copy.deepcopy(s), config, chat=lambda c, m: good)["llm_polish"]["status"], "accepted")
        invented = "It had revenue of NOK 9,999,999 last year [S1]."
        self.assertEqual(polish_with_llm(copy.deepcopy(s), config, chat=lambda c, m: invented)["llm_polish"]["status"], "rejected_by_post_check")
        uncited = "It is a staffing company."
        self.assertTrue(check_narrative(uncited, s))
        renamed = "The company is owned by Equinor [S1]."
        self.assertTrue(check_narrative(renamed, s))

    def test_envelope_carries_synthesis_and_validation_checks_it(self):
        p = profile()
        p["changes"] = []
        p["refresh"] = {"previous_snapshot_supplied": False}
        p["synthesis"] = build_synthesis(p)
        env = terminal_envelope(p, run_id="t", modules=["registry"], started_at=T, completed_at=T)
        self.assertIn("synthesis", env)
        self.assertNotIn("synthesis", env["profile"])
        result = validate_envelopes([env], 1)
        self.assertTrue(result["checks"]["synthesis_present_and_every_statement_supported"])
        env["synthesis"]["statements"][0]["text"] += " Founded 1999."
        self.assertFalse(validate_envelopes([env], 1)["checks"]["synthesis_present_and_every_statement_supported"])

    def test_static_site_is_self_contained(self):
        from build_site import build_site
        p = profile()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.jsonl"
            path.write_text(json.dumps(p, ensure_ascii=False) + "\n", encoding="utf-8")
            result = build_site([("test", path)], Path(tmp) / "site")
            html = (Path(tmp) / "site" / "index.html").read_text(encoding="utf-8")
            data = json.loads((Path(tmp) / "site" / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(result["companies"], 1)
        self.assertNotIn("__DATA__", html)
        self.assertNotRegex(html, r"<(script|link)[^>]+(src|href)=\"https?://")
        company = data["companies"][0]
        self.assertTrue(all(f["src"] and "s" in f for f in company["facts"]))
        rev = next(f for f in company["facts"] if f["l"].startswith("Revenue"))
        self.assertIsNone(rev["v"])  # missing revenue stays missing
        self.assertEqual(rev["s"], "not_available")


class RefreshReplayRegressionTests(unittest.TestCase):
    def test_public_refresh_fixture_has_no_false_changes(self):
        from run_refresh_replay import materialize
        from norway_company_agent.refresh import diff_datasets
        manifest = json.loads((ROOT / "tests" / "fixtures" / "refresh-snapshots.json").read_text(encoding="utf-8"))
        modules = set(manifest["modules"])
        old, _ = materialize(manifest["profiles"], manifest["snapshots"]["old"], modules)
        new, _ = materialize(manifest["profiles"], manifest["snapshots"]["new"], modules)
        observed = {(c["organisation_number"], c["field"]) for c in diff_datasets(old, new)}
        expected = {(c["organisation_number"], c["field"]) for c in manifest["expected_changes"]}
        self.assertEqual(observed, expected)
        self.assertEqual(diff_datasets(new, new), [])


if __name__ == "__main__":
    unittest.main()

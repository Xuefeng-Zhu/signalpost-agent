# Official-website discovery (org-number-gated)

Code: `src/norway_company_agent/website_discovery.py`, wired in through
`resolve_official_website()` from `scripts/run_competition_batch.py`. Tests:
`tests/test_website_discovery.py`.

## Flow per company
1. **Registry site first.** `website.fetch_website(brreg hjemmeside)` works as before, but now also
   records per-page `identity_signals` computed on the *full* HTML text (footers, meta, JSON-LD),
   which trafilatura's main-text extract drops. The identity gate (`identity.py`) runs as before.
2. **Discovery only if the registry site is missing, dead or unverified.** Candidate domains, in order:
   - domain of Brreg `epostadresse` (bulk and live entity), skipping free-mail/ISP domains;
   - a changed live `hjemmeside`;
   - guesses from the legal name: drop the legal form (AS/ASA/ANS/DA/ENK/SA/NUF…, also a leading "AS"),
     fold æøå both as ae/o/a and as ae/oe/aa, joined and hyphenated, with and without generic words
     (holding, invest, norge, eiendom…), `.no` first, then `.com` for the main form. Max 9 candidates.
   - Directory, marketplace, social, registrar/parking and property-portal hosts are excluded,
     both as candidates and as redirect targets. Parent/group sites are **not** used as leads.
3. **Cheap probe.** Parallel DNS check first (no HTTP request for domains that don't resolve). Then
   robots.txt, homepage (https → https://www → http → http://www), and if the homepage has no proof,
   up to 3 same-domain identity pages (kontakt/om-oss/personvern/privacy/vilkår/terms…, or `/kontakt`,
   `/om-oss` as fallbacks). Subpages of name guesses are only fetched when the homepage title/host
   shares a distinctive name token. Stops at the first verified candidate.
4. **Publish** the verified site: re-crawl it with `fetch_website` (for description, social links,
   structured data), merge the proof pages, re-run the identity gate, and emit
   `evidence.website.official_website` = `{value, confidence, identity_method, lead_source,
   evidence{source_url, retrieved_at, content_sha256, claim_span}}`.

## Identity gate (what counts as proof)
- **Org number** (primary): the exact 9-digit number as a standalone digit run, any of
  `123456789`, `123 456 789`, `123.456.789`, `NO 123 456 789 MVA`, `NO123456789MVA`, nbsp.
  Digit runs that join into more than 9 digits never match (this also fixed a substring bug in the
  old gate, which compared against *all* page digits concatenated).
  - On the homepage → exact (1.0). On an identity subpage → exact (0.97).
  - Becomes **conflict → `ambiguous`** if any other mod-11-valid org number is on the checked pages,
    or (for subpage proof) the site carries group markers (konsern, våre selskaper, subsidiaries…).
- **Legal name + registered address** (secondary, 0.92): full legal name including the legal form
  (e.g. "Brandmaker AS"), the registered postcode and the street name on the same page, with no
  conflicting org numbers.
- Name-only matches are **never** published from discovery (the existing registry-site rule that
  accepts a full legal-name match on the Brreg-declared site is unchanged).
- Parked/for-sale/placeholder pages are rejected.
- A fetched but unproven site is now `status: "ambiguous"` (module state `ambiguous`), not
  `available`. Before this change smoke-001 showed 3 sites as available, and 2 of them had failed the
  gate (studiox.no for APPELX AS, carlin.no for CARLIN INVEST AS).
- Every candidate is kept in `evidence.website.discovery.candidates` with its outcome
  (dns_not_resolved / unreachable / parked / rejected_host / blocked_robots / unverified /
  ambiguous / verified), pages checked, and the evidence for and against it.

## Budgets
Per company: 14 discovery HTTP requests (`--website-request-cap`), 40 s deadline, 5 s timeouts,
4 MB page cap (larger pages are parsed truncated). An unreachable slow host stops after 2 base
variants. Batch concurrency `--workers` (12 used for smoke-002). `--no-website-discovery` turns
the step off.

## Results
| run | companies | official websites published | false matches in spot-check |
|---|---|---|---|
| smoke-001 (baseline) | 100 | 3 "available", only 1 passed the identity gate | (2 unverified sites were shown as available) |
| smoke-002 | 100 | 8 (4 org number, 2 name+address, 2 registry site + name) | 0 / 8 |
| validation (99 different companies, ≥3 employees, no registry site) | 99 | 24 (15 org number, 9 name+address) | 0 / 24 |

smoke-002: 100 envelopes, validation passed, 69 s in-process (88 s wall clock including the bulk
CSV scan), 920 HTTP requests (420 for websites, p50 4 and max 14 per company), plus 708 DNS lookups.
Third-party cost is $0. The smoke sample is mostly dormant holding and property companies, so its
ceiling is low. The operating-company sample shows what to expect on real batches.

## Caveats
- In the Hark sandbox, plain-HTTP requests made through the environment proxy returned false 404s,
  so runs here used `no_proxy='*'`. Builderr's infrastructure should not need this.
- In this sandbox `datetime.isoformat()` is patched to a human-readable local time, so the
  `retrieved_at` values in these local outputs look like "Tue 2026-11-10 9:08 AM PST". The code
  itself writes ISO UTC.
- JS-only sites (empty static HTML) are not verified. That needs a Playwright fallback.

## Next steps for recall
1. Sitemap.xml / footer-link pass, plus a Playwright fallback for JS shells when a name-matching
   candidate is unverified. Seen in the validation run: aquapredict.io, oyemaskin.no, govi.no,
   hamarhundeskole.no, fjordhelikopter.no.
2. From verified sites: company-owned social links (already extracted and gated), careers/news pages.
   Then NAV arbeidsplassen jobs matched by org number. Not yet verified: whether its public feed
   carries the employer org number, and what its access terms are.
3. A licensed search API (Brave, if Builderr supplies the key) as an extra *lead* source for brand
   names that differ from the legal name. Use the same org-number gate, and keep results transient.

## Revision 2: recall levers (identity gate unchanged)
Code: `website_discovery.py`, `js_render.py` (new), `official.py`, `nav_jobs.py`, `scripts/run_competition_batch.py`.
Tests: `tests/test_recall_leads.py`.

New **leads** (still only leads; publication needs the same org-number or legal-name + registered-address proof):
- `nav_employer_homepage`: `employer.homepage` from Nav ads whose `employer.orgnr` is exactly this entity or one of
  its subunits. `nav_jobs` now runs *before* website discovery in each company task.
- `brreg_subunit_hjemmeside` / `brreg_subunit_email_domain`: the sub-units' own `hjemmeside`/`epostadresse`
  (stored as `locations.value.web_leads`, beside the `locations` list so the refresh diff is unchanged).
- `brreg_email_domain_variant`: `firma.com` mail → also `firma.no` (and the reverse).
- `legal_name_brand_guess`: leading/trailing token runs of the legal name (`jeistad.no` for JEISTAD ENTREPRENØR AS,
  `sigurdlima.no` for RØRLEGGERFIRMA SIGURD LIMA AS); bare trade words (`bygg.no`, `restaurant.no`) are never emitted.

Wider **identity pages**: a candidate whose joined host label contains a legal-name word (`asvomaloy.no`) now counts
as promising, so its contact/about pages are fetched; up to 4 identity subpages, padded with fallback paths
(`/kontakt`, `/om-oss`, `/kontakt-oss`, `/personvern`, `/om`).

**Headless fallback suspended** (`js_render.py`): the original Revision 2 fallback rendered JS shells in
Chromium. That path is now disabled unconditionally because this repository does not provide a verified
connection-level public-network boundary. DNS validation alone cannot prevent browser DNS rebinding or
every browser-managed connection. `SIGNALPOST_JS_FALLBACK=1` does not override this safety restriction.
The entry point no longer installs Chromium. Discovery continues using static HTML, with lower expected
coverage on JS-only sites; `operations.js_render` explains the restriction. Offline URL/DNS tests exercise
the defensive policy, not a claim of live Chromium isolation.

Budget: discovery cap 14 → 20 requests per company, deadline 40 → 45 s, up to 16 candidates (DNS-checked in parallel).

Also fixed: concurrent runs sharing the Nav cache could clobber one temp file and fail the whole feed
(`source_error` for all 100 companies in ops-003); the temp name is now per process/thread and a cache write
failure no longer fails the sync. A list-valued role-holder name crashed the change description in synthesis
(and then the site build); both are tolerant now.

| run | websites | social | news | careers pages | any job posting | Nav jobs | contact | description |
|---|---|---|---|---|---|---|---|---|
| ops-003 → ops-004 | 39 → 44 | 26 → 30 | 15 → 17 | 12 → 14 | 1 → 4 | 0 → 3 | 36 → 41 | 37 → 42 |

All 5 new ops websites were spot-checked by hand against the Brreg name/address/org number (0 wrong).

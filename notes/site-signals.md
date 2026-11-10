# Site signals + Nav jobs (smoke-003 / ops-001)

New modules in `scripts/run_competition_batch.py`, both on by default (`--modules` now ends with
`website,site_signals,nav_jobs`). Each always writes a terminal state into the envelope.

| module | code | states |
|---|---|---|
| `site_signals` | `src/norway_company_agent/site_signals.py` | `not_applicable` (no verified site), `complete`, `not_found`, `ambiguous` (site not run by the entity), `blocked_robots`, `source_error` |
| `nav_jobs` | `src/norway_company_agent/nav_jobs.py` | `complete`, `not_found` (no ad with this org number), `source_error` (feed unavailable) |

Tests: `tests/test_site_signals.py` (15 tests). Full suite: 141 passed, 5 subtests.

## site_signals: from the verified official website only
- Runs only when `evidence.website.official_website` was published by the identity gate.
- **Site-ownership gate** (added after a spot-check): the gate proves the entity *appears* on the site,
  not that it *runs* the site. Example: ARENDAL SERVERING AS has the Brreg hjemmeside
  arendalkulturhus.no. The café company is named on the culture house's contact page, but the
  concert programme there belongs to the culture house. Site-wide signals are attributed only if one of
  these holds: the org number is on the homepage; the domain carries the full legal name; the domain is
  the acronym of the legal name (hvf.no); or every distinctive legal-name word is in the domain or the
  homepage title/headings. Otherwise the state is `ambiguous` and nothing is published (2 of 40 in ops:
  arendalkulturhus.no and moo.no; the second is a false negative we accept).
- Budget: robots.txt is honoured (one fetch, then `can_fetch` before every page). At most 8 requests
  per company (`--site-signals-request-cap`), a 25 s deadline and 6 s timeouts. Nothing off the
  verified registered domain is fetched, and redirects off the domain are dropped.
- Pages, in order: robots, homepage, the RSS/Atom feed linked from the homepage, else the
  news/press/blog index, the careers page, the contact page, the services page, and the sitemap (only
  when no news was found).
- **Social profiles**: built on `normalize_social_url` (rejects sharer/intent/post/reel/plugins links).
  It adds a vendor/theme handle blocklist (elementor, wix, magezon…). A profile is published only
  when it is in the site's own JSON-LD `Organization.sameAs`, or its handle matches the domain label,
  the joined legal name or a distinctive name word. Everything else stays in
  `social_candidates_unpublished`. The website record's `social_links` is rewritten to that published
  set, and the rest moves to `social_link_candidates_unpublished`. The old code published unmatched
  links such as instagram.com/magezonvn (a theme vendor) on daisy.no.
- **News posts**: feed items (pubDate), then index items (`<time>` element, a date in nearby text with
  Norwegian month names, or a date in the URL path), then dated homepage teasers, then sitemap news URLs
  (`date_source: sitemap_lastmod`, no title). Dropped: assets, uploads, category/tag/page links, and
  other domains. A feed containing a default "Hello world"/"Hei verden" post is distrusted entirely,
  because it carries theme demo posts (byraa.no had 2013 "UX design" demo posts). Undated items are kept
  only as children of the news index, with titles of at least 20 characters.
- **Careers**: careers page (word-boundary terms; weak words such as "jobb" must be a whole path segment
  or the whole anchor text; dated article URLs are excluded), or an ATS link (teamtailor, jobbnorge,
  webcruiter, recman, finn job…). Postings come from JSON-LD `JobPosting`, ATS job links, or child pages of
  the careers path. An explicit "ingen ledige stillinger" sets `job_postings: checked_zero`.
- **Contact**: e-mails on the site's own registered domain (or the Brreg-registered e-mail). Role
  mailboxes (post@, kontakt@…) are preferred, and a named mailbox is used only when there is no role one.
  Phones are Norwegian 8-digit numbers from `tel:` links or labelled text (tlf/telefon/mobil), normalised
  to `+47 xx xx xx xx`.
- **Business description / services**: meta description, homepage title and headings, and the services
  page headings. These are company-reported claims for synthesis, never sentiment.
- Every fact carries `evidence{source_url, retrieved_at, content_sha256, claim_span}`.
  `value.field_states` gives per-type states: social_profiles, news_posts, careers, job_postings,
  contact, business_description.

## nav_jobs: Nav job-vacancy feed (pam-stilling-feed)
- Access terms (https://arbeidsplassen.nav.no/vilkar-api): free for anyone. Republishing is allowed, but
  ads must be removed when they become inactive or deleted and updated when they change, and personal
  data must be processed lawfully. The API needs a bearer JWT. Nav publishes a rotating **public token**
  "for experiments" at https://pam-stilling-feed.nav.no/api/publicToken, and that is what we use. It is
  not tied to our accounts. `NAV_JOBS_FEED_TOKEN` overrides it. For production, Builderr or the entrant
  should request a consumer token from nav.team.arbeidsplassen@nav.no by agreeing to the terms in
  writing.
- Feed headers carry `businessName` but **no org number**. The ad detail (`/api/v1/feedentry/{uuid}`)
  carries `employer.orgnr`, which is usually the **subunit** (underenhet) number, plus `employer.homepage`.
- Sync: 183-day window (ads are active for at most about 6 months). The window is split into 16
  `If-Modified-Since` segments that are fetched in parallel. The latest state per ad uuid is kept, and
  only ACTIVE ads are indexed. A cold sync is about 360 pages / 176 MB / 44 s (87.7k headers, 9,621
  active ads). It runs in a thread that overlaps the bulk CSV scan and the website work. Headers are
  cached in `.cache/nav-job-feed-headers.jsonl.gz` (a declared external cache, `--nav-cache ''`
  disables it), so a warm sync takes 3 requests / about 4 s.
- Match: candidates come from a normalized-name equality or a distinctive-token match against the legal
  name and the entity's own Brreg subunit names. An ad is **published only if `employer.orgnr` equals the
  entity's org number or one of its subunits' org numbers** (from the `locations` module, which nav_jobs
  forces on). Rejected candidates are kept with the reason (ops-001: 55 rejected, for example ads from
  VEIDEKKE INDUSTRI AS for VEIDEKKE ENTREPRENØR AS). `contactList` is never stored.
- Posting fields: title, job title, employer name/orgnr, matched_via (entity/subunit), published,
  expires, application due, work locations (city/municipality only), engagement type, extent, positions,
  sector, occupations, the arbeidsplassen ad URL, the original source, and evidence (ad URL, API
  identifier, sha256, claim span `employer.orgnr=…`).

## Coverage, before → after (companies; facts in parentheses)
| field | smoke-002 → smoke-003 | ops-000 → ops-001 |
|---|---|---|
| verified official website | 8 → 8 | 40 → 40 |
| social profiles (published) | 3 (7, unfiltered) → 3 (7, gated) | 23 (37, unfiltered) → 27 (55, gated) |
| news/blog posts | 0 → 4 (29, all dated) | 0 → 15 (95; 12 companies / 73 posts dated) |
| careers page | 0 → 0 | 0 → 13 |
| job postings (site + Nav) | 0 → 0 | 0 → 4 (Nav 3 companies / 6 ads, site 1) |
| contact e-mail | 0 → 3 (4) | 0 → 30 (32) |
| contact phone | 0 → 3 (3) | 0 → 30 (58) |
| business description / services | (website description only) → 7 | (website description only) → 38 |

The smoke sample is mostly dormant holding and property companies, so a site-derived ceiling is
about 8%. The ops sample (`ops-companies.jsonl`: 100 companies with at least 3 registry employees, not
bankrupt or in liquidation, seed 20261110, no overlap with smoke or the earlier validation set) is a
better guide for real batches.

## Runtime / requests / cost
- smoke-003: 100 envelopes, validation passed. 77.7 s in-process, 98 s wall (bulk scan and warm Nav
  sync included). 995 requests (website 419, site_signals 33, Nav details 43) plus 3 Nav feed
  requests. $0.
- ops-001: 100 envelopes, validation passed. 97.4 s in-process, 117 s wall. 1,302 requests (website
  534, site_signals 202, Nav details 66) plus 3 Nav feed requests. $0. A cold Nav sync adds about 360 requests
  but overlaps the other work (the cold run was 130 s wall).

## Spot-checks
47 fact checks: 21 + 11 site facts on ops, 12 on smoke, and 3 Nav ads. Each one re-fetched the
evidence URL and confirmed the claimed value is on it. For Nav, the subunit was confirmed via
`data.brreg.no/enhetsregisteret/api/underenheter/{nr}` → `overordnetEnhet`, and the ad page returned
200 with the same title. 0 wrong-company facts remain. Found and fixed during the checks: the
host-site attribution (Arendal kulturhus), CMS demo posts (byraa.no), an uploaded logo PNG counted as
a news post (codex.no), the "jobb" substring matching a product slug (moo.no "jobbveske") and a blog
post (flexibemanning.no), the facebook `/p/<slug>` handle, and personal e-mails being preferred over
role mailboxes.

## Sources evaluated and not integrated
- **Brønnøysund kunngjøringer** (w2.brreg.no/kunngjoring): `robots.txt` disallows `/kunngjoring/`. Skipped.
- **Enhetsregisteret oppdateringer API**: keyless and exact, but it returns only a timestamp and a change
  type ("Endring"). `includeChanges` gave no field diff for entities. Low value, so skipped.
- **Annual-report workforce** (`scripts/run_annual_report_workforce_connector.py`): the sampled PDFs
  (20/20 smoke) are scanned images, so pypdf finds no text and no org number. OCR (tesseract) works
  and the reports carry "Antall årsverk i regnskapsåret", but it costs about 10 s of CPU per company on
  2 cores, which is too slow for the batch budget. It could be an opt-in module or cached offline.
- **Google News RSS** (`scripts/run_google_news_rss_connector.py`): ops sample 4/100 companies, 5
  headlines. It is a search-result feed: the brief says search output is candidates, not evidence, and
  the connector itself is marked discovery-only/rights-review. Not integrated.

## Next steps
1. Use the Nav `employer.homepage` (tied to employer.orgnr) as a website lead for discovery. Optionally
   build a full orgnr index of the about 9.6k active ads: about 9.6k detail calls cold (2–3 min), then
   incremental via the cache. That removes the name prefilter.
2. Playwright fallback for JS-only sites, in both discovery and site_signals.
3. Opt-in OCR workforce module with a persistent PDF/OCR cache (it reaches dormant companies: "ingen
   ansatte" / "årsverk").
4. Fetch 1–2 article pages per site when the index gives no date, to read `article:published_time`.

# Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Run the starter on 100 companies before submitting. Larger local tests, including 1,000 or more companies, are encouraged but their precomputed profiles are not submitted or scored.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- discovers missing official websites (Brreg e-mail domain, legal-name domain guesses) and publishes one only when the site itself carries the exact org number or legal name + registered address (see `notes/website-discovery.md`);
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

```bash
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local test batch of any size. Use 100 rows for the recommended smoke test before trying a larger batch.

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# Use the 100-company batch as your smoke test.
cp entry-companies.jsonl smoke-companies.jsonl

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

# You may test at larger scale locally, but Builderr supplies the official batch for scoring.
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together for a larger local test. The 100-row smoke test above is practice only; Builderr supplies the companies for every official run.

### Signal modules in the batch command

`run_competition_batch.py` runs two more modules by default; each always writes a terminal state:

- `site_signals`: company-owned social profiles, dated news/blog posts, careers pages and postings,
  contact e-mail/phone and service descriptions. It reads only the identity-verified official website,
  respects robots.txt and stops after 8 requests per company (`--site-signals-request-cap`).
- `nav_jobs`: active job ads from Nav's public vacancy feed (terms: https://arbeidsplassen.nav.no/vilkar-api).
  An ad is published only when its `employer.orgnr` is the company's own organisation number or one of
  its Brreg subunits' numbers. The run uses the public token Nav publishes; set `NAV_JOBS_FEED_TOKEN`
  to use a consumer token instead. Feed headers are cached in `.cache/nav-job-feed-headers.jsonl.gz`
  (`--nav-cache ''` disables the cache). An incomplete, capped or looping feed sync is recorded as
  a source failure and does not advance the last complete cache checkpoint.

Details and measurements: `notes/site-signals.md`.

## Evaluator command (one command)

```bash
./run_signalpost.sh companies.jsonl out/run [previous-profiles.jsonl]
```

Writes `out/run/envelopes.jsonl` (one terminal envelope per input), `profiles.jsonl`, `report.json` and the review site in `out/run/site/`. It runs `uv sync --frozen` and downloads the Brreg bulk CSV if `brreg-enheter.csv` is missing. Equivalent explicit form:

```bash
uv sync
uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke4-profiles.jsonl \
  --output out/smoke4-envelopes.jsonl \
  --report out/smoke4-report.json \
  --run-id smoke-004 \
  --expected-count 100 \
  --previous-profiles out/smoke3-profiles.jsonl \
  --site-dir site
```

`--previous-profiles` (optional) is the previous run's profiles or envelopes JSONL. With it, every envelope
carries `changes` (material changes on stable claim keys) and `refresh` metadata; without it the
synthesis says "no previous snapshot". `--site-dir` (optional) also writes the static review site.

Each envelope now carries a top-level `synthesis` (see `notes/synthesis-ux.md`): what the company
does, size and latest filed accounts with reporting period, people, locations and group, digital
footprint, hiring, dated activity, what changed, and an explicit unknowns list. Every statement cites
source ids that resolve to URLs + retrieval times + hashes in the profile evidence, and the run's
validation re-checks every statement (`synthesis_present_and_every_statement_supported`). The
synthesis is deterministic and keyless. An optional LLM polish runs only when
`SIGNALPOST_LLM_API_KEY` is set (OpenAI-compatible; `SIGNALPOST_LLM_BASE_URL`, default
`https://api.openai.com/v1`; `SIGNALPOST_LLM_MODEL`, default `gpt-4o-mini`; `SIGNALPOST_LLM_TIMEOUT_S`).
The optional model may only select and reorder complete verified statements, copied verbatim with
all their original citations. Free factual rewriting is rejected; matching numbers and names alone
does not establish factual support. Statement text, fact values, and source pointers/metadata are
also checked against deterministic regeneration from the profile. This verifies output integrity,
not the independent truth of the source. `--no-llm-synthesis` forces the optional model off. We ship
no key; with no key the declared third-party cost stays $0.

## Review site (find, verify, compare)

```bash
uv run python scripts/build_site.py \
  --input ops-002=out/ops2-envelopes.jsonl \
  --input smoke-004=out/smoke4-envelopes.jsonl \
  --out site
# open site/index.html (self-contained: data embedded, no CDN, works from file://)

# optional: headless screenshots + overflow/JS-error check
uv run --with playwright playwright install chromium
uv run --with playwright python scripts/screenshot_site.py --site site --org 931015737 --compare 931015737,833384252
```

Search by name, organisation number, town or industry. A company page shows the cited synthesis,
what changed, what is unknown, every fact with its value, availability state, source link and
retrieval time, the financial history (missing values shown as "n/r", never zero), and the source list
with claim spans and hashes. Compare puts two companies side by side. The layout is responsive
(desktop and phone) and keyboard/screen-reader friendly.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.

## Revision 1 (safety fixes)

- Identity: a page that names a different legal entity containing this company's name (e.g. "Consto Bergen AS" for CONSTO AS) is no longer accepted as exact-company proof without the organisation number, in both the registry-website gate and the legal-name-plus-address proof.
- Batch resilience: official-fetch, enrichment, refresh-diff, synthesis and LLM-polish failures are caught per company and recorded as `failed`; companies missing from the bulk snapshot get a stub profile instead of aborting; a count mismatch only warns.
- Output contract: every envelope now also carries the top-level `run`, `claims`, `evidence`, `errors` and `operations` fields from OUTPUT_CONTRACT.md, and each module state has an `availability` in the six required states. The nested `profile` is unchanged.
- Resume: `--resume` reuses a checkpoint only when its fingerprint (git commit, code hash, input hash, modules, options) matches.
- Smoke test at this commit: `out/smoke5/report.json` (100/100 envelopes, all 8 validation checks passed).

## Revision 2 (recall, same identity gate)

- More website leads, all still gated by org number or legal name + registered address on the site itself:
  Nav ads' declared `employer.homepage` (only ads whose `employer.orgnr` is exact; Nav now runs before website
  discovery), Brreg sub-units' own `hjemmeside`/`epostadresse`, `.no`/`.com` swaps of the Brreg e-mail domain,
  and brand-shaped legal-name runs (`jeistad.no`). A brand-shaped domain is published only with homepage proof,
  so chain/franchise sites that list the entity on a store page stay unpublished.
- More identity pages per candidate (host-label name match, up to 4 contact/about/privacy pages with fallbacks).
- Optional headless-Chromium fallback for JS-only sites (`src/norway_company_agent/js_render.py`, extra
  `browser`). `run_signalpost.sh` installs it best-effort; without it the run is unchanged and static.
  `SIGNALPOST_JS_FALLBACK=0` disables it. The report's `operations.js_render` shows whether it ran.
- Fixes: Nav cache temp-file race (it failed the whole Nav module in ops-003), list-valued role names in the
  change description, and the review site tolerating a failed synthesis.
- Measurements: `notes/website-discovery.md` (Revision 2). Smoke test at this commit: `out/smoke6/report.json`.

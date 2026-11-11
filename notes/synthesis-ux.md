# Synthesis (12 pts) + UX (8 pts): smoke-004 / ops-002

## What the contract says (read 2026-11-10)
- Evaluation contract (https://builderr.ai/docs/signalpost-evaluation-harness.md), scoring v2: "Decision-useful
  synthesis — 12" and "UX and interaction — 8". The brief (https://builderr.ai/starter-briefs/signalpost.md)
  explains them. Synthesis: "Is the summary useful? It should explain the company, changes and unknowns without
  making unsupported claims." UX: "Is it easy to use and verify? A user should be able to find, compare and
  verify the information on desktop and mobile." The JBOX side prize uses the same UX tasks: "find a company,
  compare two, check a source, on laptop and phone".
- **Neither document names an output field or format for synthesis or UX.** The envelope must contain "legal
  identity, claims, evidence references, availability states, source snapshots, refresh metadata and errors".
  The profile should help someone "understand a company before they apply, sell, partner or invest: what it
  does, who leads it, where it operates, how its latest filed numbers look, whether it appears to be hiring,
  and what dated public activity the checked sources reveal." The agent playbook says "An LLM may summarise
  supported claims … It must not decide exact identity, invent a missing field".
- Our choice, given that gap: a top-level `synthesis` object in every envelope (it is also kept in the
  profiles JSONL), plus top-level `changes` and `refresh` objects. UX is a static site built from the same files.

## Synthesis: `src/norway_company_agent/synthesis.py` (`evidence_template_synthesis_v2`)
Deterministic, template-based, keyless. It runs as post-processing in the batch command
(`attach_refresh_and_synthesis`): about 0.8–1.1 ms per company (77 ms for smoke-004 and about 110 ms for
ops-002, per 100 companies), with no network.

Envelope `synthesis` fields:
- `headline`: the first identity statement with its citations.
- `summary`: plain text with inline `[S#]` markers, plus the what-changed sentence.
- `statements[]`: `{id, section, text, citations[S#], facts[{pointer, value}], derived?}`. `pointer` is a
  JSON pointer into the profile, so a reader (or the validator) can re-read the exact value.
- `sections[]` in this order: what_it_does (legal form, NACE code + Norwegian label, the original
  *vedtektsfestet formål* and *aktivitet* quoted, founding date, status flags, the company-reported meta
  description and services headings from the verified site), size (employees; "flagged but not published" and
  "no registered employees" are told apart), financials (latest SELSKAP period with from/to dates and
  currency, missing items listed as "not reported (not zero)", year-on-year revenue % and net result, KONSERN
  revenue when filed), people (all active roles with English+Norwegian labels; auditor and accountant org
  numbers), locations (business address, sub-unit count and towns, "checked, zero found", group
  parent/subsidiary with ownership basis), digital_footprint (verified site with *why* it is verified,
  gated social profiles, contact), hiring (careers page, site postings, Nav ads tied by org number),
  recent_activity (latest dated news), what_changed.
- `what_changed`: `{status: no_previous_snapshot | not_in_previous_snapshot | compared, count, text, previous}`.
  With `--previous-profiles`, each change from `refresh.diff_profile` becomes a cited statement (roles
  added/removed by name, a new accounts period, ad/news ids added/removed). The latest registered role
  change date is always stated, as a dated registry event.
- `unknowns[]`: one entry per info type that is not available: `{info_type, label, state, reason,
  checked_source, retrieved_at}`. States use the contract set (not_available / not_applicable / ambiguous /
  blocked / failed). `availability{}` holds the state of all 13 info types. `unknowns_text` is the one-line
  version.
- `sources[]`: `{id, label, source_url, source_class, retrieved_at, content_sha256, evidence_pointer,
  claim_span?}`. Every one is taken from evidence already in the profile.
- `validation`: the support check (below). `llm_polish`: `not_configured` by default.

**Support check** (`validate_synthesis`, also run by `validate_envelopes` as
`synthesis_present_and_every_statement_supported`):
1. Each published statement exactly matches its fixed template regenerated from the profile,
   including its text, section, facts, derived values, and citation bindings. This prevents a
   fabricated predicate from passing just because it reuses a known name or number.
2. Source IDs, evidence pointers, URLs, timestamps, hashes, class and claim spans must match
   their regenerated source records. Shared URLs alone do not bind facts to a source.
3. Fact pointers and values are rechecked, as are numeric values and derived calculations.
   Projections must match the specific template's projection, not merely numeric token subsets.
4. Any stored LLM narrative is checked again before envelope validation passes.
A failing statement is omitted and recorded in `validation.dropped`. This is a closed-template
integrity check. It is not a general semantic entailment system and cannot independently establish
that the upstream source or extractor is correct.

**Optional LLM selection** (`polish_with_llm`): runs only if `SIGNALPOST_LLM_API_KEY` is set
(OpenAI-compatible `/chat/completions`; `SIGNALPOST_LLM_BASE_URL`, `SIGNALPOST_LLM_MODEL`
[default gpt-4o-mini], `SIGNALPOST_LLM_TIMEOUT_S`). `--no-llm-synthesis` disables it.
The model may select and reorder complete supported statements, but must copy their full text
and original citations verbatim. The checker normalizes whitespace only. Paraphrasing,
negation, changing predicates, dropping a clause or citation, and adding free-form unknowns
are rejected. The deterministic statements always remain available. No paid or live LLM calls
were used for the tests; fake transports exercise both acceptance and rejection.

## UX: `scripts/build_site.py` + `scripts/site_template.html`
- `uv run python scripts/build_site.py --input ops-002=out/ops2-envelopes.jsonl --input smoke-004=out/smoke4-envelopes.jsonl --out site`
  writes `site/index.html` (data embedded; no external script, style or font; works from `file://`) and
  `site/data.json`. The batch command can do the same with `--site-dir site`.
- Views (hash routes, so links and back/forward work):
  - list `#/`: search by name, org number (spaces allowed), town or industry; run filter; "verified website /
    hiring / news" filters; sort by name, revenue or employees.
  - company `#/c/<org>`: KPIs with period and source, the cited synthesis (citation buttons jump to and
    highlight the source entry), what changed, unknowns with state chips and reasons, all facts grouped
    (value · state · source link · retrieval time · claim span), the financial history table (n/r, never 0),
    and the sources list with hashes.
  - compare `#/compare/<a>/<b>`: two pickers (datalist), 21 aligned rows, each with a value or a state chip.
  - `#/about`: how to verify, and what each state means.
- Accessibility: semantic landmarks, skip link, labelled inputs, `aria-pressed` compare toggles, an
  aria-live result count, visible focus rings, ≥40 px touch targets, dark mode via prefers-color-scheme,
  reduced-motion support.
- Responsive: under 720 px, fact rows become stacked cards and compare rows become 2-column cards.
- Checked with headless Chromium (`scripts/screenshot_site.py`; it needed `apt-get install libatk1.0-0t64
  libxcomposite1 libxdamage1 libatspi2.0-0t64` in this sandbox). 5 views × desktop 1366×900 and mobile
  390×844: 0 px horizontal overflow, 0 JS errors. Found and fixed: long social URLs in the summary overflowed on
  mobile (now `overflow-wrap:anywhere`), and the facts cards were too tall on mobile (now a grid).
  Screenshots: `site/screenshots/{desktop,mobile}-{list,search,company,company-top,compare,about}.png`.
- Size: about 16 KB per company (200 companies → 3.2 MB single file). That is fine for 1,000 companies (about
  16 MB). If it ever matters, the next step is lazy-loading data.json per company.

## Runs (final code)
| run | envelopes | validation | requests (+Nav sync) | in-process / wall | statements | dropped | material changes vs previous |
|---|---|---|---|---|---|---|---|
| smoke-004 (prev smoke-003) | 100 | passed (6/6 checks) | 998 (+3) | 83.0 s / 102.0 s | 1,152 (avg 11.5, min 10) | 0 | 0 |
| ops-002 (prev ops-001) | 100 | passed (6/6 checks) | 1,300 (+3) | 99.0 s / 118.2 s | 1,363 (avg 13.6, min 10) | 0 | 0 |

Synthesis cost: 91 ms (smoke) and 116 ms (ops) per 100 companies; $0; no requests. Published websites:
smoke 8, ops 40, the same as the previous runs.

Zero changes is the honest outcome: each previous snapshot is under an hour old. An earlier ops-002 attempt
showed what goes wrong otherwise. austevollauto.no had a transient connection failure, so the website
disappeared and 4 "changes" appeared. Those were a failed re-check, not a change, and a false-change rate is
measured. `refresh.classify_refresh_failures` now marks website and site-signal diffs as `material: false,
change_type: source_unreachable` when the previously verified domain is unreachable, blocked or erroring this
run. They stay in `changes` (nothing is hidden) but are not counted. The synthesis states "could not be reached
in this run … not re-published; a failed re-check, not a confirmed change". The old value is **not**
re-published without fresh proof. Empty-to-empty diffs ("" vs null) are no longer reported. A tracked
`registry_live.employees` field was tried and removed: it double-counted the public refresh fixture's
expected `registry.employees` change (qualification false_positive 1). A regression test now pins the fixture
to exactly its 2 expected changes.

## Spot-check (10 syntheses: ops 931015737, 833384252, 916368127, 930011940, 918720480, 931883798; smoke 985589003, 927356783, 888567232, 982669251)
Every statement was read against its cited evidence. Independently re-fetched live from Brreg:
- regnskap/931015737: revenue 3,421,111 / 3,383,867, annual result 69,694 and equity 482,486 match.
- roller/833384252: DAGL, LEDE, 4 MEDL, VARA and REVI names match.
- konsernstruktur/918720480: parent INDIGO GRUPPEN AS, 100%, matches.
No unsupported claim was found. Wording fixed during the check: "directly owned subsidiaries" became "subsidiaries
directly below it in the group tree" (the tree does not prove ownership level), and a doubled ”. was removed.
Known limits: the "registered in <town>" wording uses the business-address post town; NACE labels stay in
Norwegian (quoted, not translated, to avoid unsupported paraphrase); the Brreg `retrieved_at` in these outputs
is UTC ISO.

## Files
- new: `src/norway_company_agent/synthesis.py`, `scripts/build_site.py`, `scripts/site_template.html`,
  `scripts/screenshot_site.py`, `tests/test_synthesis.py`, `notes/synthesis-ux.md`, `site/` (built output)
- changed: `scripts/run_competition_batch.py` (`--previous-profiles`, `--no-llm-synthesis`, `--site-dir`,
  `attach_refresh_and_synthesis`, `refresh_and_synthesis` report block), `src/norway_company_agent/batch.py`
  (top-level `synthesis`/`refresh`/`changes` in envelopes and the synthesis support check in validation),
  `src/norway_company_agent/refresh.py` (tracks `website.official_website`, plus
  projected `nav_jobs.posting_ids` and `site_signals.news_post_urls`; `classify_refresh_failures`; empty-equals-empty, compared by stable id, skipped when the
  module is absent on one side), `README.md`.
- Tests: 152 passed, 5 subtests (141 before + 11 new). `scripts/run_refresh_replay.py`: qualification_passed true, 0 false positives, idempotent.

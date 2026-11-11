# Review follow-up: identity, registry and evidence scope

Base: `f3d68cf6422cfb4ba9e66bae2e2e9988f365cf00` on the existing main branch.
The fixes were prepared on a separate local branch, without overwriting the earlier
review checkouts. No deployment, formal entry, paid API call or repository push was
performed as part of this local repair.

## Changes

- Preserve H1, explicit legal-name and footer identity boundaries before flattening
  HTML. A related entity such as Bergen Consto AS must not become CONSTO AS simply
  because the target name is a suffix. Exact organisation-number proof remains an
  independent identity signal; navigation text is not treated as an identity node.
- Select current identity and registry flags field by field from available,
  organisation-matched live data. Retain original bulk evidence and label historical
  fallbacks. Missing or invalid flags mean unknown, not false. The downstream
  identity gate, profile, summary and review site use the selected name/form.
- Disable browser rendering completely until an independently enforced connection-
  level public-only network boundary exists. Static discovery continues. Defensive
  URL/DNS checks reject local names, private answers, mixed public/private answers
  and resolution errors, but are not claimed to stop DNS rebinding on their own.
  `SIGNALPOST_JS_FALLBACK=1` cannot restore rendering. The shell entry point no longer
  installs Chromium. This reduces expected recall on JS-only sites.
- Compare explicitly unordered outer collections by stable identities and content.
  Preserve duplicates and nested list order so real amendments remain observable.
  Track bankruptcy, liquidation and forced-dissolution changes, including false.
- Record news collection/window completeness and truncation. A change in observed
  URLs without proven complete coverage is not evidence of publication or deletion.
  Sitemap lastmod, Atom updated and HTML dateModified remain modification dates.
- Qualify NAV counts as at least confirmed advertisements, and restrict "newest" to
  the checked ads with publication dates. Explain incomplete coverage. News recency
  likewise applies only to observed publication dates.
- Reject social post/video/content routes as company profiles, including Facebook
  photo/story/watch routes and TikTok video paths, even when listed in sameAs.
- Make batch LLM calls opt-in with `--llm-synthesis` plus a configured key. Preserve
  provider usage and explicit USD charges; unknown attempted-call costs are null,
  including failed requests or rejected narratives. Keep known subtotals separate.
- Rebuild review-site summaries with the current templates rather than blindly
  displaying old cached claims. Old historical change records cannot be revalidated
  without both source snapshots and must not become fresh confirmed change claims.

## Reproduction

After dependencies and pytest have been cached:

```bash
uv run --offline --with pytest python scripts/run_offline_tests.py -q
uv run --offline python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json --output /tmp/refresh-review.json
uv run --offline python -m compileall -q src scripts tests
bash -n run_signalpost.sh
git diff --check
```

`run_offline_tests.py` uses the bundled public-suffix snapshot, removes the ambient
LLM key and rejects unmocked DNS/socket operations in the test process. Its existing
subprocess fixtures only transform local JSON. This test helper is not an OS-level
network sandbox or production firewall.

## Validation boundaries

Final local verification: **472 tests and 76 subtests passed**, independently rerun
with the offline runner. Compilation, shell syntax and whitespace checks passed.
The public refresh replay found both expected changes, no false positives or
misses, complete provenance and an idempotent rerun. Each saved 100-company corpus
passed all envelope/synthesis checks with zero dropped statements and zero model
requests. A separate final review found no remaining blocking issue in this diff.

The tests use controlled HTML, registry, feed, DNS and model responses. The saved
100-company smoke6 and smoke8 corpora are reprocessed offline for envelope integrity
and review-site generation. Their source data is historical, not a fresh crawl.
Template validation checks traceability and deterministic consistency, not whether
an upstream source is true. The public refresh replay is a small fixture, not formal
competition qualification. No real Chromium private-network probe, live SSRF
exploitation, paid-model billing check or full live-service crawl was performed.
Browser safety in this version comes from not launching the browser.

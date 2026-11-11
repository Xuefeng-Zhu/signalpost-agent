# Iterative correctness review after 5dab332

Base: `5dab3323c461df8851e51bf8b75a3cb3818ff592`, checked on main on November 10, 2026.
This change set addresses social attribution, NAV pagination/checkpoint integrity, and
synthesis factual integrity. It does not deploy the agent or submit a competition entry.

## Fixes and review iterations

- Bind `Organization.sameAs` to the target organisation number, complete legal name,
  or compatible brand plus verified homepage. Unrelated authors, publishers and graph
  organisations remain unpublished candidates. Structured identity conflicts cannot
  be rescued by a matching handle on another page.
- Independent review then found that ordinary outbound social handles still used
  substring matching. Restrict those links to whole company/domain aliases and narrow
  official/Facebook profile conventions. Preserve legal-form distinctions such as
  AS versus ASA when checking structured names.
- A capped, looping, invalid or unsafe NAV page sequence is incomplete. No partial
  segment updates are merged and the last complete cache checkpoint is not advanced.
  An empty page with a next link is not a terminal page. Safe same-origin pagination
  supports both query cursors and page-ID paths. The latter matches NAV’s
  [official FeedService implementation](https://github.com/navikt/pam-stilling-feed/blob/a46daf907670ea3339a74ba200134ad41668a4c0/src/main/kotlin/no/nav/pam/stilling/feed/FeedService.kt).
- Independent review then found malformed dictionary-shaped NAV entries were silently
  skipped. Require a usable ad UUID, documented status and timezone-aware timestamp
  before accepting a page. Compare timestamp instants rather than lexical offsets.
  Unusable cache horizons trigger a clean full-window rebuild, not a skipped sync.
- Optional LLM output may only select and reorder complete deterministic statements,
  verbatim with their unchanged citations. It cannot invent predicates or negate facts
  just because numbers and source markers match. Saved narratives are rechecked too.
- Regenerate deterministic statement text, fact projections and citation bindings in
  validation. Source URLs, pointers, retrieval times, hashes, class and spans must match
  the specific source record. Separate records no longer share a source ID solely
  because their URL/hash/label match. Mixed dated/undated news retains its valid dated
  subset rather than dropping that statement.

## Validation

Final result: **334 tests and 5 subtests passed**, including 86 new regression cases relative
to the 248-test base. Compile and whitespace checks pass. Commands from the repository root:

```sh
UV_CACHE_DIR=/tmp/signalpost-uv-cache uv run --offline --frozen --with pytest pytest -q
UV_CACHE_DIR=/tmp/signalpost-uv-cache uv run --offline --frozen python -m compileall -q src scripts
UV_CACHE_DIR=/tmp/signalpost-uv-cache uv run --offline --frozen python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json --output out/remaining-fixes/refresh-replay.json
git diff --check
```

The deterministic refresh fixture reports both expected changes, zero false positives
or negatives, complete provenance and idempotence. No live LLM or paid service was used.

Two saved 100-company datasets (smoke5 and the previously captured live smoke) were
reprocessed without network: 200 terminal envelopes, all eight validation checks pass
for each dataset, zero synthesis failures or dropped statements, no self-diff changes,
and both static review sites rebuild. This reuses saved observations. It does not
re-crawl websites or establish current social/profile coverage.

The final independent review exercised 102 synthesis mutations, 13 further social
attribution cases and NAV pagination boundary probes. Original repros are covered by
regressions. The final scoped pass reported no new actionable findings; this is not
proof that the whole repository is bug-free.

## Deliberate limits

- Social ownership still relies on source declarations and conservative whole-name
  matching. A legitimate shortened handle or brand-only JSON-LD without a verified
  homepage may remain a candidate instead of being published.
- NAV cannot complete a segment that consistently exceeds its configured page cap.
  It reports a failure and retries from the last complete checkpoint instead of
  silently dropping events. Future unsupported feed schemas fail closed.
- Synthesis validation proves consistency with fixed templates and supplied evidence,
  not independent truth of the upstream source or a general semantic entailment proof.
- New tests and replays are offline; no claim of fresh live coverage, formal competition
  qualification, deployment or submission is made.

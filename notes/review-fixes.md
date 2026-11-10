# Review fixes against 448937bf

Base: `448937bf6cbd12f06ebeefbef74c255337836fbf` (main, checked before work).
At the time of the initial review, these changes were local only. Publication of this
fix set does not deploy the agent or submit a formal competition entry.

## Correctness fixes

- Preserve conflicting organisation-number evidence through discovery and the final identity gate.
- Compare complete legal-name phrases, including prefix qualifiers, case-insensitive suffixes,
  A/S spelling, and AS versus ASA. A shared address cannot override a conflicting entity name.
- Compare live registry values when available, together with their actual evidence source.
  A failed live lookup never falls back to an older bulk value and pretends the company changed.
- Keep failed or incomplete rechecks as non-material availability events. This includes official
  APIs, NAV detail failures/caps, company-site errors, and switches between live and bulk sources.
  Successful empty collections remain distinguishable from failed lookups.
- Record untruncated NAV detail-failure counts and collection completeness, so an error outside
  the displayed rejection sample cannot create a false removal.
- Bind resume checkpoints to bulk content, source-code and input hashes, requested modules,
  website/site-signals limits, NAV window and cache identity. A checkpoint may recognize its own
  exact cache output but not unrelated later cache edits. Full resumes do not resync NAV.
- Validate optional previous snapshots and checkpoint contents before use, including nested
  metadata. A malformed optional input is reported and ignored rather than aborting the batch.
- Give failed synthesis the same object-shaped unknowns as normal synthesis; count failures
  explicitly. Persist the report before optional site rendering and report renderer failures.

## Regression checks

Run from the repository root:

```sh
UV_CACHE_DIR=/tmp/signalpost-uv-cache uv run --frozen --with pytest pytest -q
uv run --frozen python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/review-fixes/refresh-replay.json
```

Results: **248 tests and 5 subtests passed**, including 91 new regression cases. The deterministic
refresh fixture reports both expected changes, zero false positives/negatives, complete provenance,
and an idempotent rerun. `git diff --check` also passes.

The saved smoke5 dataset was separately revalidated without network or LLM calls: 100/100 envelopes
pass all contract/support checks, all eight previously published websites still pass the new gate,
and comparing identical snapshots produces zero changes. This is a replay of saved observations,
not evidence of fresh source availability or a precision guarantee for arbitrary companies.

## Same-sample live smoke and limitations

The live run used exactly the original smoke5 organisation input and bulk bytes:

- Organisations SHA-256: `51a96f25f79532b9ba285304d540887e0f17410907909dbe6c85c0b41b8341d3`.
- Bulk SHA-256: `1a4a807310926763dc77dcfcd2507c312343ddfa01a83eea86ecdb6ce56e6ded`.
- Previous snapshot: smoke5 envelopes. The run compared all 100 companies, not zero.
- 100/100 terminal envelopes; all eight contract/support checks passed; no synthesis failures.
- Live registry, financials, roles and locations were available for all 100 companies.
- 305.5 seconds including the bulk scan; 902 requests including the shared NAV feed sync;
  declared third-party cost $0; optional LLM explicitly disabled.
- No confirmed material changes, and 33 non-material source recheck events.

The current cloud execution environment could not resolve the company-site hostnames:
all 362 candidate records report `dns_not_resolved`, and no website HTTP requests were made.
Thus this live run **does not establish website coverage parity**. Website regression coverage
comes from controlled tests and the saved smoke5 replay, where all eight sites remain accepted.
No alternate route or paid service was used to bypass this limitation.

The live collection began before the final partial-collection/checkpoint hardening. Its original
code/input fingerprint is preserved in `out/review-fixes/smoke/report.json`. The final code then
recomputed refresh, synthesis, envelopes and the static review site from those saved observations
without new network or LLM calls. That final revalidation also passes all eight checks, and records
its separate code digest in `out/review-fixes/final-revalidation/report.json`. Reproduce it with:

```sh
uv run --frozen python out/review-fixes/revalidate_live.py
```

The ignored `out/review-fixes/` results and revalidation script were distributed in the
separate review archive; they are not included in this source commit. Before publication,
the full test suite (248 tests and 5 subtests) and deterministic refresh fixture were rerun
successfully on November 10, 2026.

The newest hardening is additionally covered by fault-injection regression tests. Neither test
results nor the demo fixture are a formal competition qualification or submission.

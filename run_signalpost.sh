#!/usr/bin/env bash
# One-command Signalpost run.
# Usage: ./run_signalpost.sh <companies.jsonl> [out_dir] [previous_profiles.jsonl]
# Installs pinned deps (uv sync --frozen), fetches the Brreg bulk snapshot if missing,
# then emits exactly one terminal envelope per input company.
set -euo pipefail
cd "$(dirname "$0")"
IN="${1:?usage: ./run_signalpost.sh <companies.jsonl> [out_dir] [previous_profiles.jsonl]}"
OUT="${2:-out/run}"
PREV="${3:-}"
RUN_ID="${SIGNALPOST_RUN_ID:-run-$(date -u +%Y%m%dT%H%M%SZ)}"
BULK="${SIGNALPOST_BULK:-brreg-enheter.csv}"
mkdir -p "$OUT"
uv sync --frozen
if [ ! -s "$BULK" ]; then
  curl -fsSL 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o "$BULK.tmp" && mv "$BULK.tmp" "$BULK"
fi
COUNT=$(grep -c . "$IN" || true)
ARGS=(--organisations "$IN" --bulk "$BULK"
      --output "$OUT/envelopes.jsonl" --profiles-output "$OUT/profiles.jsonl"
      --report "$OUT/report.json" --run-id "$RUN_ID" --expected-count "$COUNT"
      --site-dir "$OUT/site")
if [ -n "$PREV" ]; then ARGS+=(--previous-profiles "$PREV"); fi
exec uv run python scripts/run_competition_batch.py "${ARGS[@]}"

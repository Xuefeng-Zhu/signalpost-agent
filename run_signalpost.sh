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
# Optional headless-Chromium fallback for JS-only sites (SIGNALPOST_JS_FALLBACK=0 turns it off).
# Best effort: if the extra or the browser cannot be installed, the run continues without it.
UV_EXTRA=()
if [ "${SIGNALPOST_JS_FALLBACK:-1}" != "0" ] && uv sync --frozen --extra browser; then
  UV_EXTRA=(--extra browser)
  timeout 180 uv run --frozen ${UV_EXTRA[@]+"${UV_EXTRA[@]}"} playwright install chromium-headless-shell >/dev/null 2>&1 \
    || echo "note: chromium not installed; JS-only sites are checked statically" >&2
else
  uv sync --frozen
fi
if [ ! -s "$BULK" ]; then
  curl -fsSL 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o "$BULK.tmp" && mv "$BULK.tmp" "$BULK"
fi
COUNT=$(grep -c . "$IN" || true)
ARGS=(--organisations "$IN" --bulk "$BULK"
      --output "$OUT/envelopes.jsonl" --profiles-output "$OUT/profiles.jsonl"
      --report "$OUT/report.json" --run-id "$RUN_ID" --expected-count "$COUNT"
      --site-dir "$OUT/site")
if [ -n "$PREV" ]; then ARGS+=(--previous-profiles "$PREV"); fi
exec uv run --frozen ${UV_EXTRA[@]+"${UV_EXTRA[@]}"} python scripts/run_competition_batch.py "${ARGS[@]}"

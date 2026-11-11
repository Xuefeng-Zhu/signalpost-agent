from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "ambiguous",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    for org in missing:
        # Never abort the batch: a company absent from the bulk snapshot (deleted, newly registered,
        # stale snapshot) still gets a profile; the live registry module fills identity if it can.
        found[org] = {
            "organisation_number": org,
            "name": None,
            "legal_form": None,
            "employees": None,
            "bankrupt": None,
            "liquidating": None,
            "municipality": None,
            "municipality_number": None,
            "industry_code": None,
            "industry_label": None,
            "website": None,
            "latest_submitted_accounts": None,
            "registry_bulk_missing": True,
            "evidence": {
                "registry": evidence(
                    "registry", "not_found", "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    retrieved_at=retrieved_at, content_sha256=snapshot_sha256, source_row_key=org,
                    note="organisation number not present in the registry bulk snapshot",
                ),
                "accounting_obligation": evidence(
                    "accounting_obligation", "not_found", "official_rule_interpretation",
                    "https://www.brreg.no/en/submission-of-annual-accounts/reporting-obligations-to-the-register-of-company-accounts/",
                    retrieved_at=retrieved_at, note="no registry bulk row to assess",
                ),
            },
        }
    return [found[org] for org in requested], {
        "missing_from_bulk_snapshot": missing,
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "ambiguous":
        return "ambiguous"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    envelope = {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
    }
    body = profile
    if "synthesis" in profile or "refresh" in profile:
        body = dict(profile)
        envelope["synthesis"] = body.pop("synthesis", None)
        envelope["refresh"] = body.pop("refresh", None)
        envelope["changes"] = body.get("changes") or []
    envelope["profile"] = body
    envelope.update(contract_fields(profile, module_states, run_id=run_id, started_at=started_at, completed_at=completed_at))
    return envelope


# Mapping from internal module states to the six availability states the challenge requires.
AVAILABILITY = {
    "complete": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "ambiguous": "ambiguous",
    "blocked_policy": "blocked",
    "blocked_robots": "blocked",
    "source_error": "failed",
    "budget_exhausted": "failed",
    "submission_error": "failed",
}
AVAILABILITY_STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


def _small(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool)) and len(str(value)) <= 500


def contract_fields(profile: dict[str, Any], module_states: dict[str, dict], *, run_id: str, started_at: str, completed_at: str) -> dict[str, Any]:
    """Top-level fields of OUTPUT_CONTRACT.md (run, claims, evidence, errors, operations), derived
    from the same evidence records as the nested profile so the two can never disagree."""
    records = profile.get("evidence", {}) or {}
    evidence_rows: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for module, state in module_states.items():
        availability = AVAILABILITY.get(state["state"], "failed")
        state["availability"] = availability
        record = records.get(module) or {}
        ev_id = f"ev-{module}"
        if record:
            evidence_rows.append({
                "id": ev_id,
                "field": module,
                "source_url": record.get("source_url"),
                "source_class": record.get("source_class") or record.get("source_type"),
                "retrieved_at": record.get("retrieved_at"),
                "content_sha256": record.get("content_sha256"),
                "as_of": record.get("as_of"),
                "claim_span": record.get("note"),
            })
        value = record.get("value")
        claim = {
            "field": module,
            "availability": availability,
            "confidence": 1.0 if availability == "available" and str(record.get("source_class") or "").startswith("official") else None,
            "evidence_ids": [ev_id] if record else [],
        }
        if _small(value):
            claim["value"] = value
        else:
            claim["value_ref"] = f"profile.evidence.{module}.value"
        claims.append(claim)
        if availability == "failed":
            errors.append({"field": module, "state": state["state"], "message": record.get("note") or "module produced no terminal record"})
    official = (records.get("website") or {}).get("official_website")
    if official:
        ev = dict(official.get("evidence") or {})
        ev["id"] = "ev-official_website"
        ev["field"] = "official_website"
        evidence_rows.append(ev)
        claims.append({
            "field": "official_website",
            "value": official.get("value"),
            "availability": "available",
            "confidence": official.get("confidence"),
            "identity_method": official.get("identity_method"),
            "evidence_ids": ["ev-official_website"],
        })
    for item in profile.get("errors") or []:
        errors.append({"field": item.get("stage"), "state": "failed", "message": item.get("error")})
    from .synthesis import synthesis_cost
    cost = synthesis_cost(profile.get("synthesis"))
    metrics = profile.get("run_metrics") or {}
    return {
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed" if not any(s["state"] == "submission_error" for s in module_states.values()) else "failed",
        },
        "claims": claims,
        "evidence": evidence_rows,
        "errors": errors,
        "operations": {
            "requests": metrics.get("requests"),
            "bytes": metrics.get("bytes"),
            "runtime_ms": None,  # per-company wall time is not separable in the concurrent run; see report.json operations
            "third_party_cost_usd": cost["cost_usd"],
            "llm_accounting": cost,
        },
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
        "contract_fields_present": all(all(key in item for key in ("run", "claims", "evidence", "errors", "operations")) for item in envelopes),
        "all_availability_states_valid": all(
            state.get("availability") in AVAILABILITY_STATES for item in envelopes for state in item.get("modules", {}).values()
        ),
    }
    if any("synthesis" in item for item in envelopes):
        from .synthesis import validate_synthesis
        unsupported = []
        for item in envelopes:
            synthesis = item.get("synthesis")
            if not synthesis:
                unsupported.append(item.get("organisation_number"))
                continue
            if not validate_synthesis(synthesis, {**item["profile"], "changes": item.get("changes") or []})["passed"]:
                unsupported.append(item.get("organisation_number"))
        checks["synthesis_present_and_every_statement_supported"] = not unsupported
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)

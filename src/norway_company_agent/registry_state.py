"""Field-level registry selection with explicit provenance and missing-value semantics."""
from __future__ import annotations

from typing import Any

BULK_KEYS = {
    "name": "navn", "legal_form": "organisasjonsform.kode",
    "bankrupt": "konkurs", "liquidating": "underAvvikling",
    "forced_dissolution": "underTvangsavviklingEllerTvangsopplosning",
    "employees": "antallAnsatte", "business_address": "forretningsadresse",
    "latest_submitted_accounts": "sisteInnsendteAarsregnskap",
}
BOOLEAN_FIELDS = {"bankrupt", "liquidating", "forced_dissolution"}


def registry_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def registry_field(profile: dict[str, Any], field: str) -> dict[str, Any]:
    """Prefer an available, entity-matched live observation, otherwise a labelled snapshot.

    ``value`` remains the source's original value for evidence-pointer validation.
    Use ``registry_boolean`` for flags: absent/invalid data must never imply false.
    A legacy bulk row without an org number is accepted as the profile's snapshot;
    live rows require the explicit org number returned by the entity endpoint.
    """
    bulk_key = BULK_KEYS[field]
    records = profile.get("evidence") or {}
    target = str(profile.get("organisation_number") or "").strip()
    live = records.get("registry_live") or {}
    lv = live.get("value") or {}
    live_ok = (live.get("status") == "available" and bool(target)
               and str(lv.get("organisation_number") or "").strip() == target)
    def known(value: Any) -> bool:
        return registry_boolean(value) is not None if field in BOOLEAN_FIELDS else value is not None and value != "" and value != {}
    if live_ok and known(lv.get(field)):
        return {"value": lv[field], "module": "registry_live", "key": field,
                "record": live, "status": "available", "fallback": False, "note": None}
    bulk = records.get("registry") or {}
    raw = bulk.get("value") or {}
    bulk_org = str(raw.get("organisasjonsnummer") or "").strip()
    if bulk.get("status") == "available" and (not bulk_org or bulk_org == target) and known(raw.get(bulk_key)):
        return {"value": raw[bulk_key], "module": "registry", "key": bulk_key,
                "record": bulk, "status": "available", "fallback": True,
                "note": "Bulk registry snapshot; current live value unavailable or unverified."}
    return {"value": None, "module": None, "key": None, "record": {},
            "status": "not_available", "fallback": False,
            "note": "No available, entity-matched registry value."}

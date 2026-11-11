from __future__ import annotations

import json
from typing import Any

from .registry_state import BOOLEAN_FIELDS, registry_boolean


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.forced_dissolution": ("forced_dissolution",),
    "registry.bankrupt": ("bankrupt",),
    "registry.liquidating": ("liquidating",),
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
    "website.official_website": ("evidence", "website", "official_website", "value"),
}


# Only the tracked outer collections are unordered. Preserve nested list order
# (e.g. address lines), every value, and duplicates so amendments remain visible.
COLLECTION_IDENTITIES = {
    "financials.records": ("record_id", "account_type", "period"),
    "financial_history.years": (),
    "roles.roles": ("organisation_number", "name", "group_code", "role_code"),
    "locations.locations": ("organisation_number",),
    "website.social_links": ("platform", "url"),
}


def _canonical_collection(value: Any, identity: tuple[str, ...]) -> Any:
    def key(item: Any) -> tuple[str, str]:
        stable_id = [item.get(name) for name in identity] if isinstance(item, dict) else item
        return (json.dumps(stable_id, sort_keys=True, ensure_ascii=False),
                json.dumps(item, sort_keys=True, ensure_ascii=False))
    return sorted(value, key=key) if isinstance(value, list) else value


def _sorted_ids(path: tuple[str, ...], key: str):
    def read(profile: dict[str, Any]) -> Any:
        rows = _read(profile, path)
        if not isinstance(rows, list):
            return None
        return sorted({str(row.get(key)) for row in rows if isinstance(row, dict) and row.get(key)})
    return read


# Projected fields compare stable identifiers only, so retrieval-time noise is never a change.
PROJECTED_FIELDS = {
    "nav_jobs.posting_ids": _sorted_ids(("evidence", "nav_jobs", "value", "postings"), "uuid"),
    "site_signals.news_post_urls": _sorted_ids(("evidence", "site_signals", "value", "news_posts"), "url"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    return _observation(profile, field)[1]


def _observation(profile: dict[str, Any], field: str) -> tuple[Any, dict, str]:
    """Read the value and its actual provenance together, preferring current registry data.

    An attempted live read must never silently fall back to a frozen bulk value: that would
    turn a temporary API failure into an apparent reversal of the previous live observation.
    Legacy profiles without a live module still compare their bulk-derived fields.
    """
    module, key = field.split(".", 1)
    records = profile.get("evidence") or {}
    if module == "registry":
        live = records.get("registry_live")
        if isinstance(live, dict):
            value = live.get("value")
            if (live.get("status") != "available" or not isinstance(value, dict)
                    or str(value.get("organisation_number") or "").strip() != str(profile.get("organisation_number") or "").strip()):
                return None, live, "registry_live"
            if key == "municipality" and "business_address" in value:
                return _read(value, ("business_address", "kommune")), live, "registry_live"
            if key in value:
                return registry_boolean(value[key]) if key in BOOLEAN_FIELDS else value[key], live, "registry_live"
            if key in BOOLEAN_FIELDS:
                return None, live, "registry_live"
        value = _read(profile, TRACKED_FIELDS[field])
        return registry_boolean(value) if key in BOOLEAN_FIELDS else value, records.get("registry") or {}, "registry"
    value = PROJECTED_FIELDS[field](profile) if field in PROJECTED_FIELDS else _read(profile, TRACKED_FIELDS[field])
    if field in COLLECTION_IDENTITIES:
        value = _canonical_collection(value, COLLECTION_IDENTITIES[field])
    return value, records.get(module) or {}, module


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field in [*TRACKED_FIELDS, *PROJECTED_FIELDS]:
        old_value, previous_record, old_module = _observation(previous, field)
        new_value, record, new_module = _observation(current, field)
        if old_value == new_value or (old_value in (None, "", []) and new_value in (None, "", [])):
            continue
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
            "source_module": new_module,
            "previous_source_module": old_module,
        })
    # All callers, including replay tools, receive safe classifications, not just batch callers.
    return classify_refresh_failures(previous, current, changes)


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]


UNREACHABLE_OUTCOMES = {"unreachable", "blocked_robots", "timeout", "source_error"}


def classify_refresh_failures(previous: dict[str, Any], current: dict[str, Any], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate evidence availability from confirmed company changes for every module.

    A failed re-check is not a material change: the change stays in the output (with ``material: false`` and
    ``change_type: source_unreachable``) so nothing is hidden, but it is not counted or told as a change, and the
    old value is not re-published without fresh proof.
    """
    old_site = _read(previous, ("evidence", "website", "official_website", "value"))
    new_site = _read(current, ("evidence", "website", "official_website", "value"))
    unreachable = False
    if old_site and not new_site:
        from urllib.parse import urlparse
        host = (urlparse(old_site).hostname or "").removeprefix("www.")
        for candidate in _read(current, ("evidence", "website", "discovery", "candidates")) or []:
            if str(candidate.get("domain") or "").removeprefix("www.") == host and candidate.get("outcome") in UNREACHABLE_OUTCOMES:
                unreachable = True
        if _read(current, ("evidence", "website", "status")) in ("source_error", "blocked"):
            unreachable = True
    for change in changes:
        field = change["field"]
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        old_value, new_value = change.get("old_value"), change.get("new_value")

        def observed(source: dict, value: Any) -> bool:
            # A successful empty collection is evidence of zero results. HTTP 404 or missing
            # data alone is not evidence that every director/workplace/filing was removed.
            detail = source.get("value") or {}
            if isinstance(detail, dict):
                if detail.get("collection_complete") is False or detail.get("errors"):
                    return False
                if field == "site_signals.news_post_urls":
                    window = detail.get("news_posts_collection") or {}
                    if window.get("collection_complete") is True and (window.get("truncated") or window.get("window_complete") is False):
                        return False
                if field.startswith("nav_jobs."):
                    if detail.get("candidates_total", 0) > detail.get("candidates_checked", 0):
                        return False
                    if any("detail fetch failed" in str(r.get("reason")) for r in detail.get("rejected_candidates", []) if isinstance(r, dict)):
                        return False
            return source.get("status") == "available" or (
                source.get("status") == "not_found" and isinstance(value, list)
            ) or (not source and field.startswith("registry."))

        if unreachable and (change["field"].startswith("website.") or change["field"].startswith("site_signals.")):
            change["material"] = False
            change["change_type"] = "source_unreachable"
            change["previous_verified_value"] = old_site
        elif field.startswith("registry.") and change.get("source_module") != change.get("previous_source_module"):
            change["material"] = False
            change["change_type"] = "source_changed"
        elif not observed(record, new_value) or (new_value is None and old_value is not None):
            change["material"] = False
            change["change_type"] = "source_unreachable" if record.get("status") in UNREACHABLE_OUTCOMES | {"blocked", "failed", "blocked_policy"} else "source_unavailable"
        elif not observed(previous_record, old_value):
            change["material"] = False
            change["change_type"] = "previous_source_unavailable"
        elif field == "site_signals.news_post_urls" and not all(
            ((source.get("value") or {}).get("news_posts_collection") or {}).get("collection_complete") is True
            for source in (previous_record, record)
        ):
            # A feed, index or top-N window cannot prove publication or deletion.
            change["material"] = False
            change["change_type"] = "collection_window_changed"
            change["observed_added_ids"] = sorted(set(new_value or []) - set(old_value or []))
            change["observed_missing_ids"] = sorted(set(old_value or []) - set(new_value or []))
        else:
            change.setdefault("material", True)
            change.setdefault("change_type", "added" if change.get("old_value") in (None, "", []) else "removed" if change.get("new_value") in (None, "", []) else "updated")
    return changes

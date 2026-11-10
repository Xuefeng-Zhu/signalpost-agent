from __future__ import annotations

from typing import Any


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
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
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    readers = [(field, (lambda doc, path=path: _read(doc, path))) for field, path in TRACKED_FIELDS.items()] + list(PROJECTED_FIELDS.items())
    for field, reader in readers:
        old_value = reader(previous)
        new_value = reader(current)
        if old_value == new_value or (old_value in (None, "", []) and new_value in (None, "", [])):
            continue
        if field in PROJECTED_FIELDS and (old_value is None or new_value is None):
            continue  # module absent on one side (not run / not applicable): not a material change
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
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
        })
    return changes


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
    """Mark website changes caused by the previously verified site being unreachable this run.

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
        if unreachable and (change["field"].startswith("website.") or change["field"].startswith("site_signals.")):
            change["material"] = False
            change["change_type"] = "source_unreachable"
            change["previous_verified_value"] = old_site
        else:
            change.setdefault("material", True)
            change.setdefault("change_type", "added" if change.get("old_value") in (None, "", []) else "removed" if change.get("new_value") in (None, "", []) else "updated")
    return changes

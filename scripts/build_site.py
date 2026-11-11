#!/usr/bin/env python3
"""Build a static, self-contained review site from Signalpost profiles/envelopes.

    uv run python scripts/build_site.py --input ops-002=out/ops2-profiles.jsonl --input smoke-004=out/smoke4-profiles.jsonl --out site

Writes ``site/index.html`` (data embedded, no external assets, opens from file:// or any static host)
and ``site/data.json`` (the same data, for other tools). Every fact shown carries its source link,
retrieval time and availability state, straight from the profile evidence; nothing is computed
that the profile does not contain (the synthesis is the pipeline's own, already support-checked).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.synthesis import AVAILABILITY, LEGAL_FORMS, ROLE_LABELS, ROLE_ORDER, build_synthesis, check_narrative, validate_synthesis, SYNTHESIS_VERSION  # noqa: E402

from norway_company_agent.registry_state import registry_boolean, registry_field  # noqa: E402

TEMPLATE = Path(__file__).with_name("site_template.html")


def _ev(profile: dict, module: str) -> dict:
    return (profile.get("evidence") or {}).get(module) or {}


def _state(record: dict | None) -> str:
    return AVAILABILITY.get((record or {}).get("status") or "not_fetched", "failed")


def _src(record: dict | None, **extra: Any) -> dict:
    record = record or {}
    out = {"url": record.get("source_url"), "at": record.get("retrieved_at"), "cls": record.get("source_class") or record.get("source_type")}
    if record.get("claim_span"):
        out["span"] = str(record["claim_span"])[:240]
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


FRIENDLY_NOTES = {
    "No available, entity-matched registry value.": "not in the register for this organisation number",
}


def fact(group: str, label: str, value: Any, state: str, source: dict | None, note: str | None = None) -> dict:
    item = {"g": group, "l": label, "v": value, "s": state, "src": source}
    if note:
        item["n"] = FRIENDLY_NOTES.get(note, note)
    return item


def _num(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def slim(profile: dict, dataset: str, envelope: dict | None = None) -> dict:
    org = profile["organisation_number"]
    cached_synthesis = (envelope or {}).get("synthesis") or profile.get("synthesis") or {}
    changes = (envelope or {}).get("changes") or profile.get("changes") or []
    refresh = (envelope or {}).get("refresh") or profile.get("refresh")
    # Saved summaries are not trusted across template revisions. Rebuild from
    # evidence, and retain optional prose only if it exactly matches fresh facts.
    current_cache = cached_synthesis.get("method") == SYNTHESIS_VERSION
    if current_cache:
        try:
            current_cache = not validate_synthesis(cached_synthesis, {**profile, "changes": changes})["failures"]
        except (KeyError, TypeError, ValueError):
            current_cache = False
    synthesis = build_synthesis(profile, changes=changes if current_cache else None, refresh=refresh if current_cache else None)
    historical_changes = not current_cache and bool(changes or (refresh or {}).get("company_in_previous_snapshot"))
    if historical_changes:
        synthesis["what_changed"] = {
            "status": "historical_changes_not_revalidated", "count": None,
            "text": "Historical change records were not revalidated against both snapshots during site generation; no confirmed change claims are shown.",
        }
        changes = []
    polish = cached_synthesis.get("llm_polish") or {}
    if polish.get("status") == "accepted" and not check_narrative(polish.get("narrative"), synthesis):
        synthesis["llm_polish"] = polish
    reg = _ev(profile, "registry")
    raw = reg.get("value") or {}
    live = _ev(profile, "registry_live")
    facts: list[dict] = []
    s_reg = _src(reg)

    selected = {key: registry_field(profile, key) for key in (
        "name", "legal_form", "bankrupt", "liquidating", "forced_dissolution", "employees", "business_address")}
    def selected_fact(label: str, key: str, display: Any = None) -> dict:
        item = selected[key]
        return fact("Identity", label, item["value"] if display is None else display,
                    item["status"], _src(item["record"]), item["note"])

    # identity
    facts.append(selected_fact("Legal name", "name"))
    facts.append(fact("Identity", "Organisation number", org, _state(reg), s_reg))
    form = selected["legal_form"]["value"]
    facts.append(selected_fact("Legal form", "legal_form", f"{form} – {LEGAL_FORMS.get(form, '')}".strip(" –") if form else None))
    if raw.get("naeringskode1.kode"):
        facts.append(fact("Identity", "Industry (NACE)", f"{raw['naeringskode1.kode']} {raw.get('naeringskode1.beskrivelse') or ''}".strip(), _state(reg), s_reg))
    facts.append(fact("Identity", "Registered purpose (vedtektsfestet formål)", raw.get("vedtektsfestetFormaal") or None, "available" if raw.get("vedtektsfestetFormaal") else "not_available", s_reg))
    if raw.get("aktivitet"):
        facts.append(fact("Identity", "Registered activity", raw["aktivitet"], "available", s_reg))
    facts.append(fact("Identity", "Founded", raw.get("stiftelsesdato") or None, "available" if raw.get("stiftelsesdato") else "not_available", s_reg))
    addr = selected["business_address"]["value"] or {}
    if addr:
        facts.append(selected_fact("Business address", "business_address", ", ".join((addr.get("adresse") or []) + [f"{addr.get('postnummer') or ''} {addr.get('poststed') or ''}".strip()])))
    employees = selected["employees"]["value"]
    facts.append(selected_fact("Employees (register)", "employees"))
    flag_values = []
    flag_sources = []
    flag_notes = []
    for key, label in (("bankrupt", "bankrupt"), ("liquidating", "in liquidation"), ("forced_dissolution", "under forced dissolution")):
        item = selected[key]
        boolean = registry_boolean(item["value"])
        flag_values.append(label if boolean is True else "not " + label if boolean is False else label + ": unknown")
        flag_sources.append({"field": key, **_src(item["record"])})
        facts.append(fact("Identity", {"bankrupt": "Bankruptcy status", "liquidating": "Liquidation status", "forced_dissolution": "Forced dissolution status"}[key],
                          label if boolean is True else "not " + label if boolean is False else None,
                          item["status"], _src(item["record"]), item["note"]))
        if item["note"]:
            flag_notes.append(label + ": " + item["note"])
    known_flags = any(registry_boolean(selected[key]["value"]) is not None for key in ("bankrupt", "liquidating", "forced_dissolution"))
    source = {**next((_src(selected[key]["record"]) for key in ("bankrupt", "liquidating", "forced_dissolution") if selected[key]["module"]), {}), "fields": flag_sources}
    facts.append(fact("Identity", "Register status flags", "; ".join(flag_values),
                      "available" if known_flags else "not_available", source, "; ".join(flag_notes) or None))

    # financials
    fin = _ev(profile, "financials")
    records = sorted((fin.get("value") or {}).get("records") or [], key=lambda r: str((r.get("period") or {}).get("tilDato") or ""), reverse=True)
    fin_rows = []
    for r in records:
        fin_rows.append({
            "type": r.get("account_type"), "from": (r.get("period") or {}).get("fraDato"), "to": (r.get("period") or {}).get("tilDato"),
            "cur": r.get("currency"), **{k: _num(r.get(k)) for k in ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")},
        })
    fin_state = "available" if records else ("not_available" if fin.get("status") == "available" else _state(fin))
    latest = next((r for r in fin_rows if r["type"] == "SELSKAP"), fin_rows[0] if fin_rows else None)
    s_fin = _src(fin)
    if latest:
        period = f"{latest['from']} – {latest['to']}"
        for key, label in (("revenue", "Revenue"), ("operating_result", "Operating result"), ("annual_result", "Net result"), ("assets", "Total assets"), ("equity", "Equity"), ("debt", "Total debt")):
            v = latest.get(key)
            facts.append(fact("Financials", f"{label} ({latest['to'][:4] if latest.get('to') else ''})", {"money": v, "cur": latest.get("cur") or "NOK"} if v is not None else None,
                              "available" if v is not None else "not_available", {**s_fin, "period": period}, None if v is not None else "not reported in the filing (not zero)"))
    else:
        facts.append(fact("Financials", "Annual accounts", None, fin_state, s_fin))

    # people
    roles_ev = _ev(profile, "roles")
    roles = [r for r in (roles_ev.get("value") or {}).get("roles") or [] if not r.get("inactive")]
    order = {c: i for i, c in enumerate(ROLE_ORDER)}
    roles.sort(key=lambda r: order.get(r.get("role_code"), 99))
    s_roles = _src(roles_ev)
    for r in roles:
        name = " ".join(str(part) for part in r["name"] if part) if isinstance(r.get("name"), list) else r.get("name")
        name = str(name or "").strip() or ("Registered entity" if r.get("organisation_number") else "Name not registered")
        facts.append(fact("People", ROLE_LABELS.get(r.get("role_code"), r.get("role") or r.get("role_code") or "Role").split(" (")[0].capitalize(),
                          name + (f" (org. no. {r['organisation_number']})" if r.get("organisation_number") else ""), "available", {**s_roles, "changed": r.get("last_changed")}))
    if not roles:
        facts.append(fact("People", "Registered roles", None, "not_available" if roles_ev.get("status") == "available" else _state(roles_ev), s_roles))

    # locations / group
    loc = _ev(profile, "locations")
    rows = (loc.get("value") or {}).get("locations") or []
    s_loc = _src(loc)
    if rows:
        for r in rows:
            a = r.get("address") or {}
            facts.append(fact("Locations", "Workplace (sub-unit)", f"{r.get('name')} · {', '.join(a.get('adresse') or [])} {a.get('postnummer') or ''} {a.get('poststed') or ''}".strip(), "available", {**s_loc, "id": r.get("organisation_number")},
                              f"org. no. {r.get('organisation_number')}" + (f", {r['employees']} employees" if r.get("employees") is not None else "")))
    else:
        facts.append(fact("Locations", "Workplaces (sub-units)", "0 (checked)" if loc.get("status") == "available" else None, "available" if loc.get("status") == "available" else _state(loc), s_loc, "register checked: no sub-units" if loc.get("status") == "available" else None))
    grp = _ev(profile, "group")
    gv = grp.get("value") or {}
    if grp.get("status") == "available":
        facts.append(fact("Locations", "Group (ultimate parent in register)", f"{gv.get('navn')} ({gv.get('organisasjonsnummer')})", "available", _src(grp)))
    else:
        facts.append(fact("Locations", "Group membership", None, _state(grp), _src(grp), "not part of a registered group" if grp.get("status") == "not_found" else None))

    # digital footprint
    web = _ev(profile, "website")
    official = web.get("official_website") or {}
    if official.get("value"):
        facts.append(fact("Digital", "Official website", official["value"], "available", _src(official.get("evidence")), f"identity: {official.get('identity_method')}; lead: {official.get('lead_source')}"))
    else:
        facts.append(fact("Digital", "Official website", None, _state(web) if web.get("status") != "available" else "ambiguous", _src(web), {"ambiguous": "candidate site not proven to be this exact entity – not published", "not_available": "no verified site found", "blocked": "robots.txt disallows access"}.get(_state(web))))
    site = _ev(profile, "site_signals")
    sv = site.get("value") or {} if site.get("status") == "available" else {}
    fs = sv.get("field_states") or {}
    site_state = _state(site)

    def site_field(key: str) -> str:
        return AVAILABILITY.get(fs.get(key) or "not_found", "not_available") if site_state == "available" else site_state

    socials = sv.get("social_profiles") or []
    for s in socials:
        facts.append(fact("Digital", f"Social profile ({s.get('platform')})", s.get("url"), "available", _src(s.get("evidence"))))
    if not socials:
        facts.append(fact("Digital", "Social profiles", None, site_field("social_profiles"), _src(site)))
    contact = sv.get("contact") or {}
    for kind, label in (("emails", "E-mail"), ("phones", "Phone")):
        for item in contact.get(kind) or []:
            facts.append(fact("Digital", label, item.get("value"), "available", _src(item.get("evidence"))))
    if not (contact.get("emails") or contact.get("phones")):
        facts.append(fact("Digital", "Contact details", None, site_field("contact"), _src(site)))
    desc = sv.get("business_description") or {}
    if desc.get("meta_description"):
        facts.append(fact("Digital", "Company-reported description", desc["meta_description"], "available", _src(desc.get("evidence"))))

    # hiring
    careers = sv.get("careers") or {}
    for page in careers.get("pages") or []:
        facts.append(fact("Hiring", "Careers page", page.get("url"), "available", _src(page.get("evidence"))))
    if not careers.get("pages"):
        facts.append(fact("Hiring", "Careers page", None, site_field("careers"), _src(site)))
    for p in careers.get("postings") or []:
        facts.append(fact("Hiring", "Job posting (company site)", p.get("title") or p.get("url"), "available", _src(p.get("evidence"), link=p.get("url"))))
    nav = _ev(profile, "nav_jobs")
    ads = (nav.get("value") or {}).get("postings") or []
    for ad in sorted(ads, key=lambda a: str(a.get("published_at") or ""), reverse=True):
        places = ", ".join(sorted({(w.get("city") or w.get("municipal") or "").title() for w in ad.get("work_locations") or []} - {""}))
        facts.append(fact("Hiring", "Job ad (Nav)", ad.get("title"), "available", _src(ad.get("evidence")),
                          "; ".join(x for x in (ad.get("job_title"), places, ad.get("engagement_type"), f"published {ad.get('published_at')}" if ad.get("published_at") else None, f"expires {ad.get('expires_at')}" if ad.get("expires_at") else None) if x)))
    if not ads and not careers.get("postings"):
        facts.append(fact("Hiring", "Job postings", None, "not_available" if nav.get("status") in ("available", "not_found") else _state(nav), _src(nav), "Nav feed checked: no active ad with this org. no. or its sub-units"))

    # activity
    news = sorted(sv.get("news_posts") or [], key=lambda n: str(n.get("published_at") or ""), reverse=True)
    for n in news[:12]:
        facts.append(fact("Activity", f"News/blog post{' · ' + n['published_at'] if n.get('published_at') else ' (undated)'}", n.get("title") or n.get("url"), "available", _src(n.get("evidence"), link=n.get("url"))))
    if not news:
        facts.append(fact("Activity", "Dated news / blog posts", None, site_field("news_posts"), _src(site)))

    unknowns = synthesis.get("unknowns") or []
    statements = {s["id"]: s for s in synthesis.get("statements") or []}
    return {
        "org": org,
        "name": selected["name"]["value"] or profile.get("name"),
        "ds": dataset,
        "form": form,
        "city": (addr.get("poststed") or raw.get("forretningsadresse.poststed") or profile.get("municipality") or "").title(),
        "nace": raw.get("naeringskode1.kode"),
        "naceLabel": raw.get("naeringskode1.beskrivelse"),
        "emp": employees,
        "web": official.get("value"),
        "jobs": len(ads) + len(careers.get("postings") or []),
        "news": len(news),
        "socials": len(socials),
        "rev": latest.get("revenue") if latest else None,
        "res": latest.get("annual_result") if latest else None,
        "eq": latest.get("equity") if latest else None,
        "assets": latest.get("assets") if latest else None,
        "op": latest.get("operating_result") if latest else None,
        "period": f"{latest['from']} – {latest['to']}" if latest else None,
        "finSrc": s_fin,
        "dagl": next(((" ".join(r["name"]) if isinstance(r["name"], list) else r["name"]) for r in roles if r.get("role_code") in ("DAGL", "INNH", "BEST")), None),
        "chair": next(((" ".join(r["name"]) if isinstance(r["name"], list) else r["name"]) for r in roles if r.get("role_code") == "LEDE"), None),
        "subunits": len(rows) if loc.get("status") == "available" else None,
        "retrieved": live.get("retrieved_at") or reg.get("retrieved_at"),
        "facts": facts,
        "fin": fin_rows,
        "syn": {
            "headline": synthesis.get("headline"),
            "overview": (synthesis.get("overview") or {}).get("text"),
            "sections": [{"t": sec["title"], "k": sec["key"], "st": [{"x": statements[i]["text"], "c": statements[i]["citations"]} for i in sec["statement_ids"] if i in statements]} for sec in synthesis.get("sections") or []],
            "sources": [{k: s.get(k) for k in ("id", "label", "source_url", "retrieved_at", "source_class", "claim_span", "content_sha256")} for s in synthesis.get("sources") or []],
            "unknowns": [({"l": u.get("label"), "s": u.get("state"), "r": u.get("reason")} if isinstance(u, dict) else {"l": str(u), "s": "unknown", "r": None}) for u in unknowns],
            "changed": synthesis.get("what_changed"),
            "narrative": (synthesis.get("llm_polish") or {}).get("narrative"),
            "method": synthesis.get("method"),
            "checked": (synthesis.get("validation") or {}).get("statements_published"),
        },
        "avail": {k: v.get("state") for k, v in (synthesis.get("availability") or {}).items()},
        "changes": [{"f": c.get("field"), "old": c.get("old_value") if not isinstance(c.get("old_value"), (list, dict)) else None, "new": c.get("new_value") if not isinstance(c.get("new_value"), (list, dict)) else None, "url": c.get("source_url"), "at": c.get("retrieved_at"), "t": c.get("change_type"), "m": c.get("material", True)} for c in changes],
        "refresh": refresh,
    }


def read_rows(path: Path) -> list[tuple[dict, dict | None]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "profile" in row and "modules" in row:
            out.append((row["profile"], row))
        else:
            out.append((row, None))
    return out


def build_site(inputs: list[tuple[str, Path]], out_dir: Path) -> dict:
    companies: dict[str, dict] = {}
    for label, path in inputs:
        for profile, envelope in read_rows(Path(path)):
            item = slim(profile, label, envelope)
            companies.setdefault(item["org"], item)  # first input wins on overlap
    data = {"generated_from": [{"dataset": label, "path": str(path)} for label, path in inputs], "companies": sorted(companies.values(), key=lambda c: c["name"] or "")}
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "data.json").write_text(payload, encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload.replace("</", "<\\/"))
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    return {"index": str(out_dir / "index.html"), "companies": len(companies), "bytes": len(html.encode())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", action="append", required=True, help="label=path to profiles or envelopes JSONL (repeatable)")
    parser.add_argument("--out", default="site")
    args = parser.parse_args()
    inputs = []
    for item in args.input:
        label, _, path = item.partition("=")
        if not path:
            label, path = Path(item).stem, item
        inputs.append((label, Path(path)))
    print(json.dumps(build_site(inputs, Path(args.out)), indent=2))


if __name__ == "__main__":
    main()

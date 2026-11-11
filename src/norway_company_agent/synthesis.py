"""Evidence-bounded company synthesis.

Deterministic, keyless and template-based. Every statement carries
  * ``citations``: ids into ``synthesis.sources`` (each source is a URL + retrieval time + hash already
    present in the profile evidence), and
  * ``facts``: JSON pointers into the profile plus the value read there.

``validate_synthesis`` checks exact deterministic statement regeneration, citation provenance and
fact values against the profile. This is a closed-template integrity check, not a general semantic
entailment test and not independent verification of the underlying source's truth.

Optional LLM selection (``polish_with_llm``) may only select and reorder complete checked statements,
verbatim with their citations. Free factual rewriting is rejected. It is off unless
explicitly requested with ``--llm-synthesis`` and ``SIGNALPOST_LLM_API_KEY`` is set; rejected output leaves the deterministic synthesis unchanged.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from typing import Any

from .registry_state import registry_field, registry_boolean

SYNTHESIS_VERSION = "evidence_template_synthesis_v4"

AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "ambiguous": "ambiguous",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}

LEGAL_FORMS = {
    "AS": "private limited company (aksjeselskap)", "ASA": "public limited company (allmennaksjeselskap)",
    "ENK": "sole proprietorship (enkeltpersonforetak)", "ANS": "general partnership (ansvarlig selskap)",
    "DA": "partnership with shared liability (DA)", "SA": "cooperative (samvirkeforetak)",
    "NUF": "Norwegian branch of a foreign company (NUF)", "STI": "foundation (stiftelse)",
    "KS": "limited partnership (kommandittselskap)", "BRL": "housing cooperative (borettslag)",
    "SPA": "savings bank (sparebank)", "FLI": "association (forening/lag/innretning)",
    "ESEK": "owner-section condominium (eierseksjonssameie)", "SAM": "co-ownership (tingsrettslig sameie)",
    "BA": "company with limited liability (selskap med begrenset ansvar)", "KF": "municipal enterprise (kommunalt foretak)",
    "IKS": "inter-municipal company (interkommunalt selskap)", "KOMM": "municipality (kommune)",
    "ORGL": "organisational unit (organisasjonsledd)", "PK": "pension fund (pensjonskasse)",
    "STAT": "state body (staten)", "SF": "state enterprise (statsforetak)", "KIRK": "Church of Norway body (Den norske kirke)",
    "BBL": "housing builder cooperative (boligbyggelag)", "PRE": "shipping partnership (partsrederi)",
    "GFS": "mutual insurance company (gjensidig forsikringsselskap)", "OPMV": "VAT-registered unit (særskilt oppdelt enhet)",
    "ADOS": "administrative unit of a public body", "UTLA": "foreign entity (utenlandsk enhet)",
}

ROLE_LABELS = {
    "DAGL": "managing director (daglig leder)", "LEDE": "chair of the board (styrets leder)",
    "NEST": "deputy chair (nestleder)", "MEDL": "board member (styremedlem)",
    "VARA": "deputy board member (varamedlem)", "REVI": "auditor (revisor)",
    "REGN": "accountant (regnskapsfører)", "FFØR": "business manager (forretningsfører)",
    "DTSO": "partner with full liability (deltaker med solidarisk ansvar)",
    "DTPR": "partner with proportional liability (deltaker med delt ansvar)",
    "INNH": "proprietor (innehaver)", "KONT": "contact person (kontaktperson)",
    "KOMP": "general partner (komplementar)", "BEST": "manager of the Norwegian branch (daglig leder i NUF)",
    "SAM": "co-owner (sameier)",
}
ROLE_ORDER = ["DAGL", "INNH", "BEST", "LEDE", "NEST", "MEDL", "VARA", "DTSO", "DTPR", "KOMP", "FFØR", "KONT", "REVI", "REGN"]

INFO_TYPES = [
    ("registry_identity", "Registry identity"),
    ("business_description", "Business description (company-reported)"),
    ("financials", "Latest annual accounts"),
    ("roles", "Leadership and board"),
    ("locations", "Registered workplaces (sub-units)"),
    ("group", "Group structure"),
    ("official_website", "Verified official website"),
    ("social_profiles", "Company-owned social profiles"),
    ("news_posts", "Dated news / blog posts"),
    ("careers", "Careers page"),
    ("job_postings", "Job postings"),
    ("contact", "Contact details (company site)"),
    ("employees", "Employee count (registry)"),
]

SERVICE_HEADING_NOISE = re.compile(
    r"^(tjenester|våre tjenester|v[åa]re produkter|produkter|om oss|kontakt( oss)?|ta kontakt|vår adresse|adresse|"
    r"sosiale medier|følg oss|meny|menu|nyheter|aktuelt|hurtiglenker|besøk|åpningstider|nyhetsbrev|newsletter|"
    r"services|our services|contact( us)?|about( us)?|home|hjem|personvern|cookies?)\W*$",
    re.I,
)
NUMBER = re.compile(r"\d[\d,.]*\d|\d")
CITE = re.compile(r"\[(S\d+)\]")


# --------------------------------------------------------------------------- helpers

def _esc(key: str) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def pointer(*parts: Any) -> str:
    return "".join("/" + _esc(str(part)) for part in parts)


def resolve_pointer(document: Any, ptr: str) -> Any:
    if not isinstance(ptr, str) or (ptr and not ptr.startswith("/")):
        raise ValueError("Invalid JSON pointer")
    if ptr == "":
        return document
    value = document
    for raw in ptr.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("Invalid JSON pointer array index")
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise KeyError(ptr)
    return value


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _nok(value: Any, currency: str | None = "NOK") -> str:
    return f"{currency or 'NOK'} {_num(value)}"


def _clean(text: Any, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _status(record: dict | None) -> str:
    return AVAILABILITY.get((record or {}).get("status") or "not_fetched", "failed")


def _number_tokens(text: str) -> set[str]:
    tokens = set()
    for match in NUMBER.findall(text):
        token = match.replace(",", "").rstrip(".")
        if token.endswith(".0"):
            token = token[:-2]
        tokens.add(token)
    return tokens


def _value_tokens(value: Any) -> set[str]:
    out: set[str] = set()
    if value is None or isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.add(str(int(value)) if float(value).is_integer() else str(value))
        out.add(str(abs(int(value))) if float(value).is_integer() else str(abs(value)))
        out.add(f"{value}".replace(",", ""))
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out |= _value_tokens(item)
        return out
    if isinstance(value, dict):
        for item in value.values():
            out |= _value_tokens(item)
        return out
    text = str(value)
    out |= _number_tokens(text)
    out |= set(re.findall(r"\d+", text))
    return out


class _Builder:
    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self.sources: list[dict[str, Any]] = []
        self._source_keys: dict[tuple, str] = {}
        self.statements: list[dict[str, Any]] = []

    def source(self, label: str, ptr: str, record: dict[str, Any]) -> str:
        url = record.get("source_url")
        key = (ptr, url, record.get("retrieved_at"), record.get("content_sha256"), label)
        if key in self._source_keys:
            return self._source_keys[key]
        sid = f"S{len(self.sources) + 1}"
        self._source_keys[key] = sid
        item = {
            "id": sid,
            "label": label,
            "source_url": url,
            "source_class": record.get("source_class") or record.get("source_type"),
            "retrieved_at": record.get("retrieved_at"),
            "content_sha256": record.get("content_sha256"),
            "evidence_pointer": ptr,
        }
        if record.get("claim_span"):
            item["claim_span"] = _clean(record["claim_span"], 300)
        self.sources.append(item)
        return sid

    def say(self, section: str, text: str, citations: list[str], facts: list[tuple[str, Any]] | None = None, derived: list[dict] | None = None) -> None:
        text = text.replace(".”.", ".”").replace("!”.", "!”").replace("?”.", "?”")
        self.statements.append({
            "id": f"{section}-{sum(1 for s in self.statements if s['section'] == section) + 1}",
            "section": section,
            "text": text,
            "citations": sorted(set(citations), key=lambda s: int(s[1:])),
            "facts": [{"pointer": p, "value": v} for p, v in (facts or [])],
            **({"derived": derived} if derived else {}),
        })


def _ev(profile: dict, module: str) -> dict:
    return (profile.get("evidence") or {}).get(module) or {}


def _group_position(value: dict, org: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    if value.get("organisasjonsnummer") == org:
        return {"role": "parent", "children": value.get("children") or [], "top": value}
    stack = list(value.get("children") or [])
    while stack:
        node = stack.pop(0)
        if node.get("organisasjonsnummer") == org:
            return {"role": "subsidiary", "node": node, "top": value, "children": node.get("children") or []}
        stack.extend(node.get("children") or [])
    return None


# --------------------------------------------------------------------------- sections

def _identity(b: _Builder) -> None:
    p = b.profile
    reg = _ev(p, "registry")
    raw = reg.get("value") or {}
    def selected(field):
        item = registry_field(p, field)
        if item["module"] is None:
            return item, None, None
        label = "Brønnøysund entity register (bulk snapshot fallback)" if item["fallback"] else "Brønnøysund entity register (live API)"
        sid = b.source(label, pointer("evidence", item["module"]), item["record"])
        return item, sid, pointer("evidence", item["module"], "value", item["key"])

    name, ns, np = selected("name")
    form, fs, fp = selected("legal_form")
    if ns:
        text = f"{name['value']} (org. no. {p.get('organisation_number')})"
        facts = [(np, name["value"]), (pointer("organisation_number"), p.get("organisation_number"))]
        cites = [ns]
        if fs:
            label = LEGAL_FORMS.get(form["value"])
            text += (" is " + ("an " if label[:1].lower() in "aeiou" else "a ") + label) if label else f" is registered with legal form code {form['value']}"
            facts.append((fp, form["value"]))
            cites.append(fs)
        if name["fallback"] or form["fallback"]:
            text += "; identity fields from the bulk snapshot are historical fallbacks where live values are unavailable or unverified"
        b.say("what_it_does", text + ".", cites, facts)
    for field, label in (("bankrupt", "bankrupt (konkurs)"), ("liquidating", "under liquidation (under avvikling)"), ("forced_dissolution", "under forced dissolution")):
        item, sid, ptr = selected(field)
        if sid and registry_boolean(item["value"]) is True:
            scope = "The bulk registry snapshot (current live status unavailable or unverified)" if item["fallback"] else "The live register"
            b.say("what_it_does", f"{scope} marks the entity as {label}.", [sid], [(ptr, item["value"])])
    emp, sid, ptr = selected("employees")
    if sid:
        scope = "The bulk registry snapshot (current live count unavailable or unverified)" if emp["fallback"] else "The live register"
        b.say("size", f"{scope} reports {_num(emp['value'])} employees.", [sid], [(ptr, emp["value"])])
    if reg.get("status") != "available" or str(raw.get("organisasjonsnummer") or p.get("organisation_number")) != str(p.get("organisation_number")):
        return
    s_reg = b.source("Brønnøysund entity register (bulk snapshot)", pointer("evidence", "registry"), reg)
    base = pointer("evidence", "registry", "value")
    purpose = raw.get("vedtektsfestetFormaal")
    if purpose:
        b.say("what_it_does", f"The bulk snapshot’s registered purpose (vedtektsfestet formål) reads: “{_clean(purpose)}”.", [s_reg], [(base + pointer("vedtektsfestetFormaal"), purpose)])
    activity = raw.get("aktivitet")
    if activity and _clean(activity) != _clean(purpose):
        b.say("what_it_does", f"Bulk snapshot registered activity (aktivitet): “{_clean(activity)}”.", [s_reg], [(base + pointer("aktivitet"), activity)])
    founded = raw.get("stiftelsesdato")
    if founded:
        b.say("what_it_does", f"It was founded on {founded} and entered in the Central Coordinating Register on {raw.get('registreringsdatoenhetsregisteret') or 'an unreported date'}.", [s_reg], [(base + pointer("stiftelsesdato"), founded), (base + pointer("registreringsdatoenhetsregisteret"), raw.get("registreringsdatoenhetsregisteret"))])


def _description(b: _Builder) -> None:
    site = _ev(b.profile, "site_signals")
    if site.get("status") != "available":
        return
    value = site.get("value") or {}
    desc = value.get("business_description") or {}
    services = value.get("services") or {}
    if desc.get("meta_description"):
        sid = b.source("Verified company website (homepage)", pointer("evidence", "site_signals", "value", "business_description", "evidence"), desc.get("evidence") or {})
        b.say("what_it_does", f"Its verified website describes it as: “{_clean(desc['meta_description'])}” (company-reported).", [sid], [(pointer("evidence", "site_signals", "value", "business_description", "meta_description"), desc["meta_description"])])
    headings = []
    seen = set()
    for index, heading in enumerate(services.get("headings") or []):
        text = _clean(heading, 80)
        if len(text) < 3 or SERVICE_HEADING_NOISE.match(text) or text.casefold() in seen:
            continue
        if b.profile.get("name") and text.casefold() == str(b.profile["name"]).casefold():
            continue
        seen.add(text.casefold())
        headings.append((index, text))
        if len(headings) == 6:
            break
    if headings:
        sid = b.source("Verified company website (services page)", pointer("evidence", "site_signals", "value", "services", "evidence"), services.get("evidence") or {})
        b.say("what_it_does", "Its services page lists: " + "; ".join(f"“{t}”" for _, t in headings) + " (company-reported).", [sid],
              [(pointer("evidence", "site_signals", "value", "services", "headings", i), (services.get("headings") or [])[i]) for i, _ in headings])


def _financials(b: _Builder) -> None:
    fin = _ev(b.profile, "financials")
    records = ((fin.get("value") or {}).get("records") or []) if fin.get("status") == "available" else []
    if not records:
        return
    sid = b.source("Brønnøysund Register of Company Accounts (annual accounts API)", pointer("evidence", "financials"), fin)
    indexed = list(enumerate(records))
    company = [(i, r) for i, r in indexed if r.get("account_type") == "SELSKAP"] or indexed
    company.sort(key=lambda item: str((item[1].get("period") or {}).get("tilDato") or ""), reverse=True)
    i, latest = company[0]
    base = pointer("evidence", "financials", "value", "records", i)
    period = latest.get("period") or {}
    cur = latest.get("currency") or "NOK"
    facts = [(base + pointer("period", "fraDato"), period.get("fraDato")), (base + pointer("period", "tilDato"), period.get("tilDato"))]
    parts, missing = [], []
    for key, label in (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "net result for the year"), ("assets", "total assets"), ("equity", "equity"), ("debt", "total debt")):
        value = latest.get(key)
        if value is None:
            missing.append(label)
            continue
        parts.append(f"{label} {_nok(value, cur)}")
        facts.append((base + pointer(key), value))
    if parts:
        kind = "company" if latest.get("account_type") == "SELSKAP" else str(latest.get("account_type") or "").lower()
        text = f"In the filed {kind} annual accounts for {period.get('fraDato')} to {period.get('tilDato')}, " + ", ".join(parts) + "."
        if missing:
            text += " Not reported in that filing: " + ", ".join(missing) + " (left blank, not treated as zero)."
        b.say("financials", text, [sid], facts)
    elif missing:
        b.say("financials", f"The filed annual accounts for {period.get('fraDato')} to {period.get('tilDato')} report none of revenue, results, assets, equity or debt.", [sid], facts)
    # trend vs the previous company period
    if len(company) > 1:
        j, prior = company[1]
        pbase = pointer("evidence", "financials", "value", "records", j)
        for key, label in (("revenue", "Revenue"), ("annual_result", "Net result")):
            now, before = latest.get(key), prior.get(key)
            if now is None or before is None:
                continue
            pperiod = prior.get("period") or {}
            text = f"{label} was {_nok(now, cur)} for the period ending {period.get('tilDato')}, against {_nok(before, prior.get('currency') or cur)} for the period ending {pperiod.get('tilDato')}"
            derived = None
            if key == "revenue" and before and before > 0:
                pct = round((now - before) / before * 100, 1)
                text += f" ({'+' if pct >= 0 else ''}{pct}%)"
                derived = [{"op": "pct_change", "inputs": [base + pointer(key), pbase + pointer(key)], "value": pct}]
            b.say("financials", text + ".", [sid], [(base + pointer(key), now), (pbase + pointer(key), before), (base + pointer("period", "tilDato"), period.get("tilDato")), (pbase + pointer("period", "tilDato"), pperiod.get("tilDato"))], derived)
    group_records = [(i, r) for i, r in indexed if r.get("account_type") == "KONSERN"]
    if group_records:
        group_records.sort(key=lambda item: str((item[1].get("period") or {}).get("tilDato") or ""), reverse=True)
        gi, g = group_records[0]
        gbase = pointer("evidence", "financials", "value", "records", gi)
        if g.get("revenue") is not None:
            b.say("financials", f"Consolidated group accounts are also filed: group revenue {_nok(g['revenue'], g.get('currency'))} for the period ending {(g.get('period') or {}).get('tilDato')}.", [sid], [(gbase + pointer("revenue"), g["revenue"]), (gbase + pointer("period", "tilDato"), (g.get("period") or {}).get("tilDato"))])


def _months_between(start: str, end: str) -> int | None:
    m1 = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(start or ""))
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(end or ""))
    if not m1 or not m2:
        return None
    y1, mo1, d1 = map(int, m1.groups())
    y2, mo2, d2 = map(int, m2.groups())
    return (y2 - y1) * 12 + (mo2 - mo1) - (1 if d2 < d1 else 0)


def _signals(b: _Builder) -> None:
    """Decision signals: a few derived readings of the cited filings and register flags.

    Every number is either copied from a cited fact or a declared derived value (ratio_pct /
    pct_change) that the validator recomputes. Wording is qualitative only where the inputs'
    signs decide it (loss, negative equity). Nothing here is a forecast or a rating.
    """
    p = b.profile
    flags = []
    for field in ("bankrupt", "liquidating", "forced_dissolution"):
        item = registry_field(p, field)
        if item["module"] is None or item["fallback"]:
            flags = None
            break
        flags.append((field, item))
    if flags and all(registry_boolean(item["value"]) is False for _, item in flags):
        label = "Brønnøysund entity register (live API)"
        cites, facts = [], []
        for _, item in flags:
            cites.append(b.source(label, pointer("evidence", item["module"]), item["record"]))
            facts.append((pointer("evidence", item["module"], "value", item["key"]), item["value"]))
        b.say("key_signals", "Status: the live register shows no bankruptcy, liquidation or forced-dissolution flag.", cites, facts)
    fin = _ev(p, "financials")
    records = ((fin.get("value") or {}).get("records") or []) if fin.get("status") == "available" else []
    company = [(i, r) for i, r in enumerate(records) if r.get("account_type") == "SELSKAP"]
    if not company:
        return
    company.sort(key=lambda item: str((item[1].get("period") or {}).get("tilDato") or ""), reverse=True)
    sid = b.source("Brønnøysund Register of Company Accounts (annual accounts API)", pointer("evidence", "financials"), fin)
    i, latest = company[0]
    base = pointer("evidence", "financials", "value", "records", i)
    end = (latest.get("period") or {}).get("tilDato")
    end_fact = (base + pointer("period", "tilDato"), end)
    revenue, operating, result = latest.get("revenue"), latest.get("operating_result"), latest.get("annual_result")
    equity, assets = latest.get("equity"), latest.get("assets")
    if isinstance(revenue, (int, float)) and revenue > 0 and isinstance(operating, (int, float)):
        pct = round(operating / revenue * 100, 1)
        b.say("key_signals", f"Operating margin: {pct}% of revenue (operating result ÷ revenue) for the period ending {end}.", [sid],
              [(base + pointer("operating_result"), operating), (base + pointer("revenue"), revenue), end_fact],
              [{"op": "ratio_pct", "inputs": [base + pointer("operating_result"), base + pointer("revenue")], "value": pct}])
    if isinstance(equity, (int, float)) and isinstance(assets, (int, float)) and assets > 0:
        pct = round(equity / assets * 100, 1)
        text = f"Equity ratio: {pct}% (equity ÷ total assets) for the period ending {end}"
        if equity < 0:
            text += "; equity is negative, so recorded debts exceed recorded assets"
        elif pct < 10:
            text += "; most of the balance sheet is financed by debt"
        b.say("key_signals", text + ".", [sid], [(base + pointer("equity"), equity), (base + pointer("assets"), assets), end_fact],
              [{"op": "ratio_pct", "inputs": [base + pointer("equity"), base + pointer("assets")], "value": pct}])
    if len(company) > 1 and isinstance(result, (int, float)):
        j, prior = company[1]
        before = prior.get("annual_result")
        pend = (prior.get("period") or {}).get("tilDato")
        pbase = pointer("evidence", "financials", "value", "records", j)
        if isinstance(before, (int, float)):
            if result < 0 and before < 0:
                reading = "net losses in both of the two latest filed years"
            elif result > 0 and before > 0:
                reading = "a net profit in both of the two latest filed years"
            elif result == 0 or before == 0:
                reading = "a break-even result in at least one of the two latest filed years"
            elif result < 0:
                reading = "a net loss in the latest filed year, after a profit the year before"
            else:
                reading = "a net profit in the latest filed year, after a loss the year before"
            b.say("key_signals", f"Profitability: {reading} (periods ending {pend} and {end}).", [sid],
                  [(base + pointer("annual_result"), result), (pbase + pointer("annual_result"), before), end_fact, (pbase + pointer("period", "tilDato"), pend)])
    elif isinstance(result, (int, float)):
        reading = "a net loss" if result < 0 else ("a net profit" if result > 0 else "a break-even result")
        b.say("key_signals", f"Profitability: {reading} in the latest filed year (period ending {end}).", [sid], [(base + pointer("annual_result"), result), end_fact])
    checked = fin.get("retrieved_at")
    gap = _months_between(end, checked)
    if gap is not None and gap > 18:
        b.say("key_signals", f"Filing recency: the newest filed accounts end on {end}, more than a year and a half before they were checked ({str(checked)[:10]}), so the figures may be out of date.", [sid],
              [end_fact, (pointer("evidence", "financials", "retrieved_at"), checked)])


def _people(b: _Builder) -> None:
    roles_ev = _ev(b.profile, "roles")
    roles = ((roles_ev.get("value") or {}).get("roles") or []) if roles_ev.get("status") == "available" else []
    active = [(i, r) for i, r in enumerate(roles) if not r.get("inactive") and r.get("name")]
    if not active:
        return
    sid = b.source("Brønnøysund roles register", pointer("evidence", "roles"), roles_ev)
    by_code: dict[str, list] = {}
    for i, r in active:
        by_code.setdefault(r.get("role_code") or "?", []).append((i, r))
    lines, facts = [], []
    for code in ROLE_ORDER + sorted(set(by_code) - set(ROLE_ORDER)):
        if code not in by_code:
            continue
        items = by_code[code]
        label = ROLE_LABELS.get(code, (items[0][1].get("role") or code).lower())
        names = []
        for i, r in items:
            n = " ".join(map(str, r["name"])) if isinstance(r["name"], list) else str(r["name"])
            if r.get("organisation_number"):
                n += f" (org. no. {r['organisation_number']})"
                facts.append((pointer("evidence", "roles", "value", "roles", i, "organisation_number"), r["organisation_number"]))
            names.append(n)
            facts.append((pointer("evidence", "roles", "value", "roles", i, "name"), r["name"]))
        plural = label if len(items) == 1 else label.replace(" (", "s (", 1) if "(" in label else label + "s"
        lines.append(f"{plural}: {', '.join(names)}")
    b.say("people", "Registered roles: " + "; ".join(lines) + ".", [sid], facts)
    changed = sorted({(r.get("last_changed"), r.get("group")) for _, r in active if r.get("last_changed")}, reverse=True)
    if changed:
        date, group = changed[0]
        i = next(i for i, r in active if r.get("last_changed") == date and r.get("group") == group)
        b.say("what_changed", f"The most recent registered change to its roles ({group}) is dated {date}.", [sid], [(pointer("evidence", "roles", "value", "roles", i, "last_changed"), date), (pointer("evidence", "roles", "value", "roles", i, "group"), group)])


def _locations(b: _Builder) -> None:
    p = b.profile
    live = _ev(p, "registry_live")
    addr = (live.get("value") or {}).get("business_address") if live.get("status") == "available" else None
    if addr and addr.get("poststed"):
        sid = b.source("Brønnøysund entity register (live API)", pointer("evidence", "registry_live"), live)
        street = ", ".join(addr.get("adresse") or [])
        aptr = pointer("evidence", "registry_live", "value", "business_address")
        text = f"Registered business address: {street + ', ' if street else ''}{addr.get('postnummer') or ''} {str(addr.get('poststed')).title()}".replace("  ", " ")
        b.say("locations", text + f" ({(addr.get('kommune') or '').title()} municipality).", [sid], [(aptr + pointer("adresse"), addr.get("adresse")), (aptr + pointer("postnummer"), addr.get("postnummer")), (aptr + pointer("poststed"), addr.get("poststed")), (aptr + pointer("kommune"), addr.get("kommune"))])
    loc = _ev(p, "locations")
    if loc.get("status") == "available":
        sid = b.source("Brønnøysund sub-unit register (underenheter)", pointer("evidence", "locations"), loc)
        rows = (loc.get("value") or {}).get("locations") or []
        if not rows:
            b.say("locations", "The sub-unit register lists no registered workplaces (underenheter) for it (checked, zero found).", [sid], [(pointer("evidence", "locations", "value", "locations"), rows)])
        else:
            places = {}
            for i, r in enumerate(rows):
                town = ((r.get("address") or {}).get("poststed") or "").title()
                if town:
                    places.setdefault(town, i)
            text = f"It has {len(rows)} registered workplace{'s' if len(rows) != 1 else ''} (underenheter)"
            facts = [(pointer("evidence", "locations", "value", "locations", i, "address", "poststed"), rows[i]["address"]["poststed"]) for i in places.values()]
            facts.append((pointer("evidence", "locations", "value", "locations"), [r.get("organisation_number") for r in rows]))
            if places:
                shown = list(places)[:8]
                text += " in " + ", ".join(shown) + (", among other towns" if len(places) > 8 else "")
            b.say("locations", text + ".", [sid], facts)
    grp = _ev(p, "group")
    if grp.get("status") == "available":
        pos = _group_position(grp.get("value") or {}, p["organisation_number"])
        if pos:
            sid = b.source("Brønnøysund group structure (konsernstruktur)", pointer("evidence", "group"), grp)
            top = pos["top"]
            if pos["role"] == "parent":
                kids = pos["children"]
                b.say("locations", f"The group register lists it as a group parent with {len(kids)} subsidiar{'y' if len(kids) == 1 else 'ies'} directly below it in the group tree.", [sid], [(pointer("evidence", "group", "value", "organisasjonsnummer"), top.get("organisasjonsnummer")), (pointer("evidence", "group", "value", "children"), [c.get("organisasjonsnummer") for c in kids])])
            else:
                node = pos["node"]
                text = f"The group register lists it as a subsidiary of {node.get('parentNavn')} (org. no. {node.get('parentOrganisasjonsnummer')})"
                facts = [(pointer("evidence", "group", "value"), {"parentNavn": node.get("parentNavn"), "parentOrganisasjonsnummer": node.get("parentOrganisasjonsnummer"), "grunnlag": node.get("grunnlag"), "top": top.get("navn"), "top_org": top.get("organisasjonsnummer")})]
                if node.get("grunnlag"):
                    text += f", ownership basis {node['grunnlag']}"
                if top.get("organisasjonsnummer") != node.get("parentOrganisasjonsnummer"):
                    text += f"; the ultimate parent in the register is {top.get('navn')} (org. no. {top.get('organisasjonsnummer')})"
                b.say("locations", text + ".", [sid], facts)


def _digital(b: _Builder) -> None:
    p = b.profile
    web = _ev(p, "website")
    official = web.get("official_website") or {}
    if official.get("value"):
        sid = b.source("Verified official website (identity proof page)", pointer("evidence", "website", "official_website", "evidence"), official.get("evidence") or {})
        method = {
            "organisation_number": "its organisation number appears on the site",
            "legal_name_and_registered_address": "its full legal name and registered address appear on the same page",
            "deterministic_name_org_evidence_v3": "it is the website declared in the register and the legal name appears on it",
            "deterministic_name_org_evidence_v4": "it is the website declared in the register and the legal name appears on it",
        }.get(official.get("identity_method"), "it passed the exact-entity identity gate")
        lead = {"brreg_hjemmeside": "declared in the register", "brreg_email_domain": "found from the registered e-mail domain", "legal_name_domain_guess": "found from the legal name"}.get(official.get("lead_source"))
        b.say("digital_footprint", f"Official website: {official['value']}{' (' + lead + ')' if lead else ''}, verified because {method}.", [sid], [(pointer("evidence", "website", "official_website", "value"), official["value"])])
    site = _ev(p, "site_signals")
    value = site.get("value") or {} if site.get("status") == "available" else {}
    vptr = pointer("evidence", "site_signals", "value")
    socials = value.get("social_profiles") or []
    if socials:
        cites = [b.source(f"Verified website link to {s.get('platform')}", vptr + pointer("social_profiles", i, "evidence"), s.get("evidence") or {}) for i, s in enumerate(socials)]
        b.say("digital_footprint", "Company-owned profiles linked from the verified site: " + ", ".join(f"{s.get('platform')} ({s.get('url')})" for s in socials) + ".", cites, [(vptr + pointer("social_profiles", i, "url"), s.get("url")) for i, s in enumerate(socials)])
    contact = value.get("contact") or {}
    bits, facts, cites = [], [], []
    for kind, label in (("emails", "e-mail"), ("phones", "phone")):
        for i, item in enumerate((contact.get(kind) or [])[:2]):
            bits.append(f"{label} {item.get('value')}")
            facts.append((vptr + pointer("contact", kind, i, "value"), item.get("value")))
            cites.append(b.source("Verified company website (contact)", vptr + pointer("contact", kind, i, "evidence"), item.get("evidence") or {}))
    if bits:
        b.say("digital_footprint", "Contact details published on its own site: " + ", ".join(bits) + ".", cites, facts)
    careers = value.get("careers") or {}
    if careers.get("pages"):
        page = careers["pages"][0]
        sid = b.source("Verified company website (careers link)", vptr + pointer("careers", "pages", 0, "evidence"), page.get("evidence") or {})
        b.say("hiring", f"The verified site links a careers page: {page.get('url')}.", [sid], [(vptr + pointer("careers", "pages", 0, "url"), page.get("url"))])
    postings = careers.get("postings") or []
    if postings:
        cites = [b.source("Verified company website (job posting)", vptr + pointer("careers", "postings", i, "evidence"), x.get("evidence") or {}) for i, x in enumerate(postings[:5])]
        b.say("hiring", f"Its careers pages list {len(postings)} job posting{'s' if len(postings) != 1 else ''}, e.g. “{_clean(postings[0].get('title'), 120)}”.", cites, [(vptr + pointer("careers", "postings"), [x.get("url") for x in postings]), (vptr + pointer("careers", "postings", 0, "title"), postings[0].get("title"))])
    elif careers.get("job_postings") == "checked_zero" or careers.get("no_vacancies_statement"):
        if careers.get("pages"):
            sid = b.source("Verified company website (careers link)", vptr + pointer("careers", "pages", 0, "evidence"), careers["pages"][0].get("evidence") or {})
            b.say("hiring", "Its careers page states there are no open vacancies.", [sid], [(vptr + pointer("careers", "no_vacancies_statement"), careers.get("no_vacancies_statement"))])
    nav = _ev(p, "nav_jobs")
    ads = ((nav.get("value") or {}).get("postings") or []) if nav.get("status") == "available" else []
    if ads:
        order = sorted(range(len(ads)), key=lambda i: str(ads[i].get("published_at") or ""), reverse=True)
        cites = [b.source("Nav arbeidsplassen job ad (employer org. no. match)", pointer("evidence", "nav_jobs", "value", "postings", i, "evidence"), ads[i].get("evidence") or {}) for i in order[:5]]
        first = ads[order[0]]
        latest_label = "the newest by publication date among the checked dated ads" if first.get("published_at") else "one checked ad"
        text = f"The checked Nav job-vacancy feed records confirm at least {len(ads)} active ad{'s' if len(ads) != 1 else ''} whose employer is this company or one of its registered sub-units; {latest_label} is “{_clean(first.get('title'), 120)}”"
        facts = [(pointer("evidence", "nav_jobs", "value", "postings"), [a.get("uuid") for a in ads]), (pointer("evidence", "nav_jobs", "value", "postings", order[0], "title"), first.get("title"))]
        if first.get("published_at"):
            facts.append((pointer("evidence", "nav_jobs", "value", "postings", order[0], "published_at"), first["published_at"]))
        if first.get("expires_at"):
            text += f", open until {first['expires_at']}"
            facts.append((pointer("evidence", "nav_jobs", "value", "postings", order[0], "expires_at"), first["expires_at"]))
        if (nav.get("value") or {}).get("collection_complete") is not True:
            text += "; collection completeness is unknown or incomplete, so additional active ads may exist"
        b.say("hiring", text + ".", cites, facts)
    news = value.get("news_posts") or []
    dated = sorted([(i, n) for i, n in enumerate(news) if n.get("published_at")], key=lambda x: x[1]["published_at"], reverse=True)
    if dated:
        i, n = dated[0]
        cites = [b.source("Verified company website (news/blog)", vptr + pointer("news_posts", j, "evidence"), x.get("evidence") or {}) for j, x in dated[:3]]
        text = f"The checked site records include {len(dated)} dated news/blog post{'s' if len(dated) != 1 else ''}; the latest by publication date among these observed posts, dated {n['published_at']}, is “{_clean(n.get('title') or n.get('url'), 120)}”"
        b.say("recent_activity", text + ".", cites, [(vptr + pointer("news_posts"), [x.get("url") for _, x in dated]), (vptr + pointer("news_posts", i, "published_at"), n["published_at"]), (vptr + pointer("news_posts", i, "title" if n.get("title") else "url"), n.get("title") or n.get("url"))])
    elif news:
        cites = [b.source("Verified company website (news/blog)", vptr + pointer("news_posts", 0, "evidence"), news[0].get("evidence") or {})]
        b.say("recent_activity", f"The checked site records include {len(news)} news/blog item{'s' if len(news) != 1 else ''} without a publication date.", cites, [(vptr + pointer("news_posts"), [x.get("url") for x in news])])


CHANGE_LABELS = {
    "registry.name": "Legal name", "registry.legal_form": "Legal form", "registry.employees": "Employee count (register)",
    "registry.bankrupt": "Bankruptcy status", "registry.liquidating": "Liquidation status",
    "registry.forced_dissolution": "Forced dissolution status",
    "registry.municipality": "Municipality",
    "registry.website": "Registered website", "registry.latest_submitted_accounts": "Latest submitted accounts year",
    "financials.records": "Annual accounts", "financial_history.years": "Annual-account copies",
    "roles.roles": "Registered roles", "locations.locations": "Registered workplaces",
    "website.title": "Website title", "website.description": "Website description", "website.social_links": "Published social links",
    "website.official_website": "Verified official website", "nav_jobs.posting_ids": "Active Nav job ads",
    "site_signals.news_post_urls": "News/blog posts on the site",
}


def _describe_change(change: dict) -> str:
    field = change.get("field")
    old, new = change.get("old_value"), change.get("new_value")
    label = CHANGE_LABELS.get(field, field)
    if field in {"registry.bankrupt", "registry.liquidating", "registry.forced_dissolution"}:
        phrase = {"registry.bankrupt": "bankrupt", "registry.liquidating": "under liquidation", "registry.forced_dissolution": "under forced dissolution"}[field]
        def status(value):
            flag = registry_boolean(value)
            return phrase if flag is True else f"not marked as {phrase}" if flag is False else "unknown"
        return f"{label}: {status(old)} → {status(new)}."
    if field == "roles.roles":
        def keyset(rows):
            def text(value):
                # role holder names can arrive as a list (an entity with several name lines)
                return " ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else value
            return {(text(r.get("role_code")), text(r.get("name"))) for r in rows or [] if isinstance(r, dict) and not r.get("inactive")}
        added = sorted(keyset(new) - keyset(old), key=str)
        removed = sorted(keyset(old) - keyset(new), key=str)
        bits = []
        if added:
            bits.append("added " + ", ".join(f"{n} as {ROLE_LABELS.get(c, c)}" for c, n in added[:5]))
        if removed:
            bits.append("removed " + ", ".join(f"{n} as {ROLE_LABELS.get(c, c)}" for c, n in removed[:5]))
        return f"{label}: " + ("; ".join(bits) if bits else "role details updated") + "."
    if field == "financials.records":
        periods = lambda rows: sorted({(r.get("period") or {}).get("tilDato") for r in rows or []} - {None})
        newp = sorted(set(periods(new)) - set(periods(old)))
        if newp:
            return f"{label}: new filing for the period ending {', '.join(newp)}."
        return f"{label}: figures for an existing period were revised."
    if isinstance(old, list) or isinstance(new, list):
        o, n = set(map(str, old or [])), set(map(str, new or []))
        return f"{label}: {len(n - o)} added, {len(o - n)} removed (now {len(n)})."
    return f"{label}: {old if old not in (None, '') else 'none'} → {new if new not in (None, '') else 'none'}."


def _changes(b: _Builder, refresh: dict | None, changes: list[dict] | None) -> dict:
    if not refresh or not refresh.get("previous_snapshot_supplied"):
        return {"status": "no_previous_snapshot", "text": "No previous snapshot was supplied for this run, so changes since an earlier run cannot be determined; dated registry and site events are listed instead.", "count": 0}
    if not refresh.get("company_in_previous_snapshot"):
        return {"status": "not_in_previous_snapshot", "text": "This company was not in the previous snapshot, so this run is its first observation.", "count": 0, "previous": refresh.get("previous")}
    failed = [c for c in changes or [] if c.get("material") is False]
    described_failures = set()
    for change in failed:
        index = (changes or []).index(change)
        module = change.get("source_module") or change["field"].split(".", 1)[0]
        if module in described_failures:
            continue
        described_failures.add(module)
        record = _ev(b.profile, module)
        sid = b.source(f"{module} comparison availability", pointer("evidence", module), record)
        facts = [(pointer("changes", index, "change_type"), change.get("change_type"))]
        if change.get("previous_verified_value"):
            text = f"The previously verified website {change['previous_verified_value']} could not be reached in this run, so it is not re-published; this is a failed re-check, not a confirmed change."
            facts.append((pointer("changes", index, "previous_verified_value"), change["previous_verified_value"]))
        elif change.get("change_type") == "previous_source_unavailable":
            text = f"The previous {module} source observation was unavailable, so this result is not reported as a confirmed company change."
        elif change.get("change_type") == "collection_window_changed":
            text = f"The observed {module} collection window changed; coverage is incomplete or unknown, so missing items are not confirmed removals."
        elif change.get("change_type") == "source_changed":
            text = "The registry observations came from different sources, so their differences are not reported as confirmed company changes."
        else:
            text = f"The {module} source could not be fully verified in this run; missing information is not a confirmed company change."
        b.say("what_changed", text, [sid], facts)
    for change in changes or []:
        if change.get("material") is False:
            continue
        module = change["field"].split(".", 1)[0]
        source_module = change.get("source_module") or ("registry_live" if module == "registry" and _ev(b.profile, "registry_live") else module)
        record = _ev(b.profile, source_module)
        sid = b.source(f"Current {module} evidence (compared with previous snapshot)", pointer("evidence", source_module), record)
        index = (changes or []).index(change)
        base = pointer("changes", index)
        derived = None
        if isinstance(change.get("old_value"), list) or isinstance(change.get("new_value"), list):
            o, n = set(map(str, change.get("old_value") or [])), set(map(str, change.get("new_value") or []))
            derived = [{"op": "set_added", "inputs": [base + "/old_value", base + "/new_value"], "value": len(n - o)},
                       {"op": "set_removed", "inputs": [base + "/old_value", base + "/new_value"], "value": len(o - n)}]
        b.say("what_changed", "Since the previous snapshot — " + _describe_change(change), [sid],
              [(base + "/field", change["field"]), (base + "/old_value", change.get("old_value")), (base + "/new_value", change.get("new_value"))], derived)
    count = len([c for c in changes or [] if c.get("material", True)])
    text = f"{count} material change{'s' if count != 1 else ''} since the previous snapshot." if count else "No material changes since the previous snapshot in the tracked fields."
    if failed:
        text = (text if count else "No material changes were confirmed in the comparable tracked fields.") + " Some source observations were unavailable or collection coverage was incomplete or unknown; the comparison is incomplete (see below)."
    return {"status": "compared", "count": count, "refresh_failures": len(failed), "previous": refresh.get("previous"), "text": text}


def _unknowns(b: _Builder) -> tuple[list[dict], dict]:
    p = b.profile
    raw = (_ev(p, "registry").get("value") or {})
    site = _ev(p, "site_signals")
    fs = (site.get("value") or {}).get("field_states") or {}
    web = _ev(p, "website")
    states: dict[str, dict] = {}

    def put(key, state, reason, record=None, ptr=None):
        entry = {"state": state, "reason": reason}
        if record and record.get("source_url"):
            entry["checked_source"] = record.get("source_url")
            entry["retrieved_at"] = record.get("retrieved_at")
        states[key] = entry

    identity = registry_field(p, "name")
    put("registry_identity", identity["status"], identity["note"] or "Brønnøysund live entity register", identity["record"])
    fin = _ev(p, "financials")
    recs = (fin.get("value") or {}).get("records") or []
    put("financials", _status(fin) if not (fin.get("status") == "available" and not recs) else "not_available", "Regnskapsregisteret annual accounts" + ("" if recs or fin.get("status") != "available" else ": no filed accounts returned"), fin)
    roles = _ev(p, "roles")
    active_roles = [r for r in (roles.get("value") or {}).get("roles") or [] if not r.get("inactive")]
    put("roles", "available" if active_roles else ("not_available" if roles.get("status") == "available" else _status(roles)), "Brønnøysund roles register", roles)
    loc = _ev(p, "locations")
    put("locations", _status(loc), "Brønnøysund sub-unit register" + (" (checked: zero sub-units)" if loc.get("status") == "available" and not (loc.get("value") or {}).get("locations") else ""), loc)
    grp = _ev(p, "group")
    put("group", _status(grp), "Brønnøysund group structure" + (": not part of a registered group" if grp.get("status") == "not_found" else ""), grp)
    emp = registry_field(p, "employees")
    put("employees", emp["status"], emp["note"] or "Brønnøysund live entity register: employee count published", emp["record"])
    if web.get("official_website"):
        put("official_website", "available", "identity-verified site", web)
    else:
        wstate = _status(web)
        reason = {"not_available": "no site carrying the org number or legal name + address was found (registry site and discovery candidates checked)", "ambiguous": "a site was found but could not be proven to belong to this exact entity, so it is not published", "blocked": "the candidate site disallows automated access (robots.txt)", "failed": "website check failed"}.get(wstate if wstate != "available" else "ambiguous", "not verified")
        put("official_website", wstate if wstate != "available" else "ambiguous", reason, web)
    site_state = _status(site)
    for key, label in (("social_profiles", "social_profiles"), ("news_posts", "news_posts"), ("careers", "careers"), ("contact", "contact"), ("business_description", "business_description")):
        if site_state == "available":
            put(key, AVAILABILITY.get(fs.get(label) or "not_found", "not_available"), "checked on the verified official website", site)
        elif site_state == "not_applicable":
            put(key, "not_applicable", "no verified official website to read company-owned signals from", site)
        else:
            put(key, site_state, "verified-site signals not published (" + str(site.get("note") or site_state) + ")", site)
    nav = _ev(p, "nav_jobs")
    site_jobs = ((site.get("value") or {}).get("careers") or {}).get("postings") or []
    nav_jobs = (nav.get("value") or {}).get("postings") or []
    if nav_jobs or site_jobs:
        put("job_postings", "available", "Nav job-vacancy feed (employer org. no. match)" + (" and company careers pages" if site_jobs else ""), nav)
    else:
        put("job_postings", "not_available" if nav.get("status") in ("available", "not_found") else _status(nav), "No active ads confirmed in the checked Nav records for this org number or its sub-units; this is not an exhaustive absence claim" + ("; company careers pages checked" if fs.get("job_postings") else ""), nav)

    unknowns = []
    for key, label in INFO_TYPES:
        entry = states.get(key)
        if entry and entry["state"] != "available":
            unknowns.append({"info_type": key, "label": label, **entry})
    for module, field, label in (("nav_jobs", "job_postings", "Additional Nav job ads"), ("site_signals", "news_posts", "Additional news/blog posts")):
        record = _ev(p, module)
        value = record.get("value") or {}
        coverage = value if module == "nav_jobs" else value.get("news_posts_collection") or {}
        if record.get("status") == "available" and coverage.get("collection_complete") is not True:
            unknowns.append({"info_type": field + "_coverage", "label": label, "state": "not_available", "reason": "Collection is incomplete or completeness is unknown; observed items do not establish the total or an exhaustive latest item."})
    return unknowns, states


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


SECTION_ORDER = ["what_it_does", "key_signals", "size", "financials", "people", "locations", "digital_footprint", "hiring", "recent_activity", "what_changed"]
SECTION_TITLES = {
    "what_it_does": "What the company does", "key_signals": "Key signals", "size": "Size", "financials": "Financial snapshot", "people": "People",
    "locations": "Locations and group", "digital_footprint": "Digital footprint", "hiring": "Hiring",
    "recent_activity": "Recent public activity", "what_changed": "What changed",
}


def _template_statements(profile: dict[str, Any], changes: list[dict] | None = None, refresh: dict | None = None) -> tuple[_Builder, dict]:
    """Build the fixed, auditable templates without invoking validation."""
    b = _Builder(profile)
    _identity(b)
    _description(b)
    _financials(b)
    _signals(b)
    _people(b)
    _locations(b)
    _digital(b)
    change_summary = _changes(b, refresh, changes)
    return b, change_summary


def build_synthesis(profile: dict[str, Any], *, changes: list[dict] | None = None, refresh: dict | None = None) -> dict[str, Any]:
    b, change_summary = _template_statements(profile, changes, refresh)
    unknowns, availability = _unknowns(b)
    doc = {"organisation_number": profile.get("organisation_number"), "changes": changes or []}
    check = validate_synthesis({"statements": b.statements, "sources": b.sources}, {**profile, **doc})
    statements = [s for s in b.statements if s["id"] not in check["failed_ids"]]
    used = {c for s in statements for c in s["citations"]}
    sources = [s for s in b.sources if s["id"] in used]
    sections = []
    for key in SECTION_ORDER:
        items = [s for s in statements if s["section"] == key]
        if items:
            sections.append({"key": key, "title": SECTION_TITLES[key], "statement_ids": [s["id"] for s in items]})
    headline_parts = [s for s in statements if s["section"] == "what_it_does"][:1]
    summary = " ".join(f"{s['text']} " + "".join(f"[{c}]" for c in s["citations"]) for s in statements if s["section"] not in ("what_changed",))
    summary += " " + change_summary["text"]
    overview_ids: list[str] = []
    for key, limit in (("what_it_does", 1), ("size", 1), ("key_signals", 4), ("hiring", 1), ("recent_activity", 1)):
        overview_ids += [st["id"] for st in statements if st["section"] == key][:limit]
    unknown_text = ("Not found or not verifiable: " + "; ".join(f"{u['label'].lower()} ({u['state'].replace('_', ' ')})" for u in unknowns) + ".") if unknowns else "Every tracked information type has supported evidence."
    return {
        "method": SYNTHESIS_VERSION,
        "language": "en",
        "headline": (headline_parts[0]["text"] + " " + "".join(f"[{c}]" for c in headline_parts[0]["citations"])) if headline_parts else None,
        "summary": summary.strip(),
        "overview": {
            "statement_ids": overview_ids,
            "text": " ".join(f"{st['text']} " + "".join(f"[{c}]" for c in st["citations"]) for st in statements if st["id"] in overview_ids).strip(),
            "rule": "verbatim selection of supported statements: identity, size, key signals, hiring and latest activity",
        },
        "statements": statements,
        "sections": sections,
        "what_changed": change_summary,
        "unknowns": unknowns,
        "unknowns_text": unknown_text,
        "availability": availability,
        "sources": sources,
        "validation": {
            "passed": not check["failed_ids"],
            "statements_checked": len(b.statements),
            "statements_published": len(statements),
            "dropped": check["failures"],
            "rule": "every statement exactly matches a deterministic template regenerated from the profile, including facts and citation bindings; each source pointer and provenance matches the profile; numbers and derived values are checked",
        },
        "llm_polish": {"status": "not_configured"},
    }


def validate_synthesis(synthesis: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    # Regenerate rather than mistaking shared numbers or names for factual support.
    # Changes already carry their old/new provenance; refresh metadata is not needed
    # to validate the wording of an existing change statement.
    expected, _ = _template_statements(profile, profile.get("changes") or [], {
        "previous_snapshot_supplied": True, "company_in_previous_snapshot": True,
    })
    expected_statements = {s["id"]: s for s in expected.statements}
    expected_sources = {s["id"]: s for s in expected.sources}
    sources = synthesis.get("sources") or []
    source_ids = {s["id"]: s for s in sources}
    failures = []
    if len(source_ids) != len(sources):
        failures.append({"id": None, "problems": ["duplicate source ids"]})
    invalid_sources = set()
    for src in sources:
        problems = []
        if src != expected_sources.get(src["id"]):
            problems.append("source does not match regenerated evidence binding")
        try:
            record = resolve_pointer(profile, src.get("evidence_pointer"))
            if not isinstance(record, dict) or not str(src.get("evidence_pointer") or "").startswith("/evidence/"):
                raise ValueError("not an evidence record")
            for key in ("source_url", "retrieved_at", "content_sha256"):
                if src.get(key) != record.get(key):
                    problems.append(f"source {key} mismatch")
            if not src.get("source_url"):
                problems.append("source URL missing")
        except (KeyError, IndexError, ValueError, TypeError):
            problems.append("unresolvable source evidence pointer")
        if problems:
            invalid_sources.add(src["id"])
            failures.append({"id": None, "problems": problems[:5]})
    seen_ids = set()
    for s in synthesis.get("statements") or []:
        problems = []
        expected_statement = expected_statements.get(s.get("id"))
        if not expected_statement or any(s.get(key) != expected_statement.get(key) for key in (
            "section", "text", "citations", "facts", "derived",
        )):
            problems.append("statement does not match a supported deterministic template")
        if s.get("id") in seen_ids:
            problems.append("duplicate statement id")
        seen_ids.add(s.get("id"))
        if not s.get("citations"):
            problems.append("no citation")
        for c in s.get("citations") or []:
            src = source_ids.get(c)
            if not src:
                problems.append(f"unknown citation {c}")
            elif c in invalid_sources or src != expected_sources.get(c):
                problems.append(f"citation {c} does not match its source evidence")
        allowed: set[str] = set()
        for fact in s.get("facts") or []:
            try:
                actual = resolve_pointer(profile, fact["pointer"])
            except (KeyError, IndexError, ValueError, TypeError):
                problems.append(f"unresolvable pointer {fact['pointer']}")
                continue
            projected = (isinstance(fact["value"], dict) and isinstance(actual, dict) and fact["value"] != actual) or (
                isinstance(fact["value"], list) and isinstance(actual, list) and actual and isinstance(actual[0], dict))
            if projected:
                # The exact template comparison above verifies this projection's
                # keys/ids and selected subset, including non-numeric content.
                # A dated-news subset can legitimately be smaller than all news.
                allowed |= _value_tokens(fact["value"]) | ({str(len(fact["value"]))} if isinstance(fact["value"], list) else set())
                continue
            if actual != fact["value"]:
                problems.append(f"value mismatch at {fact['pointer']}")
            allowed |= _value_tokens(actual)
            if isinstance(actual, list):
                allowed.add(str(len(actual)))
        for d in s.get("derived") or []:
            try:
                a, bval = (resolve_pointer(profile, p) for p in d["inputs"])
                if d["op"] == "ratio_pct" and bval and round(a / bval * 100, 1) == d["value"]:
                    allowed |= _value_tokens(float(d["value"])) | {str(d["value"]), str(abs(d["value"]))}
                elif d["op"] == "pct_change" and round((a - bval) / bval * 100, 1) == d["value"]:
                    allowed |= _value_tokens(float(d["value"])) | {str(d["value"]), str(abs(d["value"]))}
                elif d["op"] in ("set_added", "set_removed"):
                    o, n = set(map(str, a or [])), set(map(str, bval or []))
                    if len(n - o if d["op"] == "set_added" else o - n) != d["value"]:
                        problems.append("derived value mismatch")
                    allowed.add(str(d["value"]))
                else:
                    problems.append("derived value mismatch")
            except Exception:  # noqa: BLE001
                problems.append("derived value unresolvable")
        # source spans/urls cited may also legitimately contain numbers (e.g. URLs in the text)
        for c in s.get("citations") or []:
            if c in source_ids:
                allowed |= _value_tokens(source_ids[c].get("source_url"))
        for token in _number_tokens(s.get("text") or ""):
            if token not in allowed and token.lstrip("+-") not in allowed:
                problems.append(f"unsupported number {token}")
        if problems:
            failures.append({"id": s.get("id"), "problems": problems[:5]})
    overview = synthesis.get("overview")
    if overview is not None:
        published = {st.get("id"): st for st in synthesis.get("statements") or [] if st.get("id") not in {f["id"] for f in failures}}
        ids = overview.get("statement_ids") or []
        rebuilt = " ".join(f"{published[i]['text']} " + "".join(f"[{c}]" for c in published[i]["citations"]) for i in ids if i in published).strip()
        if any(i not in published for i in ids) or overview.get("text") != rebuilt:
            failures.append({"id": None, "problems": ["overview is not a verbatim selection of supported statements"]})
    polish = synthesis.get("llm_polish") or {}
    if polish.get("narrative") is not None:
        problems = check_narrative(polish["narrative"], synthesis)
        if polish.get("status") != "accepted":
            problems.append("unaccepted narrative must not be published")
        if problems:
            failures.append({"id": None, "problems": problems})
    return {"passed": not failures, "failures": failures, "failed_ids": {f["id"] for f in failures}}


# --------------------------------------------------------------------------- optional LLM polish

def llm_config() -> dict[str, str] | None:
    key = os.environ.get("SIGNALPOST_LLM_API_KEY")
    if not key:
        return None
    return {
        "api_key": key,
        "base_url": os.environ.get("SIGNALPOST_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("SIGNALPOST_LLM_MODEL", "gpt-4o-mini"),
        "timeout": os.environ.get("SIGNALPOST_LLM_TIMEOUT_S", "20"),
    }


def _chat(config: dict[str, str], messages: list[dict]) -> dict:
    body = json.dumps({"model": config["model"], "messages": messages, "temperature": 0}).encode()
    request = urllib.request.Request(config["base_url"] + "/chat/completions", data=body, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=float(config["timeout"])) as response:  # noqa: S310 - fixed, configured endpoint
        payload = json.loads(response.read())
    return {"content": payload["choices"][0]["message"]["content"], "usage": payload.get("usage"), "model": payload.get("model"), "response_id": payload.get("id")}


def check_narrative(narrative: str, synthesis: dict[str, Any]) -> list[str]:
    """Only allow whole verified statements with their original citations, verbatim.

    Token/name checks cannot detect changed predicates, negation or invented claims.
    Selection and reordering are the only supported LLM operations. Normalize layout
    whitespace, but do not relax words, punctuation, citation membership or ordering.
    """
    normalize = lambda text: re.sub(r"\s+", " ", text).strip()
    if not isinstance(narrative, str) or not narrative.strip():
        return ["empty narrative"]
    remaining = normalize(narrative)
    choices = {
        normalize(s["text"] + " " + "".join(f"[{c}]" for c in s["citations"]))
        for s in synthesis.get("statements") or []
        if s.get("text") and s.get("citations")
    }
    choices = sorted(choices, key=len, reverse=True)
    while remaining:
        match = next((text for text in choices if remaining == text or remaining.startswith(text + " ")), None)
        if match is None:
            return ["narrative is not a verbatim selection of supported statements and citations"]
        remaining = remaining[len(match):].lstrip()
    return []


def polish_with_llm(synthesis: dict[str, Any], config: dict[str, str] | None = None, chat=_chat) -> dict[str, Any]:
    config = config or llm_config()
    if not config:
        return synthesis
    facts = [{"citations": s["citations"], "text": s["text"]} for s in synthesis.get("statements") or []]
    messages = [
        {"role": "system", "content": "Select up to 6 of the supplied statements for a concise company overview. Copy each selected text VERBATIM in full, even when it contains several sentences, then a space and ALL its citation markers in the supplied order, e.g. [S1][S3]. You may only select and reorder complete statements. Do not paraphrase, combine, shorten, explain, add headings, or add unknowns. Separate selections with whitespace."},
        {"role": "user", "content": json.dumps({"statements": facts}, ensure_ascii=False)},
    ]
    accounting = {"requests": 1, "cost_usd": None, "cost_status": "unknown", "cost_provenance": "Provider request attempted; no verified USD charge supplied."}
    try:
        response = chat(config, messages)
        if isinstance(response, dict):
            accounting.update({"usage": response.get("usage"), "provider_model": response.get("model"), "response_id": response.get("response_id")})
            # Only an explicitly USD-denominated reported charge is actual cost.
            charge = (response.get("usage") or {}).get("cost_usd")
            if isinstance(charge, (int, float)) and not isinstance(charge, bool) and math.isfinite(charge) and charge >= 0:
                accounting.update(cost_usd=charge, cost_status="provider_reported", cost_provenance="Provider response usage.cost_usd")
            narrative = response.get("content", "").strip()
        else:
            narrative = response.strip()
    except Exception as exc:  # noqa: BLE001 - optional step never fails the run
        synthesis["llm_polish"] = {"status": "failed", "model": config["model"], "error": f"{type(exc).__name__}: {str(exc)[:160]}", "accounting": accounting}
        return synthesis
    problems = check_narrative(narrative, synthesis)
    if problems:
        synthesis["llm_polish"] = {"status": "rejected_by_post_check", "model": config["model"], "problems": problems[:8]}
    else:
        synthesis["llm_polish"] = {"status": "accepted", "model": config["model"], "narrative": narrative, "post_check": "verbatim selection of complete supported statements with unchanged citations; no free rewriting"}
    synthesis["llm_polish"]["accounting"] = accounting
    return synthesis


def synthesis_cost(synthesis: dict | None) -> dict:
    """Unknown attempted-call billing must never be reported as a free run."""
    polish = (synthesis or {}).get("llm_polish") or {}
    accounting = polish.get("accounting")
    if accounting:
        return accounting
    if polish.get("status", "not_attempted") in {"not_attempted", "not_configured"}:
        return {"requests": 0, "cost_usd": 0, "cost_status": "not_incurred", "cost_provenance": "No LLM request attempted."}
    return {"requests": 1, "cost_usd": None, "cost_status": "unknown", "cost_provenance": "LLM attempt without billing metadata."}

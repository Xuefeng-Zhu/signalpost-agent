from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any

from .website_discovery import assess_org_proof, find_org_numbers


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


_LEGAL_SUFFIX_RE = re.compile(
    r"((?:[0-9A-Za-zÆØÅæøåÉéÜüÖöÄä&'\-]+\s+){1,12}?)"
    r"(ASA|A/S|AS|ANS|DA|SA|NUF|BA|KS|SUS|ENK|IKS|STI|SF|KF|BBL|BRL|ESEK)(?![0-9A-Za-zÆØÅæøå])",
    re.I,
)
# These introduce names in ordinary prose rather than distinguishing a legal entity.
_NAME_INTRODUCERS = {"velkommen", "til", "welcome", "to", "om", "about", "kontakt", "contact",
                     "hos", "fra", "by", "of", "is", "er", "are", "og", "and"}


def competing_legal_names(legal_name: Any, texts: list[Any], body_texts: list[Any] | None = None) -> list[str]:
    """Find incompatible legal names, retaining prefix qualifiers and legal forms.

    Token inclusion alone cannot distinguish Bergen Consto AS from Consto AS, or
    Consto ASA from Consto AS. Case and the spelling A/S do not change identity.
    Ambiguous phrases are retained conservatively rather than asserted exact.
    """
    core = _tokens(legal_name)
    if not core:
        return []
    core_set = set(core)
    target_matches = list(_LEGAL_SUFFIX_RE.finditer(str(legal_name or "")))
    target_form = target_matches[-1].group(2).casefold().replace("/", "") if target_matches else None
    found: list[str] = []
    # ``texts`` are identity statements (title, structured names, site name): prefix qualifiers count
    # there. ``body_texts`` are flattened page text, where menus and headings run straight into the
    # name ("HELE SAKEN Egil Ulvan Rederi AS"), so only a qualifier between the name and its legal
    # form ("Consto Bergen AS") or a different legal form counts without corroboration.
    # Body prefixes need corroboration from explicit identity fields.
    identity_tokens = set(_tokens(" ".join(str(text or "") for text in texts)))
    tagged = [(text, True) for text in texts] + [(text, False) for text in (body_texts or [])]
    for text, allow_prefix in tagged:
        for match in _LEGAL_SUFFIX_RE.finditer(str(text or "")):
            words = match.group(1).split()
            # Trim prose introductions, but never silently discard a name qualifier
            # merely because the shortest matching tail contains the target tokens.
            # The name sits directly before the legal suffix: take the shortest tail holding every core
            # token, then add a prefix qualifier ("Bergen Consto AS"): up to two adjacent name words
            # written in the same case style as the name itself. Prose ("utarbeidet av Insite Media AS"),
            # file names ("png Arkitektfirma ... as"), separators and introducers end the run.
            first_core = None
            for i in range(len(words) - 1, -1, -1):
                if core_set.issubset(_tokens(" ".join(words[i:]))):
                    first_core = i
                    break
            if first_core is None:
                continue
            core_word = words[first_core].strip("'-&")
            core_lower = core_word.islower()

            def _same_case(raw: str) -> bool:
                bare = raw.strip("'-&")
                return bool(bare) and bare.replace("-", "").isalpha() and (bare.islower() if core_lower else bare[0].isupper())

            last_core = max(i for i, word in enumerate(words) if core_set.intersection(_tokens(word)))
            trailing = words[last_core + 1:]
            if len(trailing) > 2 or not all(_same_case(word) for word in trailing):
                continue  # prose ending in the word "as", not a legal name
            start = first_core
            while allow_prefix and start > 0 and first_core - start < 2:
                word = words[start - 1]
                bare = word.strip("'-&")
                same_case = bool(bare) and (bare.islower() if core_lower else bare[0].isupper())
                if (not same_case or not bare.replace("-", "").isalpha()
                        or word.casefold() in _NAME_INTRODUCERS or word.endswith(("-", ":", "|"))):
                    break
                start -= 1
            if not allow_prefix and start > 0:
                # Flattening loses H1/footer boundaries. A prefix is meaningful when
                # independently corroborated by the title/site name. Repeating a
                # menu label does not turn it into a legal-name prefix. H1/footer
                # boundaries are captured separately as identity statements.
                prefix = words[start - 1]
                bare = prefix.strip("'-&")
                if (_same_case(prefix) and bare.casefold() not in _NAME_INTRODUCERS
                        and not prefix.endswith(("-", ":", "|"))
                        and set(_tokens(prefix)) & (identity_tokens - core_set)):
                    start -= 1
            words = words[start:]
            window = _tokens(" ".join(words))
            form = match.group(2).casefold().replace("/", "")
            if core_set.issubset(window) and (window != core or (target_form and form != target_form)):
                phrase = " ".join(words) + " " + match.group(2)
                if phrase not in found:
                    found.append(phrase)
    return found


def html_identity_statements(soup: Any) -> list[str]:
    """Preserve explicit identity-node boundaries before HTML is flattened.

    Body/menu words are not identity statements. Footer navigation is excluded,
    and independent footer blocks remain separate so menu text cannot become a
    spurious legal-name prefix. Inline spans within a name remain joined.
    """
    import json
    from bs4 import BeautifulSoup

    names: list[str] = []
    def add(value: Any) -> None:
        text = " ".join(str(value or "").split())
        if text and len(text) <= 500 and text not in names:
            names.append(text)
    for node in soup.select('h1, [itemprop="legalName"], meta[property="og:site_name"]'):
        if not node.find_parent(["nav", "menu"]):
            add(node.get("content") or node.get_text(" ", strip=True))
    for footer in soup.select('footer, [role="contentinfo"]'):
        cleaned = BeautifulSoup(str(footer), "lxml")
        for node in cleaned.select("nav, menu, ul, ol, script, style, [role=navigation]"):
            node.decompose()
        blocks = cleaned.select("p, div, address, section")
        for block in blocks:
            if not block.select("p, div, address, section"):
                add(block.get_text(" ", strip=True))
        for block in cleaned.select("p, div, address, section"):
            if block.parent is not None:
                block.decompose()
        add(cleaned.get_text(" ", strip=True))
    def legal_names(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("legalName"), str):
                add(value["legalName"])
            for child in value.values():
                legal_names(child)
        elif isinstance(value, list):
            for child in value:
                legal_names(child)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            legal_names(json.loads(script.get_text()))
        except (ValueError, TypeError):
            pass
    return names[:50]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"), *value.get("identity_statements", []),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    # Match the organisation number only as a standalone formatted run, never as a substring of a
    # longer concatenation of digits (phone lists, account numbers, IDs).
    homepage_org_hit = bool(org_digits) and any(item["digits"] == org_digits for item in find_org_numbers(homepage_candidate_text))
    incompatible_org_numbers = sorted({
        item["digits"] for part in candidate_parts for item in find_org_numbers(str(part or ""))
        if org_digits and item["valid"] and item["digits"] != org_digits
    })
    page_signals = [page for page in value.get("pages", []) if page.get("identity_signals")]
    org_proof = assess_org_proof(page_signals) if page_signals else None
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    competing = competing_legal_names(profile.get("name"), [part for part in homepage_identity_parts if part is not value.get("identity_text_excerpt")],
                                      [value.get("identity_text_excerpt"), value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
    if incompatible_org_numbers:
        score = 0.5
        reasons.append("conflicting identity evidence: other valid organisation numbers on the site: " + ", ".join(incompatible_org_numbers))
    elif org_proof and org_proof.get("status") == "exact" and not any(marker in normalized_raw for marker in parked_markers):
        score = float(org_proof.get("score") or 0.97)
        reasons.extend(org_proof.get("evidence_for") or ["exact-entity proof on the site"])
    elif org_proof and org_proof.get("status") == "conflict":
        score = 0.5
        reasons.append("conflicting identity evidence: " + "; ".join(org_proof.get("evidence_against") or []))
    elif any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif homepage_org_hit:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif competing:
        score = 0.6
        reasons.append("page names a different legal entity containing this name (" + "; ".join(competing[:3]) + ") and no organisation-number proof")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "org_number_proof": org_proof,
        "competing_legal_names": competing,
        "method": "deterministic_name_org_evidence_v5",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path)
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v1",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    if not assessment["publishable"]:
        # A fetched but unverified site is not a claim: keep it for audit, never as "available".
        website["status"] = "ambiguous"
        website["note"] = "Site fetched but exact-entity identity not proven; not published as the official website"
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }

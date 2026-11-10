"""Official-website discovery for entities whose registry website is absent, dead or unverified.

Candidate sources (all keyless, no search-results scraping):
  1. the Brreg ``hjemmeside`` field (handled first by ``website.fetch_website``);
  2. the domain of the Brreg ``epostadresse`` field, excluding free-mail/ISP providers;
  3. domains guessed from the normalized legal name (.no first, then .com).

A candidate is only ever a lead. Publication requires an identity proof fetched from the candidate
site itself: the exact 9-digit organisation number (any common Norwegian formatting) on the homepage
or on a same-site contact/about/privacy/terms page, with no competing organisation numbers and no
group/parent markers. A full legal name plus the registered postcode and street is accepted as a
secondary proof. Everything else is returned as ``ambiguous`` or ``not_found`` with the candidates
and the evidence for and against each one.
"""
from __future__ import annotations

import hashlib
import itertools
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bs4 import BeautifulSoup
import tldextract

from .evidence import utc_now
from .website import USER_AGENT, SafeRedirectHandler, assert_public_url

METHOD = "org_number_site_proof_v1"

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "sa", "nuf", "ba", "ks", "iks", "sti", "sf", "kf", "bbl", "brl", "esek",
}
DROPPABLE_DESCRIPTORS = {
    "holding", "invest", "investering", "norge", "norway", "nordic", "group", "gruppen", "capital", "eiendom",
    "consulting", "services", "service", "industrier", "og", "i",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no",
    "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "online.no", "broadpark.no", "lyse.net", "getmail.no", "frisurf.no", "start.no", "c2i.net", "chello.no",
    "altibox.no", "ebnett.no", "tele2.no", "epost.no", "mail.com", "gmx.com", "gmx.net", "enivest.net",
    "haugnett.no", "bluezone.no", "loqal.no", "tussa.com", "nettfiber.no", "sensewave.com", "vikenfiber.no",
    "telenor.com", "telenor.no", "kpnmail.no", "netcom.no", "frisurf.com", "hotmail.co.uk", "yahoo.co.uk",
    "e-post.no", "mimer.no", "tdcadsl.no", "monsternett.no", "sfjbb.net", "vestnett.no", "fiberpost.no",
}
# Hosts that list many companies, sell domains, or host profiles: never an entity's official site.
BLOCKED_SITE_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no", "firmadatabasen.no",
    "sokfirma.no", "yra.no", "northdata.com", "nor47business.com", "regnskapstall.no", "brreg.no", "kompass.com",
    "bizbook.no", "bedriftsdatabasen.no", "infobel.com", "enirogroup.com", "finn.no", "mittanbud.no",
    "anbudstorget.no", "byggstart.no", "facebook.com", "linkedin.com", "instagram.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "sedo.com", "dan.com", "afternic.com", "hugedomains.com", "godaddy.com",
    "domeneshop.no", "one.com", "uniweb.no", "webhuset.no", "pro.no", "domainnameshop.com", "parkingcrew.net",
    "bodis.com", "above.com", "undeveloped.com", "squarespace.com", "wix.com", "wordpress.com", "google.com",
    "amazon.com", "ebay.com", "tripadvisor.com", "booking.com", "airbnb.com", "styrerommet.net", "styrerommet.no",
    "vipps.no", "visma.no",
}
PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "this domain may be for sale", "buy this domain", "domenet er til salgs",
    "domenet kan vaere til salgs", "hugedomains", "parked at", "parkingcrew", "sedoparking", "this domain is parked",
    "domain parking", "denne domenen er registrert", "dette domenet er registrert", "her flytter snart en ny gjest",
    "miss hosting", "has been informing visitors", "find the best information and most relevant links on all topics",
    "default web site page", "welcome to nginx", "apache2 ubuntu default page", "it works!", "index of /",
    "domenet er parkert", "website is under construction", "future home of something quite cool",
)
GROUP_MARKERS = ("konsern", "konsernet", "vare selskaper", "våre selskaper", "our companies", "datterselskap", "subsidiaries", "portefoljeselskap", "porteføljeselskap", "portfolio companies")
IDENTITY_LINK_TERMS = (
    "kontakt", "contact", "om-oss", "om_oss", "om oss", "about", "personvern", "privacy", "cookie", "vilkar",
    "vilkår", "terms", "betingelser", "salgsbetingelser", "impressum", "firma", "selskapet", "informasjon",
)
FALLBACK_PATHS = ("/kontakt", "/om-oss", "/personvern")

DIGIT_RUN = re.compile(r"\d+(?:[ .\u00a0\u202f-]\d+)*")


class _BudgetExhausted(Exception):
    pass


_UNREACHABLE = object()


class _ChainRedirectHandler(SafeRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            chain = list(getattr(req, "redirect_chain", []) or [req.full_url])
            chain.append(newurl)
            new.redirect_chain = chain
        return new


_OPENER = urllib.request.build_opener(_ChainRedirectHandler())


# ---------------------------------------------------------------------------
# Organisation-number detection
# ---------------------------------------------------------------------------

def valid_org_number(digits: str) -> bool:
    """Brreg modulus-11 check digit (weights 3,2,7,6,5,4,3,2)."""
    if not re.fullmatch(r"\d{9}", digits or ""):
        return False
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits[:8], weights))
    check = 11 - total % 11
    if check == 11:
        check = 0
    return check != 10 and check == int(digits[8])


def find_org_numbers(text: str) -> list[dict[str, Any]]:
    """Return 9-digit numbers formatted as a standalone run ("123456789", "123 456 789", "NO 123.456.789 MVA").

    Digit runs that join into more than nine digits (phone lists, account numbers) are ignored, so a
    target number can never be matched as a substring of a longer number.
    """
    found = []
    for match in DIGIT_RUN.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) != 9:
            continue
        start, end = match.span()
        found.append({
            "digits": digits,
            "valid": valid_org_number(digits),
            "span": " ".join(text[max(0, start - 60): min(len(text), end + 40)].split()),
        })
    return found


def _ascii_fold(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _words(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _ascii_fold(value))


def identity_hints(profile: dict[str, Any]) -> dict[str, Any]:
    raw = (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
    street_line = str(raw.get("forretningsadresse.adresse") or "").split("\n")[0]
    street_words = [w for w in _words(street_line) if not w.isdigit() and len(w) > 2]
    return {
        "organisation_number": re.sub(r"\D", "", str(profile.get("organisation_number") or "")),
        "legal_name": str(profile.get("name") or ""),
        "legal_name_words": _words(profile.get("name")),
        "postcode": str(raw.get("forretningsadresse.postnummer") or "").strip(),
        "street_words": street_words[:3],
    }


def _visible_text(soup: BeautifulSoup) -> str:
    parts = []
    for script in soup.select('script[type="application/ld+json"]'):
        parts.append(script.get_text(" ", strip=True))
    for meta in soup.select("meta[content]"):
        parts.append(str(meta.get("content") or ""))
    for node in soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    parts.insert(0, soup.get_text(" ", strip=True))
    return " ".join(parts)


def page_identity_signals(html: str | bytes, hints: dict[str, Any] | None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    links = [(str(a.get("href") or ""), a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
    text = _visible_text(soup)
    folded = _ascii_fold(text)
    numbers = find_org_numbers(text)
    target = (hints or {}).get("organisation_number") or ""
    target_hits = [item for item in numbers if item["digits"] == target]
    others = sorted({item["digits"] for item in numbers if item["valid"] and item["digits"] != target})
    name_words = (hints or {}).get("legal_name_words") or []
    page_words = " " + " ".join(_words(text)) + " "
    legal_name_present = bool(name_words) and (" " + " ".join(name_words) + " ") in page_words
    postcode = (hints or {}).get("postcode") or ""
    street_words = (hints or {}).get("street_words") or []
    signals = {
        "title": title,
        "target_org_number_found": bool(target_hits),
        "target_org_span": target_hits[0]["span"][:200] if target_hits else None,
        "other_valid_org_numbers": others[:10],
        "legal_name_present": legal_name_present,
        "postcode_present": bool(postcode) and re.search(rf"(?<!\d){re.escape(postcode)}(?!\d)", text) is not None,
        "street_present": bool(street_words) and all(word in page_words for word in street_words),
        "parked_marker": next((marker for marker in PARKED_MARKERS if marker in folded), None),
        "group_marker": next((marker for marker in GROUP_MARKERS if _ascii_fold(marker) in _ascii_fold(title) + " " + folded[:4000]), None),
        "text_length": len(text),
    }
    legal_name = (hints or {}).get("legal_name") or ""
    if legal_name:
        from .identity import competing_legal_names  # local import: identity imports this module

        signals["competing_legal_names"] = competing_legal_names(legal_name, [title, text[:20000]])[:5]
    return {"signals": signals, "links": links}


def assess_org_proof(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide exact-entity identity from per-page signals. ``pages[0]`` must be the homepage."""
    if not pages:
        return {"status": "absent", "publishable": False, "reasons": ["no pages captured"], "method": METHOD}
    home = pages[0].get("identity_signals") or {}
    hits = [page for page in pages if (page.get("identity_signals") or {}).get("target_org_number_found")]
    others = sorted({number for page in pages for number in (page.get("identity_signals") or {}).get("other_valid_org_numbers", [])})
    parked = home.get("parked_marker")
    group = next(((page.get("identity_signals") or {}).get("group_marker") for page in pages if (page.get("identity_signals") or {}).get("group_marker")), None)
    against = []
    if parked:
        against.append(f"parked/placeholder marker on homepage: {parked!r}")
    if others:
        against.append(f"other valid organisation numbers on the site: {', '.join(others[:5])}")
    if hits:
        page = hits[0]
        where = "homepage" if page is pages[0] else "identity_subpage"
        if group and where != "homepage":
            # A group site can list a subsidiary's number on its contact page; only the homepage
            # identity statement is strong enough when the site presents itself as a group.
            against.append(f"group/parent marker on the site: {group!r}")
        result = {
            "status": "exact" if not against else "conflict",
            "proof": "organisation_number",
            "where": where,
            "source_url": page.get("url"),
            "content_sha256": page.get("content_sha256"),
            "retrieved_at": page.get("retrieved_at"),
            "claim_span": (page.get("identity_signals") or {}).get("target_org_span"),
            "evidence_for": [f"exact organisation number on {where} {page.get('url')}"],
            "evidence_against": against,
            "score": 1.0 if where == "homepage" else 0.97,
            "method": METHOD,
        }
        result["publishable"] = result["status"] == "exact"
        return result
    if group:
        against.append(f"group/parent marker on the site: {group!r}")
    # A shared address cannot resolve contradictory legal names anywhere on the site.
    # Exact organisation-number proof above remains the stronger positive evidence.
    competing = list(dict.fromkeys(
        name for page in pages for name in (page.get("identity_signals") or {}).get("competing_legal_names", [])
    ))
    if competing:
        against.append("site also names a different legal entity: " + "; ".join(competing[:3]))
    for page in pages:
        signals = page.get("identity_signals") or {}
        if signals.get("legal_name_present") and signals.get("postcode_present") and signals.get("street_present"):
            status = "exact" if not against else "conflict"
            return {
                "status": status,
                "publishable": status == "exact",
                "proof": "legal_name_and_registered_address",
                "where": "homepage" if page is pages[0] else "identity_subpage",
                "source_url": page.get("url"),
                "content_sha256": page.get("content_sha256"),
                "retrieved_at": page.get("retrieved_at"),
                "claim_span": None,
                "evidence_for": ["full legal name, registered postcode and street on the same page"],
                "evidence_against": against,
                "score": 0.92,
                "method": METHOD,
            }
    # Preserve contradictions for the final gate; name-only fallback must not erase them.
    named = any((page.get("identity_signals") or {}).get("legal_name_present") for page in pages)
    return {
        "status": "conflict" if against else "name_only" if named else "absent",
        "publishable": False,
        "evidence_for": ["legal name appears on the site"] if named else [],
        "evidence_against": against + ["organisation number not found on the captured pages"],
        "method": METHOD,
    }


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

_FOLD_VARIANTS = (
    {"ø": "o", "å": "a", "æ": "ae"},
    {"ø": "oe", "å": "aa", "æ": "ae"},
)


def _name_tokens(name: str, mapping: dict[str, str]) -> list[str]:
    text = str(name or "").casefold().replace("&", " og ")
    text = "".join(mapping.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in {"as", "asa"}:
        tokens.pop(0)
    return tokens


def guess_domains(name: str, *, limit: int = 8) -> list[str]:
    ordered: list[str] = []

    def add(label: str, tld: str) -> None:
        label = label.strip("-")
        if 2 <= len(label) <= 63 and not label.isdigit():
            domain = f"{label}.{tld}"
            if domain not in ordered:
                ordered.append(domain)

    for mapping in _FOLD_VARIANTS:
        tokens = _name_tokens(name, mapping)
        tokens_no_og = [t for t in tokens if t != "og"]
        if not tokens:
            continue
        add("".join(tokens_no_og), "no")
        if len(tokens_no_og) > 1:
            add("-".join(tokens_no_og), "no")
        core = [t for t in tokens_no_og if t not in DROPPABLE_DESCRIPTORS]
        if core and core != tokens_no_og:
            add("".join(core), "no")
            if len(core) > 1:
                add("-".join(core), "no")
    primary = ordered[0].rsplit(".", 1)[0] if ordered else None
    if primary:
        add(primary, "com")
    return ordered[:limit]


def email_domain(email: Any) -> str | None:
    value = str(email or "").strip().casefold()
    if "@" not in value:
        return None
    host = value.rsplit("@", 1)[1].strip(". ")
    ext = tldextract.extract(host)
    registered = ext.top_domain_under_public_suffix
    if not registered or registered in FREE_MAIL_DOMAINS or host in FREE_MAIL_DOMAINS:
        return None
    return registered


def _blocked_host(host: str) -> bool:
    host = (host or "").casefold().removeprefix("www.")
    return any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_SITE_HOSTS)


def build_candidates(profile: dict[str, Any], *, exclude_domains: set[str] | None = None, limit: int = 9) -> list[dict[str, Any]]:
    raw = (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
    exclude = {d.casefold() for d in (exclude_domains or set())}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(domain: str | None, source: str) -> None:
        if not domain:
            return
        domain = domain.casefold()
        if domain in seen or domain in exclude or _blocked_host(domain):
            return
        seen.add(domain)
        candidates.append({"domain": domain, "lead_source": source})

    live = (profile.get("evidence", {}).get("registry_live") or {}).get("value") or {}
    add(email_domain(raw.get("epostadresse")), "brreg_email_domain")
    add(email_domain(live.get("email")), "brreg_email_domain")
    live_site = live.get("website")
    if live_site and live_site != profile.get("website"):
        parsed = urllib.parse.urlparse(live_site if "://" in live_site else "https://" + live_site)
        add(tldextract.extract(parsed.hostname or "").top_domain_under_public_suffix or None, "brreg_live_hjemmeside")
    for domain in guess_domains(str(profile.get("name") or "")):
        add(domain, "legal_name_domain_guess")
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

class _Budget:
    def __init__(self, max_requests: int, deadline_s: float) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.bytes = 0
        self.latencies_ms: list[int] = []
        self.dns_lookups = 0
        self.deadline = time.monotonic() + deadline_s

    def take(self) -> None:
        if self.requests >= self.max_requests or time.monotonic() > self.deadline:
            raise _BudgetExhausted()
        self.requests += 1


def _resolves(host: str) -> bool:
    try:
        assert_public_url(f"https://{host}/")
        return True
    except ValueError:
        return False


def _resolve_many(hosts: list[str], timeout: float) -> dict[str, bool]:
    if not hosts:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(8, len(hosts)))
    futures = {host: pool.submit(_resolves, host) for host in hosts}
    result = {}
    deadline = time.monotonic() + timeout
    for host, future in futures.items():
        try:
            result[host] = future.result(timeout=max(0.05, deadline - time.monotonic()))
        except Exception:
            result[host] = False
    pool.shutdown(wait=False, cancel_futures=True)
    return result


def _get(url: str, budget: _Budget, timeout: float, max_bytes: int) -> dict[str, Any]:
    budget.take()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    request.redirect_chain = [url]
    started = time.monotonic()
    try:
        assert_public_url(url)
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            budget.latencies_ms.append(elapsed)
            budget.bytes += len(raw)
            final_url = response.geturl()
            assert_public_url(final_url)
            return {
                # Oversized pages are parsed truncated rather than discarded; identity text is usually early or in a footer we may miss.
                "ok": "html" in response.headers.get("content-type", "html").lower() or url.endswith("/robots.txt"),
                "truncated": len(raw) > max_bytes,
                "status": response.status,
                "requested_url": url,
                "final_url": final_url,
                "redirect_chain": [*getattr(request, "redirect_chain", [url])],
                "raw": raw[:max_bytes],
                "retrieved_at": utc_now(),
            }
    except urllib.error.HTTPError as exc:
        budget.latencies_ms.append(int((time.monotonic() - started) * 1000))
        return {"ok": False, "status": exc.code, "requested_url": url, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - every failure is a recorded candidate outcome
        budget.latencies_ms.append(int((time.monotonic() - started) * 1000))
        return {"ok": False, "status": 0, "requested_url": url, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}


def _robots_ok(base: str, budget: _Budget, timeout: float, cache: dict[str, urllib.robotparser.RobotFileParser | None]) -> urllib.robotparser.RobotFileParser | None:
    if base in cache:
        return cache[base]
    parser = None
    result = _get(base + "/robots.txt", budget, timeout, 300_000)
    if result.get("status") == 0:
        # Connection-level failure (refused, TLS, timeout): the homepage on this base will fail too.
        cache[base] = _UNREACHABLE
        return _UNREACHABLE
    if result.get("status") == 200 and result.get("raw") is not None:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(result["raw"].decode("utf-8", errors="replace").splitlines())
    cache[base] = parser
    return parser


def _allowed(parser: Any, url: str) -> bool:
    return True if parser is None or parser is _UNREACHABLE else parser.can_fetch(USER_AGENT, url)


def _registered_domain(url: str) -> str:
    return tldextract.extract(urllib.parse.urlparse(url).hostname or "").top_domain_under_public_suffix


def _page_record(result: dict[str, Any], hints: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    parsed = page_identity_signals(result["raw"], hints)
    return {
        "url": result["final_url"],
        "requested_url": result["requested_url"],
        "redirect_chain": result.get("redirect_chain"),
        "status": result.get("status"),
        "retrieved_at": result.get("retrieved_at"),
        "content_sha256": hashlib.sha256(result["raw"]).hexdigest(),
        "identity_signals": parsed["signals"],
    }, parsed["links"]


def _identity_links(base_url: str, links: list[tuple[str, str]], limit: int) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    domain = _registered_domain(base_url)
    ranked: dict[str, int] = {}
    for href, label in links:
        url = urllib.parse.urljoin(base_url, href.strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or _registered_domain(url) != domain:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/") or re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?)$", parsed.path, re.I):
            continue
        haystack = _ascii_fold(parsed.path + " " + label)
        rank = next((i for i, term in enumerate(IDENTITY_LINK_TERMS) if _ascii_fold(term) in haystack), None)
        if rank is not None:
            ranked[clean] = min(rank, ranked.get(clean, rank))
    return [url for url, _ in sorted(ranked.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))[:limit]]


def probe_site(domain: str, hints: dict[str, Any], budget: _Budget, *, timeout: float = 6.0, max_bytes: int = 4_000_000,
               max_subpages: int = 3, require_name_for_subpages: bool = True, robots_cache: dict | None = None) -> dict[str, Any]:
    """Fetch a candidate homepage plus a few identity pages and assess the exact-entity proof."""
    robots_cache = {} if robots_cache is None else robots_cache
    outcome: dict[str, Any] = {"domain": domain, "pages": [], "errors": []}
    home = None
    probe_started = time.monotonic()
    for base in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}", f"http://www.{domain}"):
        host = urllib.parse.urlparse(base).hostname or ""
        if host != domain and not _resolves(host):
            continue
        parser = _robots_ok(base, budget, timeout, robots_cache)
        if parser is _UNREACHABLE:
            outcome["errors"].append(f"{base}: connection failed")
            failures = sum(1 for err in outcome["errors"] if err.endswith("connection failed"))
            if failures >= 2 and time.monotonic() - probe_started > 2 * timeout:
                break  # slow, unreachable host: do not spend the company's time budget on more variants
            continue
        if not _allowed(parser, base + "/"):
            outcome["status"] = "blocked_robots"
            outcome["errors"].append(f"robots.txt disallows {base}/")
            return outcome
        result = _get(base + "/", budget, timeout, max_bytes)
        if result.get("ok"):
            home = result
            break
        outcome["errors"].append(f"{base}/: {result.get('error') or result.get('status')}")
    if not home:
        outcome["status"] = "unreachable"
        return outcome
    final_url = home["final_url"]
    final_host = urllib.parse.urlparse(final_url).hostname or ""
    outcome["final_url"] = final_url
    outcome["final_domain"] = _registered_domain(final_url)
    if _blocked_host(final_host):
        outcome["status"] = "rejected_host"
        outcome["errors"].append(f"redirected to directory/marketplace/parking host {final_host}")
        return outcome
    home_page, links = _page_record(home, hints)
    outcome["pages"].append(home_page)
    signals = home_page["identity_signals"]
    if signals.get("parked_marker") and not signals.get("target_org_number_found"):
        outcome["status"] = "parked"
        outcome["proof"] = assess_org_proof(outcome["pages"])
        return outcome
    if not signals.get("target_org_number_found"):
        name_words = set(hints.get("legal_name_words") or []) - LEGAL_SUFFIXES - DROPPABLE_DESCRIPTORS
        home_words = set(_words(signals.get("title"))) | set(_words(final_host))
        promising = signals.get("legal_name_present") or bool(name_words & home_words) or not require_name_for_subpages
        sub_urls = _identity_links(final_url, links, max_subpages) if promising else []
        if promising and not sub_urls:
            root = f"{urllib.parse.urlparse(final_url).scheme}://{final_host}"
            sub_urls = [root + path for path in FALLBACK_PATHS[:2]]
        parser = robots_cache.get(f"{urllib.parse.urlparse(final_url).scheme}://{final_host}")
        for sub_url in sub_urls:
            if not _allowed(parser, sub_url):
                continue
            try:
                result = _get(sub_url, budget, timeout, max_bytes)
            except _BudgetExhausted:
                outcome["errors"].append("per-company request budget exhausted")
                break
            if not result.get("ok") or _registered_domain(result["final_url"]) != outcome["final_domain"]:
                continue
            if any(page["url"] == result["final_url"] for page in outcome["pages"]):
                continue
            page, _ = _page_record(result, hints)
            outcome["pages"].append(page)
            if page["identity_signals"].get("target_org_number_found"):
                break
    outcome["proof"] = assess_org_proof(outcome["pages"])
    outcome["status"] = {"exact": "verified", "conflict": "ambiguous"}.get(outcome["proof"]["status"], "unverified")
    return outcome


def _summarize(outcome: dict[str, Any], lead_source: str) -> dict[str, Any]:
    proof = outcome.get("proof") or {}
    return {
        "domain": outcome.get("domain"),
        "lead_source": lead_source,
        "outcome": outcome.get("status"),
        "final_url": outcome.get("final_url"),
        "pages_checked": [page["url"] for page in outcome.get("pages", [])],
        "evidence_for": proof.get("evidence_for", []),
        "evidence_against": proof.get("evidence_against", []) + outcome.get("errors", [])[:3],
    }


def discover_official_website(profile: dict[str, Any], *, exclude_domains: set[str] | None = None, max_requests: int = 14,
                              deadline_s: float = 40.0, timeout: float = 5.0, max_candidates: int = 9) -> dict[str, Any]:
    """Return a discovery record: status verified|ambiguous|not_found plus candidates and metrics."""
    hints = identity_hints(profile)
    budget = _Budget(max_requests, deadline_s)
    candidates = build_candidates(profile, exclude_domains=exclude_domains, limit=max_candidates)
    resolution = _resolve_many(
        list(dict.fromkeys(itertools.chain.from_iterable((c["domain"], "www." + c["domain"]) for c in candidates))), timeout=4.0,
    )
    budget.dns_lookups = len(resolution)
    assessed: list[dict[str, Any]] = []
    verified = None
    ambiguous = None
    robots_cache: dict = {}
    for candidate in candidates:
        domain = candidate["domain"]
        if not (resolution.get(domain) or resolution.get("www." + domain)):
            assessed.append({"domain": domain, "lead_source": candidate["lead_source"], "outcome": "dns_not_resolved"})
            continue
        try:
            outcome = probe_site(domain, hints, budget, timeout=timeout, robots_cache=robots_cache,
                                 require_name_for_subpages=candidate["lead_source"] == "legal_name_domain_guess")
        except _BudgetExhausted:
            assessed.append({"domain": domain, "lead_source": candidate["lead_source"], "outcome": "budget_exhausted"})
            break
        assessed.append(_summarize(outcome, candidate["lead_source"]))
        if outcome.get("status") == "verified":
            verified = {**outcome, "lead_source": candidate["lead_source"]}
            break
        if outcome.get("status") == "ambiguous" and ambiguous is None:
            ambiguous = {**outcome, "lead_source": candidate["lead_source"]}
    status = "verified" if verified else "ambiguous" if ambiguous else "not_found"
    return {
        "status": status,
        "method": METHOD,
        "selected": verified,
        "ambiguous": ambiguous,
        "candidates": assessed,
        "policy": "Candidates are leads only; publication requires the exact organisation number (or legal name plus registered address) fetched from the candidate site itself.",
        "metrics": {"requests": budget.requests, "bytes": budget.bytes, "latencies_ms": budget.latencies_ms, "dns_lookups": budget.dns_lookups},
    }


# ---------------------------------------------------------------------------
# Orchestration: registry website first, discovery only when it is not verified
# ---------------------------------------------------------------------------

def _merge_metrics(total: dict[str, Any], part: dict[str, Any]) -> None:
    total["requests"] += part.get("requests", 0)
    total["bytes"] += part.get("bytes", 0)
    total["latencies_ms"].extend(part.get("latencies_ms", []))
    total["dns_lookups"] = total.get("dns_lookups", 0) + part.get("dns_lookups", 0)


def _official_claim(record: dict[str, Any]) -> dict[str, Any] | None:
    value = record.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if record.get("status") != "available" or not assessment.get("publishable"):
        return None
    proof = assessment.get("org_number_proof") or {}
    final_url = value.get("final_url") or record.get("source_url")
    parsed = urllib.parse.urlparse(final_url)
    return {
        "field": "official_website",
        "value": urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", "")),
        "availability": "available",
        "confidence": assessment.get("score"),
        "identity_method": proof.get("proof") or assessment.get("method"),
        "lead_source": (record.get("discovery") or {}).get("selected_lead_source") or "brreg_hjemmeside",
        "evidence": {
            "source_url": proof.get("source_url") or final_url,
            "source_class": "company_owned",
            "retrieved_at": proof.get("retrieved_at") or record.get("retrieved_at"),
            "content_sha256": proof.get("content_sha256") or record.get("content_sha256"),
            "claim_span": proof.get("claim_span") or "; ".join(assessment.get("reasons") or []),
        },
    }


def resolve_official_website(profile: dict[str, Any], *, discovery: bool = True, max_requests: int = 14) -> tuple[dict[str, Any], dict[str, Any]]:
    from .identity import apply_website_identity_gate
    from .website import fetch_website

    hints = identity_hints(profile)
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": [], "dns_lookups": 0}
    registry_record, registry_metrics = fetch_website(profile.get("website"), hints=hints, timeout=10.0)
    _merge_metrics(metrics, registry_metrics)
    record = apply_website_identity_gate(profile, registry_record)["website"]
    if record.get("status") == "available" or not discovery:
        record["official_website"] = _official_claim(record)
        return record, metrics

    exclude: set[str] = set()
    registry_value = record.get("value") or {}
    for url in (record.get("source_url"), registry_value.get("final_url")):
        if url and record.get("status") == "ambiguous":
            # Already fetched and assessed by the registry step; do not probe it twice.
            exclude.add(_registered_domain(url))
    found = discover_official_website(profile, exclude_domains={d for d in exclude if d}, max_requests=max_requests)
    _merge_metrics(metrics, found["metrics"])
    summary = {
        "status": found["status"],
        "method": found["method"],
        "policy": found["policy"],
        "candidates": found["candidates"],
        "registry_website_status": record.get("status"),
        "registry_website_url": record.get("source_url") if profile.get("website") else None,
    }
    selected = found.get("selected") or found.get("ambiguous")
    if selected:
        summary["selected_lead_source"] = selected.get("lead_source")
        summary["selected_domain"] = selected.get("domain")
    if found["status"] == "verified":
        selected = found["selected"]
        site_record, site_metrics = fetch_website(selected["final_url"], hints=hints, timeout=8.0, source_type="discovered_company_website")
        _merge_metrics(metrics, site_metrics)
        if site_record.get("status") != "available":
            site_record = {
                **site_record,
                "status": "available",
                "source_url": selected["final_url"],
                "content_sha256": selected["pages"][0]["content_sha256"],
                "value": {
                    "requested_url": selected["pages"][0]["requested_url"],
                    "final_url": selected["final_url"],
                    "registered_domain": selected.get("final_domain"),
                    "title": selected["pages"][0]["identity_signals"].get("title"),
                    "pages": [],
                    "social_links": [],
                    "crawl_note": "full crawl failed after verification; proof pages retained",
                },
            }
        value = site_record.setdefault("value", {})
        known = {page.get("url") for page in value.get("pages", [])}
        value.setdefault("pages", []).extend(page for page in selected["pages"] if page.get("url") not in known)
        if not value["pages"] or not value["pages"][0].get("identity_signals"):
            # The homepage proof must lead the page list so the gate reads it as the homepage.
            home = selected["pages"][0]
            value["pages"] = [home] + [page for page in value["pages"] if page is not home and page.get("url") != home.get("url")]
        value["redirect_chain"] = selected["pages"][0].get("redirect_chain")
        gated = apply_website_identity_gate(profile, site_record)["website"]
        gated["discovery"] = summary
        gated["official_website"] = _official_claim(gated)
        if gated["official_website"]:
            return gated, metrics
        summary["status"] = "ambiguous"
        summary["note"] = "probe proof did not survive the full-site identity gate"
        record = {**gated, "status": "ambiguous"}
        record["discovery"] = summary
        record["official_website"] = None
        return record, metrics
    if found["status"] == "ambiguous" and record.get("status") != "ambiguous":
        amb = found["ambiguous"]
        from .evidence import evidence
        record = evidence(
            "website", "ambiguous", "discovered_company_website", amb.get("final_url") or f"https://{amb['domain']}/",
            value={"final_url": amb.get("final_url"), "pages": amb.get("pages"), "identity_assessment": amb.get("proof")},
            note="Candidate site carries the organisation number but also conflicting identity evidence; not published",
            content_sha256=(amb.get("pages") or [{}])[0].get("content_sha256"),
        )
        record["registry_website"] = {"status": registry_record.get("status"), "source_url": registry_record.get("source_url"), "note": registry_record.get("note")}
    record["discovery"] = summary
    record["official_website"] = None
    return record, metrics

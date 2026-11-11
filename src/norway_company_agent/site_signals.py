"""Company-owned signals from a *verified* official website.

Runs only when ``evidence.website.official_website`` was published by the org-number identity gate.
Every fact carries the page URL it came from, retrieval time, content hash and the text span.

Signals (each with its own field state):
  * social_profiles  - outbound profile links (share/intent/post links rejected), only published when
                       the handle matches the brand/domain/legal name or the site's own JSON-LD
                       identity-bound ``Organization.sameAs`` declares it; everything else is kept as unpublished candidates.
  * news_posts       - dated posts from the site's RSS/Atom feed, its news/press/blog index, or sitemap.
  * careers          - careers page(s), job postings (JSON-LD JobPosting, ATS links, child pages) and an
                       explicit "no vacancies" state.
  * contact          - e-mail addresses on the site's own domain (or the Brreg-registered e-mail) and
                       Norwegian phone numbers from tel: links or labelled text.
  * business_description / services - meta description, homepage summary and the services page headings.

Budget: robots.txt is honoured; at most ``max_requests`` HTTP requests per company (default 8) and a
25 s deadline. No third-party hosts are fetched.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup
import tldextract

from .evidence import evidence, utc_now
from .website import USER_AGENT, assert_public_url, normalize_social_url

METHOD = "verified_site_signals_v1"
SOURCE_TYPE = "company_owned_site_signals"

NEWS_TERMS = ("nyheter", "aktuelt", "news", "presse", "press", "blogg", "blog", "artikler", "articles", "nyhet", "insights", "innsikt", "media")
CAREER_TERMS = ("ledige-stillinger", "ledige stillinger", "karriere", "career", "jobbe-hos-oss", "jobbe hos oss", "jobb hos oss", "stillinger", "jobs", "jobb", "vacancies", "rekruttering", "join-us", "join us", "bli med")
CONTACT_TERMS = ("kontakt", "contact", "om-oss", "om oss", "about")
SERVICE_TERMS = ("tjenester", "tjeneste", "produkter", "produkt", "services", "service", "products", "vi-tilbyr", "vi tilbyr", "hva-vi-gjor", "hva vi gjør", "sortiment", "losninger", "løsninger", "solutions", "fagomrader", "fagområder", "virksomhetsomrader")
MAX_NEWS_POSTS = 25
MAX_JOB_POSTINGS = 40
MAX_ATS_AD_CHECKS = 6
ATS_HOSTS = ("jobbnorge.no", "webcruiter.no", "webcruiter.com", "teamtailor.com", "recman.no", "recman.page", "hrmanager.no", "easycruit.com", "reachmee.com", "jobylon.com", "talentech.com", "varbi.com", "emply.com", "workable.com", "lever.co", "greenhouse.io", "smartrecruiters.com", "finn.no/job", "arbeidsplassen.nav.no", "simployer.com", "zalaris", "talmundo")
JOB_PATH = re.compile(r"/(?:job|jobs|jobb|stilling|stillinger|ad|ads|vacancy|vacancies|position|positions|career|careers|karriere|o)/[^/?#]+", re.I)
NO_VACANCY = re.compile(r"(ingen\s+ledige\s+stillinger|ingen\s+utlyste\s+stillinger|har\s+ingen\s+ledige|no\s+(?:current\s+)?(?:open\s+positions|vacancies|openings))", re.I)
VENDOR_HANDLES = {
    "wordpress", "elementor", "wix", "squarespace", "shopify", "mailchimp", "visma", "tripletex", "fiken", "hubspot",
    "google", "youtube", "facebook", "instagram", "linkedin", "twitter", "x", "tiktok", "meta", "apple", "microsoft",
    "sharer", "share", "home", "pages", "groups", "watch", "channel", "user", "playlist", "embed", "plugins", "tr",
    "bring", "posten", "vipps", "klarna", "nets", "dnb", "sparebank1", "proff", "gulesider", "1881", "mittanbud",
    "webflow", "jimdo", "godaddy", "domeneshop", "onecom", "one", "uniweb", "framer", "weebly", "strikingly",
}
GENERIC_NAME_WORDS = {
    "as", "asa", "ans", "da", "enk", "sa", "nuf", "holding", "invest", "norge", "norway", "nordic", "group", "gruppen",
    "eiendom", "service", "services", "consulting", "og", "i", "the", "and", "company", "co", "bygg", "transport",
    "regnskap", "rorlegger", "elektro", "maler", "drift", "utvikling", "handel", "sport", "auto", "bil", "data",
}
PLACEHOLDER_POST_TITLES = {"hello world!", "hello world", "hei verden!", "hei verden", "sample page", "eksempelside"}
NO_MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "mars": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "okt": 10, "october": 10, "oct": 10, "november": 11, "nov": 11, "desember": 12,
    "des": 12, "december": 12, "dec": 12, "january": 1, "february": 2, "march": 3,
}
MONTH_ALT = "|".join(sorted(NO_MONTHS, key=len, reverse=True))
DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(20\d{2}|19\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)"),
    re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({MONTH_ALT})\.?,?\s+(20\d{{2}}|19\d{{2}})(?!\d)", re.I),
    re.compile(rf"\b({MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})(?!\d)", re.I),
)
URL_DATE = re.compile(r"/(20\d{2})/(\d{2})(?:/(\d{2}))?/")
EMAIL = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+)", re.I)
PHONE_LABELLED = re.compile(r"(?i)(?:tlf|telefon|tel|phone|mobil|mob|ring\s+oss|sentralbord)\.?\s*(?:nr\.?)?\s*[:.]?\s*((?:\+47|0047)?[\s\u00a0]*(?:\d[\s\u00a0.-]?){8})(?!\d)")


class _Exhausted(Exception):
    pass


class _Budget:
    def __init__(self, max_requests: int, deadline_s: float) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.bytes = 0
        self.latencies_ms: list[int] = []
        self.deadline = time.monotonic() + deadline_s

    def take(self) -> None:
        if self.requests >= self.max_requests or time.monotonic() > self.deadline:
            raise _Exhausted()
        self.requests += 1


def _rd(url: str) -> str:
    return tldextract.extract(urllib.parse.urlparse(url).hostname or "").top_domain_under_public_suffix


def _fold(value: Any) -> str:
    import unicodedata
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _fetch(url: str, budget: _Budget, *, timeout: float = 6.0, max_bytes: int = 2_500_000) -> dict[str, Any]:
    from .website_discovery import _OPENER
    budget.take()
    started = time.monotonic()
    try:
        assert_public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5"})
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes)
            final_url = response.geturl()
            assert_public_url(final_url)
            elapsed = int((time.monotonic() - started) * 1000)
            budget.latencies_ms.append(elapsed)
            budget.bytes += len(raw)
            return {"ok": True, "status": response.status, "url": url, "final_url": final_url, "raw": raw,
                    "content_type": response.headers.get("content-type", ""), "retrieved_at": utc_now(),
                    "content_sha256": hashlib.sha256(raw).hexdigest()}
    except urllib.error.HTTPError as exc:
        budget.latencies_ms.append(int((time.monotonic() - started) * 1000))
        return {"ok": False, "status": exc.code, "url": url, "error": f"HTTP {exc.code}"}
    except _Exhausted:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded as page outcome
        budget.latencies_ms.append(int((time.monotonic() - started) * 1000))
        return {"ok": False, "status": 0, "url": url, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def parse_date(text: str, *, today: date | None = None) -> str | None:
    today = today or datetime.now(timezone.utc).date()
    for index, pattern in enumerate(DATE_PATTERNS):
        match = pattern.search(text or "")
        if not match:
            continue
        g = match.groups()
        try:
            if index == 0:
                y, m, d = int(g[0]), int(g[1]), int(g[2])
            elif index in (1, 2):
                d, m, y = int(g[0]), int(g[1]), int(g[2])
            elif index == 3:
                d, m, y = int(g[0]), NO_MONTHS[g[1].casefold()], int(g[2])
            else:
                m, d, y = NO_MONTHS[g[0].casefold()], int(g[1]), int(g[2])
            value = date(y, m, d)
        except (ValueError, KeyError):
            continue
        if date(1995, 1, 1) <= value <= today + timedelta(days=1):
            return value.isoformat()
    return None


def _iso_from_any(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    return parse_date(value)


def brand_tokens(profile: dict[str, Any], site_url: str, title: str = "") -> dict[str, Any]:
    domain_label = _fold(tldextract.extract(urllib.parse.urlparse(site_url).hostname or "").domain).replace("-", "")
    words = [w for w in re.findall(r"[a-z0-9]+", _fold(profile.get("name"))) if w not in {"as", "asa", "ans", "da", "enk", "sa", "nuf"}]
    distinctive = [w for w in words if len(w) >= 4 and w not in GENERIC_NAME_WORDS]
    return {"domain": domain_label, "joined": "".join(words), "distinctive": distinctive,
            "legal_name": _identity_name(profile.get("name")),
            "organisation_number": str(profile.get("organisation_number") or ""), "site_url": site_url}


def handle_matches(handle: str, tokens: dict[str, Any]) -> bool:
    h = re.sub(r"[^a-z0-9]", "", _fold(handle))
    if len(h) < 3 or h in VENDOR_HANDLES:
        return False
    # A brand inside another handle is not ownership evidence (e.g. "notacme"
    # or "acme-partner"). Accept whole names and narrowly bounded conventions.
    aliases = {tokens.get("domain") or "", tokens.get("joined") or "",
               re.sub(r"[^a-z0-9]", "", tokens.get("legal_name") or "")}
    aliases = {alias for alias in aliases if len(alias) >= 3 and alias not in VENDOR_HANDLES}
    if h in aliases or any(h == alias + "official" for alias in aliases):
        return True
    # Facebook's /p/ profile URLs append a long numeric ID to the full name.
    slug = re.sub(r"[-_]\d{5,}$", "", _fold(handle))
    return slug != _fold(handle) and re.sub(r"[^a-z0-9]", "", slug) in aliases


def _social_handle(url: str) -> str:
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    if parts[0] in {"company", "channel", "user", "c", "p", "people", "pages"} and len(parts) > 1:
        return parts[1]
    return parts[0].lstrip("@")


def jsonld_nodes(soup: BeautifulSoup) -> list[Any]:
    nodes: list[Any] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes.append(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return nodes


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


def _types(node: dict[str, Any]) -> set[str]:
    kind = node.get("@type")
    return set(kind if isinstance(kind, list) else [kind]) - {None}


def _identity_name(value: Any) -> str:
    words = re.findall(r"[a-z0-9]+", _fold(value))
    return " ".join(words)


def _same_homepage(raw: Any, site_url: str, page_url: str) -> bool:
    if not isinstance(raw, str) or not raw.strip() or not site_url:
        return False
    try:
        candidate = urllib.parse.urlparse(urllib.parse.urljoin(page_url or site_url, raw))
        home = urllib.parse.urlparse(site_url)
        same_port = candidate.port == home.port
    except ValueError:
        return False
    return (candidate.scheme in {"http", "https"}
            and (candidate.hostname or "").casefold().removeprefix("www.")
            == (home.hostname or "").casefold().removeprefix("www.")
            and not candidate.username and not candidate.password
            and same_port
            and candidate.path.rstrip("/") == home.path.rstrip("/")
            and not candidate.query)


def _org_matches_identity(item: dict[str, Any], tokens: dict[str, Any], page_url: str) -> bool:
    # Only identifiers attached to this organization count, never a child/parent's.
    identifiers = []
    for key in ("taxID", "vatID", "identifier"):
        raw = item.get(key)
        for value in raw if isinstance(raw, list) else [raw]:
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, (str, int)):
                number = re.sub(r"\D", "", str(value))
                if len(number) == 9:
                    identifiers.append(number)
    target_number = tokens.get("organisation_number")
    if identifiers and target_number:
        return all(number == target_number for number in identifiers)
    names = [item.get(key) for key in ("legalName", "name") if isinstance(item.get(key), str) and item[key].strip()]
    target_name = tokens.get("legal_name")
    # A contradictory legal name cannot be rescued by a loose brand match.
    if item.get("legalName") and target_name and _identity_name(item["legalName"]) != target_name:
        return False
    if target_name and any(_identity_name(name) == target_name for name in names):
        return True
    # Common CMS graphs identify a brand (rather than its legal name) by its
    # exact homepage URL. A local @id alone is not evidence of ownership.
    domain = tokens.get("domain")
    brand_name = any(re.sub(r"[^a-z0-9]", "", _fold(name)) == domain for name in names)
    return ((not names or brand_name)
            and _same_homepage(item.get("url"), tokens.get("site_url") or "", page_url))


def org_same_as(nodes: list[Any], tokens: dict[str, Any] | None = None, *, page_url: str = "") -> set[str]:
    """Read organization links, optionally requiring a verified target identity.

    Omitting tokens enumerates candidates only; callers must not publish them.
    """
    found: set[str] = set()
    for node in nodes:
        for item in _walk(node):
            if _types(item) & {"Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "Restaurant", "HomeAndConstructionBusiness"}:
                if tokens is not None and not _org_matches_identity(item, tokens, page_url):
                    continue
                same = item.get("sameAs")
                for raw in (same if isinstance(same, list) else [same]):
                    if isinstance(raw, str):
                        social = normalize_social_url(raw.strip())
                        if social:
                            found.add(social["url"])
    return found


def extract_social(pages: list[dict[str, Any]], tokens: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    published: dict[str, dict[str, Any]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    structured_urls = set().union(*(org_same_as(page["jsonld"]) for page in pages))
    for page in pages:
        all_declared = org_same_as(page["jsonld"])
        declared = org_same_as(page["jsonld"], tokens, page_url=page["url"])
        hrefs = [(href, text) for href, text in page["links"]]
        hrefs.extend((url, "json-ld sameAs") for url in sorted(all_declared))
        for href, text in hrefs:
            url = href
            parsed = urllib.parse.urlparse(url)
            if (parsed.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed.path.startswith("/plugins/"):
                embedded = urllib.parse.parse_qs(parsed.query).get("href", [])
                url = embedded[0] if embedded else url
            social = normalize_social_url(url)
            if not social or social["url"] in published:
                continue
            handle = _social_handle(social["url"])
            if _fold(handle) in VENDOR_HANDLES:
                continue
            item = {
                "platform": social["platform"], "url": social["url"], "handle": handle,
                "evidence": {"source_url": page["url"], "retrieved_at": page["retrieved_at"], "content_sha256": page["content_sha256"],
                             "claim_span": f'link "{(text or "")[:60]}" -> {href[:200]}'},
            }
            if social["url"] in declared:
                item.update(match="site_jsonld_organization_sameAs", confidence=0.97)
                published[social["url"]] = item
            elif social["url"] not in structured_urls and handle_matches(handle, tokens):
                item.update(match="handle_matches_brand_or_domain", confidence=0.95)
                published[social["url"]] = item
            else:
                item.update(match="unverified_organization_identity" if social["url"] in structured_urls else "unmatched_handle_not_published")
                candidates.setdefault(social["url"], item)
    for url in published:
        candidates.pop(url, None)
    return sorted(published.values(), key=lambda i: (i["platform"], i["url"])), sorted(candidates.values(), key=lambda i: (i["platform"], i["url"]))[:10]


def extract_contacts(page: dict[str, Any], site_domain: str, registry_email: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    emails: dict[str, dict[str, Any]] = {}
    phones: dict[str, dict[str, Any]] = {}
    ev = {"source_url": page["url"], "retrieved_at": page["retrieved_at"], "content_sha256": page["content_sha256"]}
    reg = (registry_email or "").strip().casefold()
    raw_emails = [(urllib.parse.unquote(h[7:].split("?")[0]).strip(), "mailto") for h, _ in page["links"] if h.lower().startswith("mailto:")]
    raw_emails += [(m.group(1), "text") for m in EMAIL.finditer(page["text"])]
    for address, how in raw_emails:
        address = address.strip().strip(".").casefold()
        if not EMAIL.fullmatch(address):
            continue
        domain = address.rsplit("@", 1)[1]
        rd = tldextract.extract(domain).top_domain_under_public_suffix
        if rd != site_domain and address != reg:
            continue
        if re.search(r"\.(png|jpe?g|gif|webp|svg)$", address) or "sentry" in address or "example" in address:
            continue
        emails.setdefault(address, {"value": address, "method": how, "registry_match": address == reg, "evidence": {**ev, "claim_span": address}})
    raw_phones = [(urllib.parse.unquote(h[4:]), "tel") for h, _ in page["links"] if h.lower().startswith("tel:")]
    raw_phones += [(m.group(1), "labelled_text") for m in PHONE_LABELLED.finditer(page["text"])]
    for raw, how in raw_phones:
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("0047"):
            digits = digits[4:]
        elif digits.startswith("47") and len(digits) == 10:
            digits = digits[2:]
        if len(digits) != 8 or digits[0] not in "2345679":
            continue
        normalized = "+47 " + " ".join([digits[0:2], digits[2:4], digits[4:6], digits[6:8]])
        phones.setdefault(normalized, {"value": normalized, "method": how, "evidence": {**ev, "claim_span": raw.strip()[:40]}})
    role = [e for e in emails.values() if e["registry_match"] or re.match(r"^(post|postmottak|firmapost|kontakt|contact|info|hei|hello|hallo|mail|office|kontor|salg|sales|service|kundeservice|support|ordre|order|booking|bestilling|faktura|regnskap|admin|resepsjon|servering|ops|jobb|hr)\b", e["value"])]
    personal = [e for e in emails.values() if e not in role]
    for item in role:
        item["kind"] = "role_mailbox"
    chosen = role[:3] if role else []
    if not chosen and personal:
        personal[0]["kind"] = "named_mailbox"
        chosen = personal[:1]
    return chosen, list(phones.values())[:4]


def parse_feed(raw: bytes, site_domain: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    posts = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item")
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = _iso_from_any(item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date"))
        posts.append((title, link, published, None, "feed_pubDate"))
    for entry in root.findall(f".//{ns_atom}entry"):
        title = (entry.findtext(f"{ns_atom}title") or "").strip()
        link_node = entry.find(f"{ns_atom}link")
        link = (link_node.get("href") if link_node is not None else "") or ""
        published = _iso_from_any(entry.findtext(f"{ns_atom}published"))
        modified = _iso_from_any(entry.findtext(f"{ns_atom}updated"))
        posts.append((title, link, published, modified, "feed_published"))
    output = []
    if any(re.sub(r"[^a-z ]", "", _fold(title)).strip() in {"hello world", "hei verden"} for title, _, _, _, _ in posts):
        # A default CMS welcome post means the feed still carries theme/demo content: trust none of it.
        return []
    for title, link, published, modified, date_source in posts:
        if not title or not link or title.casefold() in PLACEHOLDER_POST_TITLES:
            continue
        if _rd(link) != site_domain:
            continue
        output.append({"title": title[:300], "url": link, "published_at": published, "date_source": date_source if published else None,
                       "modified_at": modified, "modified_date_source": "feed_updated" if modified else None})
    return output


def _date_near(anchor: Any, href: str) -> tuple[str | None, str | None]:
    node = anchor
    for _ in range(4):
        if node is None:
            break
        time_tag = (node.select_one('time[itemprop~="datePublished"]') or node.find("time")) if hasattr(node, "select_one") else None
        if time_tag is not None:
            if "datemodified" in str(time_tag.get("itemprop") or "").casefold():
                return None, None
            parsed = _iso_from_any(str(time_tag.get("datetime") or "")) or parse_date(time_tag.get_text(" ", strip=True))
            if parsed:
                return parsed, "time_element"
        text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        if len(text) > 600:
            break
        parsed = parse_date(text)
        if parsed:
            return parsed, "text_near_link"
        node = node.parent
    match = URL_DATE.search(href)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1)).isoformat(), "url_path_date"
        except ValueError:
            pass
    return None, None


def extract_index_posts(page: dict[str, Any], index_url: str, site_domain: str) -> list[dict[str, Any]]:
    soup = page["soup"]
    index_path = urllib.parse.urlparse(index_url).path.rstrip("/")
    posts: dict[str, dict[str, Any]] = {}
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(page["url"], str(anchor.get("href") or ""))
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in {"http", "https"} or _rd(href) != site_domain:
            continue
        path = parsed.path.rstrip("/")
        if not path or path == index_path:
            continue
        under_index = bool(index_path) and path.startswith(index_path + "/")
        newsish = any(term in path.casefold() for term in ("/nyhet", "/aktuelt", "/news", "/blog", "/presse", "/press", "/artikl", "/article", "/innsikt", "/insights"))
        if not (under_index or newsish or URL_DATE.search(path + "/")):
            continue
        if re.search(r"/(page|side|category|kategori|tag|tags|author|forfatter|wp-content|uploads|content/uploads|media|files)/", path + "/", re.I):
            continue
        if re.search(r"\.(png|jpe?g|gif|webp|svg|pdf|docx?|xlsx?|zip|mp4|mp3)$", path, re.I):
            continue
        inner = anchor.find(["h1", "h2", "h3", "h4", "h5"])
        title = inner.get_text(" ", strip=True) if inner else anchor.get_text(" ", strip=True)
        if len(title) < 12 or title.casefold() in {"les mer", "read more", "les hele saken", "se alle", "flere nyheter"}:
            heading = None
            node = anchor.parent
            for _ in range(3):
                if node is None:
                    break
                heading = node.find(["h1", "h2", "h3", "h4"])
                if heading:
                    break
                node = node.parent
            title = heading.get_text(" ", strip=True) if heading else ""
        if len(title) < 12 or len(title) > 250:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        published, source = _date_near(anchor, href)
        modified = None
        node = anchor
        for _ in range(4):
            if node is None:
                break
            modified_tag = node.select_one('time[itemprop~="dateModified"]')
            if modified_tag is not None:
                modified = _iso_from_any(str(modified_tag.get("datetime") or "")) or parse_date(modified_tag.get_text(" ", strip=True))
                break
            node = node.parent
        if not published and not (under_index and len([p for p in path[len(index_path):].split("/") if p]) >= 1 and len(title) >= 20):
            continue
        if any(p["title"] == title[:300] for u, p in posts.items() if u != clean):
            continue
        prior = posts.get(clean)
        if prior and (prior.get("published_at") or not published):
            continue
        posts[clean] = {"title": title[:300], "url": clean, "published_at": published, "date_source": source,
                        "modified_at": modified, "modified_date_source": "time_dateModified" if modified else None}
    ordered = sorted(posts.values(), key=lambda p: (p["published_at"] is None, "" if p["published_at"] is None else "~" + p["published_at"]), reverse=False)
    ordered.sort(key=lambda p: p["published_at"] or "", reverse=True)
    return ordered


def extract_job_postings(page: dict[str, Any], careers_url: str, site_domain: str) -> tuple[list[dict[str, Any]], bool]:
    postings: dict[str, dict[str, Any]] = {}
    for node in page["jsonld"]:
        for item in _walk(node):
            if "JobPosting" in _types(item):
                title = str(item.get("title") or "").strip()
                if title:
                    url = str(item.get("url") or page["url"])
                    postings.setdefault(url + "#" + title, {"title": title[:200], "url": url, "date_posted": _iso_from_any(str(item.get("datePosted") or "")), "valid_through": _iso_from_any(str(item.get("validThrough") or "")), "method": "jsonld_jobposting"})
    careers_path = urllib.parse.urlparse(careers_url).path.rstrip("/")
    nav = {"ledige stillinger", "karriere", "jobb hos oss", "jobbe hos oss", "stillinger", "careers", "jobs", "se alle stillinger", "les mer", "søk her", "send åpen søknad", "åpen søknad", "open application"}
    for anchor in page["soup"].select("a[href]"):
        href = urllib.parse.urljoin(page["url"], str(anchor.get("href") or ""))
        parsed = urllib.parse.urlparse(href)
        text = anchor.get_text(" ", strip=True)
        if not (6 <= len(text) <= 140) or text.casefold() in nav:
            continue
        host = (parsed.hostname or "").casefold()
        on_ats = any(host == h or host.endswith("." + h) or (h in (host + parsed.path)) for h in ATS_HOSTS)
        child = _rd(href) == site_domain and bool(careers_path) and parsed.path.rstrip("/").startswith(careers_path + "/")
        if on_ats and JOB_PATH.search(parsed.path):
            method = "ats_job_link"
        elif child and len([p for p in parsed.path.split("/") if p]) >= 2:
            method = "careers_child_page"
        else:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query if on_ats else "", ""))
        postings.setdefault(clean, {"title": text[:200], "url": clean, "date_posted": None, "valid_through": None, "method": method})
    return list(postings.values())[:MAX_JOB_POSTINGS], bool(NO_VACANCY.search(page["text"]))


def _parse_page(result: dict[str, Any]) -> dict[str, Any]:
    raw = result["raw"]
    soup = BeautifulSoup(raw, "lxml")
    jsonld = jsonld_nodes(soup)
    links = [(urllib.parse.urljoin(result["final_url"], str(a.get("href") or "").strip()), a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
    links += [(str(n.get("data-href") or ""), "") for n in soup.select("[data-href]")]
    links += [(urllib.parse.urljoin(result["final_url"], str(n.get("src") or "")), "iframe") for n in soup.select("iframe[src]")]
    feeds = [urllib.parse.urljoin(result["final_url"], str(n.get("href") or "")) for n in soup.select('link[rel~="alternate"][href]') if re.search(r"(rss|atom)\+xml", str(n.get("type") or ""), re.I)]
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    desc_tag = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    description = str(desc_tag.get("content") or "").strip() if desc_tag else ""
    headings = [h.get_text(" ", strip=True) for h in soup.select("h1, h2, h3")]
    text_soup = BeautifulSoup(raw, "lxml")
    for node in text_soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    text = text_soup.get_text(" ", strip=True)
    return {"url": result["final_url"], "retrieved_at": result["retrieved_at"], "content_sha256": result["content_sha256"],
            "soup": soup, "jsonld": jsonld, "links": links, "feeds": feeds, "title": title, "description": description,
            "headings": headings, "text": text}


WEAK_TERMS = {"jobb", "jobs", "stillinger", "career", "karriere", "press", "media", "service", "produkt", "about", "blog", "news"}


def _pick_link(links: list[tuple[str, str]], terms: tuple[str, ...], site_domain: str, exclude: set[str]) -> str | None:
    best: tuple[int, int, str] | None = None
    for href, text in links:
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in {"http", "https"} or _rd(href) != site_domain:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean in exclude or clean.rstrip("/") == "" or parsed.path in {"", "/"}:
            continue
        if re.search(r"/(19|20)\d{2}/\d{2}/", parsed.path):
            continue  # dated article URLs are posts, not section pages
        haystack = _fold(parsed.path.replace("_", "-") + " " + text)
        segments = {_fold(seg) for seg in parsed.path.split("/") if seg} | {_fold(text).strip()}
        for rank, term in enumerate(terms):
            folded_term = _fold(term)
            if folded_term in WEAK_TERMS:
                hit = folded_term in segments
            else:
                hit = re.search(r"(?<![a-z])" + re.escape(folded_term) + r"(?![a-z])", haystack) is not None
            if hit:
                depth = len([p for p in parsed.path.split("/") if p])
                candidate = (rank, depth, clean)
                if best is None or candidate < best:
                    best = candidate
                break
    return best[2] if best else None


OPEN_APPLICATION = re.compile(r"(open[\s-]+application|apen[\s-]+soknad|åpen[\s-]+søknad|spontaneous|generell[\s-]+soknad|general[\s-]+application)", re.I)


def ats_listing_url(ats_url: str) -> str:
    """Public job-list page for an ATS career site the verified site links to."""
    parsed = urllib.parse.urlparse(ats_url)
    host = (parsed.hostname or "").casefold()
    if host.endswith(".teamtailor.com") and parsed.path.rstrip("/") in ("", "/jobs"):
        return f"{parsed.scheme}://{parsed.netloc}/jobs"
    return ats_url


def postings_from_ats_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Job ads on an ATS listing page, minus standing open-application entries."""
    host_domain = _rd(page["url"])
    postings, _ = extract_job_postings(page, "", host_domain)
    kept = []
    for posting in postings:
        if OPEN_APPLICATION.search(posting["title"]) or OPEN_APPLICATION.search(posting["url"].replace("-", " ")):
            continue
        if posting["method"] == "ats_job_link":
            posting["method"] = "ats_listing_linked_from_verified_site"
        kept.append(posting)
    return kept


def _ats_listing_postings(ats_url: str, budget: "_Budget", fetched: list, errors: list) -> list[dict[str, Any]]:
    """Fetch the ATS job list the company's own verified site links to (robots.txt respected)."""
    url = ats_listing_url(ats_url)
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    robots = urllib.robotparser.RobotFileParser()
    try:
        robots_result = _fetch(base + "/robots.txt", budget, max_bytes=300_000)
        if robots_result.get("ok") and robots_result.get("status") == 200:
            robots.parse(robots_result["raw"].decode("utf-8", errors="replace").splitlines())
        elif robots_result.get("status") in (401, 403):
            errors.append({"url": url, "kind": "ats_listing", "error": "robots.txt not readable"})
            return []
        else:
            robots.parse([])
        if not robots.can_fetch(USER_AGENT, url):
            errors.append({"url": url, "kind": "ats_listing", "error": "robots.txt disallows"})
            return []
        result = _fetch(url, budget)
    except _Exhausted:
        raise
    fetched.append({"url": url, "kind": "ats_listing", "status": result.get("status"), "content_sha256": result.get("content_sha256"), "retrieved_at": result.get("retrieved_at")})
    if not result.get("ok"):
        errors.append({"url": url, "kind": "ats_listing", "error": result.get("error")})
        return []
    if _rd(result["final_url"]) != _rd(url):
        errors.append({"url": url, "kind": "ats_listing", "error": "ATS page redirected to another domain"})
        return []
    page = _parse_page(result)
    kept: list[dict[str, Any]] = []
    # A group's shared ATS can list sister companies' and foreign offices' jobs. Each ad is
    # kept only when its own JobPosting markup places it in Norway and it has not expired.
    for posting in postings_from_ats_page(page)[:MAX_ATS_AD_CHECKS]:
        if not robots.can_fetch(USER_AGENT, posting["url"]) or budget.requests >= budget.max_requests:
            break
        ad = _fetch(posting["url"], budget)
        fetched.append({"url": posting["url"], "kind": "ats_ad", "status": ad.get("status"), "content_sha256": ad.get("content_sha256"), "retrieved_at": ad.get("retrieved_at")})
        if not ad.get("ok") or _rd(ad["final_url"]) != _rd(url):
            continue
        ad_page = _parse_page(ad)
        verdict = ats_ad_in_norway(ad_page)
        if not verdict:
            continue
        posting.update(verdict)
        posting["url"] = ad_page["url"]
        posting["evidence"] = _ev(ad_page, f'{posting["title"]} | addressCountry=NO')
        kept.append(posting)
    return kept


def ats_ad_in_norway(page: dict[str, Any], today: str | None = None) -> dict[str, Any] | None:
    """Dates from an ATS ad's JobPosting markup when it lists a Norwegian job location and is not expired."""
    today = today or utc_now()[:10]
    for node in page["jsonld"]:
        for item in _walk(node):
            if "JobPosting" not in _types(item):
                continue
            locations = item.get("jobLocation") or []
            locations = locations if isinstance(locations, list) else [locations]
            countries = set()
            for loc in locations:
                address = (loc or {}).get("address") if isinstance(loc, dict) else None
                country = address.get("addressCountry") if isinstance(address, dict) else None
                if isinstance(country, dict):
                    country = country.get("name")
                if country:
                    countries.add(str(country).strip().casefold())
            if not countries & {"no", "nor", "norway", "norge"}:
                continue
            valid = _iso_from_any(str(item.get("validThrough") or ""))
            if valid and valid[:10] < today:
                continue
            return {"date_posted": _iso_from_any(str(item.get("datePosted") or "")), "valid_through": valid,
                    "hiring_organization": ((item.get("hiringOrganization") or {}).get("name") if isinstance(item.get("hiringOrganization"), dict) else None)}
    return None


def _ats_link(links: list[tuple[str, str]]) -> str | None:
    for href, text in links:
        parsed = urllib.parse.urlparse(href)
        host = (parsed.hostname or "").casefold()
        if any(host == h or host.endswith("." + h) for h in ATS_HOSTS if "/" not in h):
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
    return None


def site_ownership(profile: dict[str, Any], claim: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
    """Is the verified site the entity's *own* site, rather than a host/venue/group site that merely names it?

    The website gate proves the entity is on the site; site-wide signals (news, socials, careers) are only
    attributed when the site is also evidently run by it.
    """
    proof_url = ((claim.get("evidence") or {}).get("source_url") or "").rstrip("/")
    home_url = (home.get("url") or "").rstrip("/")
    if claim.get("identity_method") == "organisation_number" and proof_url in {home_url, (claim.get("value") or "").rstrip("/")}:
        return {"owned": True, "reason": "organisation number on the homepage"}
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    if org and org in re.sub(r"[\s.\u00a0]", "", home.get("text") or ""):
        return {"owned": True, "reason": "organisation number on the homepage"}
    words = [w for w in re.findall(r"[a-z0-9]+", _fold(profile.get("name"))) if w not in {"as", "asa", "ans", "da", "enk", "sa", "nuf"}]
    distinctive = [w for w in words if len(w) >= 3 and w not in GENERIC_NAME_WORDS] or [w for w in words if len(w) >= 3]
    host = _fold(tldextract.extract(urllib.parse.urlparse(home_url).hostname or "").domain).replace("-", "")
    title = re.sub(r"[^a-z0-9]", "", _fold(home.get("title")) + " " + _fold(" ".join(home.get("headings", [])[:3])))
    joined = "".join(words)
    if joined and joined in host:
        return {"owned": True, "reason": "domain carries the full legal name"}
    acronym = "".join(w[0] for w in words)
    if len(acronym) >= 3 and host == acronym:
        return {"owned": True, "reason": "domain is the acronym of the legal name"}
    if distinctive and all(w in host or w in title for w in distinctive):
        return {"owned": True, "reason": "every distinctive legal-name word is in the domain or homepage title"}
    return {"owned": False, "reason": "site names the entity but looks run by another party (host, venue, group or partner site)"}


def _ev(page: dict[str, Any], span: str) -> dict[str, Any]:
    return {"source_url": page["url"], "retrieved_at": page["retrieved_at"], "content_sha256": page["content_sha256"], "claim_span": span[:300]}


def collect_site_signals(profile: dict[str, Any], website_record: dict[str, Any] | None, *, max_requests: int = 8, deadline_s: float = 25.0) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    claim = (website_record or {}).get("official_website")
    if not claim:
        return evidence("site_signals", "not_applicable", SOURCE_TYPE, "https://data.brreg.no/enhetsregisteret/api/enheter",
                        note="No identity-verified official website; company-site signals are not collected from unverified sites"), metrics
    value = (website_record.get("value") or {})
    start_url = value.get("final_url") or claim["value"]
    site_domain = _rd(start_url)
    budget = _Budget(max_requests, deadline_s)
    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    out: dict[str, Any] = {"site": claim["value"], "site_domain": site_domain, "method": METHOD}
    states: dict[str, str] = {}
    news_posts: list[dict[str, Any]] = []
    news_pages: list[str] = []
    careers: dict[str, Any] = {"pages": [], "postings": [], "no_vacancies_statement": None}
    services_page = None
    try:
        parsed = urllib.parse.urlparse(start_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        robots = urllib.robotparser.RobotFileParser()
        robots_result = _fetch(base + "/robots.txt", budget, max_bytes=300_000)
        sitemaps: list[str] = []
        if robots_result.get("ok") and robots_result.get("status") == 200:
            lines = robots_result["raw"].decode("utf-8", errors="replace").splitlines()
            robots.parse(lines)
            sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("sitemap:")]
        else:
            robots.parse([])

        def get_page(url: str, kind: str) -> dict[str, Any] | None:
            if not robots.can_fetch(USER_AGENT, url):
                errors.append({"url": url, "kind": kind, "error": "robots.txt disallows"})
                return None
            result = _fetch(url, budget)
            fetched.append({"url": url, "kind": kind, "status": result.get("status"), "content_sha256": result.get("content_sha256"), "retrieved_at": result.get("retrieved_at")})
            if not result.get("ok"):
                errors.append({"url": url, "kind": kind, "error": result.get("error")})
                return None
            if _rd(result["final_url"]) != site_domain:
                errors.append({"url": url, "kind": kind, "error": "redirected off the verified domain"})
                return None
            result["kind"] = kind
            return result

        home_raw = get_page(start_url, "homepage")
        if not home_raw:
            status = "blocked" if any("robots" in str(e.get("error")) for e in errors) else "source_error"
            return evidence("site_signals", status, SOURCE_TYPE, start_url, value={**out, "pages_fetched": fetched, "errors": errors},
                            note="robots.txt disallows the homepage" if status == "blocked" else "verified homepage could not be re-fetched"), _m(budget)
        home = _parse_page(home_raw)
        home["kind"] = "homepage"
        ownership = site_ownership(profile, claim, home)
        out["site_ownership"] = ownership
        if not ownership["owned"]:
            return evidence("site_signals", "ambiguous", SOURCE_TYPE, home["url"], value={**out, "pages_fetched": fetched, "errors": errors},
                            note="Verified site is not evidently run by this entity; site-wide signals are not attributed", content_sha256=home["content_sha256"]), _m(budget)
        pages.append(home)
        seen = {home["url"].rstrip("/"), start_url.rstrip("/")}
        links = home["links"]

        news_index = _pick_link(links, NEWS_TERMS, site_domain, seen)
        careers_url = _pick_link(links, CAREER_TERMS, site_domain, seen)
        contact_url = _pick_link(links, CONTACT_TERMS, site_domain, seen)
        services_url = _pick_link(links, SERVICE_TERMS, site_domain, seen)
        ats_url = _ats_link(links)
        feed_url = next((f for f in home["feeds"] if _rd(f) == site_domain and "comment" not in f.casefold()), None)

        if feed_url:
            result = get_page(feed_url, "feed")
            if result:
                for post in parse_feed(result["raw"], site_domain):
                    post["evidence"] = {"source_url": result["final_url"], "retrieved_at": result["retrieved_at"], "content_sha256": result["content_sha256"], "claim_span": f'{post["title"][:120]} | {post["published_at"]}'}
                    news_posts.append(post)
        if news_index and not news_posts:
            result = get_page(news_index, "news_index")
            if result:
                page = _parse_page(result)
                page["kind"] = "news_index"
                pages.append(page)
                news_pages.append(page["url"])
                for post in extract_index_posts(page, news_index, site_domain):
                    post["evidence"] = _ev(page, f'{post["title"][:120]} | {post["published_at"] or "undated"}')
                    news_posts.append(post)
                if not news_posts and page["feeds"] and budget.requests < budget.max_requests - 2:
                    alt = next((f for f in page["feeds"] if _rd(f) == site_domain and "comment" not in f.casefold()), None)
                    if alt:
                        alt_result = get_page(alt, "feed")
                        if alt_result:
                            for post in parse_feed(alt_result["raw"], site_domain):
                                post["evidence"] = {"source_url": alt_result["final_url"], "retrieved_at": alt_result["retrieved_at"], "content_sha256": alt_result["content_sha256"], "claim_span": f'{post["title"][:120]} | {post["published_at"]}'}
                                news_posts.append(post)
        if not news_posts:
            # Homepage teasers are often the only news listing on small sites.
            for post in extract_index_posts(home, "", site_domain):
                if post["published_at"]:
                    post["evidence"] = _ev(home, f'{post["title"][:120]} | {post["published_at"]}')
                    news_posts.append(post)

        for url, kind in ((careers_url, "careers"), ):
            if not url:
                continue
            careers["pages"].append({"url": url, "evidence": _ev(home, f"homepage link to {url}")})
            result = get_page(url, kind)
            if result:
                page = _parse_page(result)
                page["kind"] = kind
                pages.append(page)
                postings, none_open = extract_job_postings(page, url, site_domain)
                for posting in postings:
                    posting["evidence"] = _ev(page, posting["title"])
                careers["postings"].extend(postings)
                if none_open:
                    match = NO_VACANCY.search(page["text"])
                    careers["no_vacancies_statement"] = _ev(page, match.group(0))
                ats_url = ats_url or _ats_link(page["links"])
        if ats_url:
            careers["ats_url"] = {"url": ats_url, "evidence": _ev(pages[-1] if careers_url and len(pages) > 1 else home, f"link to {ats_url}")}
            if not careers["pages"]:
                careers["pages"].append({"url": ats_url, "evidence": careers["ats_url"]["evidence"]})
            if budget.requests < budget.max_requests:
                careers["postings"].extend(_ats_listing_postings(ats_url, budget, fetched, errors))
        # Job postings embedded straight on the homepage.
        home_postings, _ = extract_job_postings(home, careers_url or "", site_domain)
        for posting in home_postings:
            if posting["method"] != "careers_child_page":
                posting["evidence"] = _ev(home, posting["title"])
                careers["postings"].append(posting)

        contact_page = None
        if contact_url and budget.requests < budget.max_requests:
            result = get_page(contact_url, "contact")
            if result:
                contact_page = _parse_page(result)
                contact_page["kind"] = "contact"
                pages.append(contact_page)
        if services_url and services_url != contact_url and budget.requests < budget.max_requests:
            result = get_page(services_url, "services")
            if result:
                services_page = _parse_page(result)
                services_page["kind"] = "services"
                pages.append(services_page)

        if not news_posts and budget.requests < budget.max_requests:
            sitemap_url = sitemaps[0] if sitemaps else base + "/sitemap.xml"
            if _rd(sitemap_url) == site_domain:
                result = get_page(sitemap_url, "sitemap")
                if result:
                    news_posts.extend(_sitemap_news(result, site_domain, budget, get_page))
    except _Exhausted:
        errors.append({"error": "per-company request budget or deadline reached"})

    if not pages:
        return evidence("site_signals", "source_error", SOURCE_TYPE, start_url, value={**out, "pages_fetched": fetched, "errors": errors}), _m(budget)

    tokens = brand_tokens(profile, start_url, pages[0]["title"])
    social, social_candidates = extract_social(pages, tokens)
    registry_email = ((profile.get("evidence", {}).get("registry") or {}).get("value") or {}).get("epostadresse")
    emails: dict[str, Any] = {}
    phones: dict[str, Any] = {}
    for page in pages:
        if page.get("kind") not in {"homepage", "contact"}:
            continue
        page_emails, page_phones = extract_contacts(page, site_domain, registry_email)
        for item in page_emails:
            emails.setdefault(item["value"], item)
        for item in page_phones:
            phones.setdefault(item["value"], item)

    home = pages[0]
    description = home["description"] or ""
    services = None
    if services_page:
        heads = [h for h in services_page["headings"] if 3 <= len(h) <= 90][:12]
        services = {"url": services_page["url"], "title": services_page["title"], "description": services_page["description"][:600],
                    "headings": heads, "evidence": _ev(services_page, "; ".join(heads)[:300] or services_page["title"])}
    home_heads = [h for h in home["headings"] if 3 <= len(h) <= 90][:10]
    business = {"meta_description": description[:800] or None, "homepage_title": home["title"], "homepage_headings": home_heads,
                "evidence": _ev(home, description[:300] or home["title"])} if (description or home_heads) else None

    dedup_posts: dict[str, dict[str, Any]] = {}
    for post in news_posts:
        dedup_posts.setdefault(post["url"], post)
    posts = sorted(dedup_posts.values(), key=lambda p: (p.get("published_at") or p.get("modified_at") or "", p["url"]), reverse=True)[:MAX_NEWS_POSTS]
    dedup_jobs: dict[str, dict[str, Any]] = {}
    for posting in careers.get("postings", []):
        dedup_jobs.setdefault(posting["url"] + "|" + posting["title"], posting)
    careers_value = careers
    careers_value["postings"] = list(dedup_jobs.values())[:MAX_JOB_POSTINGS]

    states["social_profiles"] = "available" if social else ("ambiguous" if social_candidates else "not_found")
    states["news_posts"] = "available" if posts else "not_found"
    states["careers"] = "available" if (careers_value.get("pages") or careers_value.get("postings")) else "not_found"
    states["job_postings"] = "available" if careers_value.get("postings") else ("checked_zero" if careers_value.get("no_vacancies_statement") else "not_found")
    states["contact"] = "available" if (emails or phones) else "not_found"
    states["business_description"] = "available" if (business or services) else "not_found"
    out.update({
        "field_states": states,
        "social_profiles": social,
        "social_candidates_unpublished": social_candidates,
        "news_posts": posts,
        "news_posts_collection": {
            # Successful response != exhaustive archive. Feeds and indexes often rotate.
            "collection_complete": False,
            "window_complete": not errors and len(dedup_posts) <= MAX_NEWS_POSTS,
            "window_limit": MAX_NEWS_POSTS,
            "observed_count": len(dedup_posts),
            "returned_count": len(posts),
            "truncated": len(dedup_posts) > MAX_NEWS_POSTS,
        },
        "news_index_pages": news_pages,
        "careers": careers_value,
        "contact": {"emails": list(emails.values()), "phones": list(phones.values())},
        "business_description": business,
        "services": services,
        "pages_fetched": fetched,
        "errors": errors,
        "request_budget": {"max_requests": max_requests, "used": budget.requests},
    })
    any_signal = any(state == "available" for state in states.values())
    return evidence("site_signals", "available" if any_signal else "not_found", SOURCE_TYPE, home["url"], value=out,
                    note="Company-owned claims from the identity-verified official website", content_sha256=home["content_sha256"]), _m(budget)


def _sitemap_news(result: dict[str, Any], site_domain: str, budget: _Budget, get_page) -> list[dict[str, Any]]:
    def parse(raw: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return [], []
        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        urls = [((u.findtext(f"{ns}loc") or "").strip(), (u.findtext(f"{ns}lastmod") or "").strip() or None) for u in root.findall(f"{ns}url")]
        subs = [(s.findtext(f"{ns}loc") or "").strip() for s in root.findall(f"{ns}sitemap")]
        return urls, subs

    urls, subs = parse(result["raw"])
    source = result
    if not urls and subs:
        sub = next((s for s in subs if re.search(r"post|news|nyhet|artikl|blog", s, re.I) and _rd(s) == site_domain), None)
        if sub and budget.requests < budget.max_requests:
            sub_result = get_page(sub, "sitemap")
            if sub_result:
                urls, _ = parse(sub_result["raw"])
                source = sub_result
    posts = []
    for loc, lastmod in urls:
        path = urllib.parse.urlparse(loc).path.casefold()
        if _rd(loc) != site_domain or not re.search(r"/(nyhet|nyheter|aktuelt|news|blogg|blog|presse|press|artikler|articles)/[^/]+", path):
            continue
        modified = _iso_from_any(lastmod) if lastmod else None
        slug = [p for p in path.split("/") if p][-1]
        posts.append({"title": None, "slug": slug[:200], "url": loc, "published_at": None, "date_source": None,
                      "modified_at": modified, "modified_date_source": "sitemap_lastmod" if modified else None,
                      "evidence": {"source_url": source["final_url"], "retrieved_at": source["retrieved_at"], "content_sha256": source["content_sha256"], "claim_span": f"<loc>{loc[:200]}</loc> <lastmod>{lastmod}</lastmod>"}})
    posts.sort(key=lambda p: p["published_at"] or "", reverse=True)
    return posts


def _m(budget: _Budget) -> dict[str, Any]:
    return {"requests": budget.requests, "bytes": budget.bytes, "latencies_ms": list(budget.latencies_ms)}

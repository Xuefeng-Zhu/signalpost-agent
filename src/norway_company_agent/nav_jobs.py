"""Job postings from Nav's public job-vacancy feed (arbeidsplassen.nav.no / pam-stilling-feed).

Access: https://navikt.github.io/pam-stilling-feed/ — free for anyone under
https://arbeidsplassen.nav.no/vilkar-api (republishing allowed; inactive/deleted ads must be removed,
updated ads must be updated; personal data in ads must be handled under personopplysningsloven).
Authentication is a bearer JWT. ``NAV_JOBS_FEED_TOKEN`` (a consumer token Nav issues on request) is
used when set; otherwise the rotating public token Nav publishes at
https://pam-stilling-feed.nav.no/api/publicToken is fetched. Neither is tied to an entrant account.

Matching is exact on organisation number: feed headers carry only ``businessName``, so a normalized
name match against the legal name or a registered subunit name only selects *candidates*; the ad
detail's ``employer.orgnr`` must equal the entity's organisation number or one of its own Brreg
subunits' numbers before anything is published. Contact persons in ads are never stored.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from .evidence import evidence, utc_now

BASE = "https://pam-stilling-feed.nav.no"
PUBLIC_TOKEN_URL = BASE + "/api/publicToken"
AD_PAGE = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
SOURCE_TYPE = "nav_job_vacancy_feed"
UA = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
GENERIC = {"holding", "invest", "norge", "norway", "nordic", "group", "gruppen", "eiendom", "service", "services", "consulting", "og", "i", "the", "and",
           "avd", "avdeling", "bygg", "transport", "regnskap", "drift", "handel", "auto", "bil", "data", "kommune", "barnehage", "skole", "sykehjem", "senter"}
LEGAL = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "ba", "ks", "sf", "iks", "kf", "stiftelsen"}


def normalize_name(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t not in LEGAL]
    return " ".join(tokens)


def _iso(value: datetime) -> str:
    """Explicit ISO-8601 UTC formatting (robust even where ``datetime.isoformat`` is patched)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class NavJobsFeed:
    def __init__(self, *, window_days: int = 183, workers: int = 12, cache_path: str | Path | None = None, timeout: float = 30.0) -> None:
        self.window_days = window_days
        self.workers = workers
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = timeout
        self.token: str | None = None
        self.token_source: str | None = None
        self.headers: dict[str, dict[str, Any]] = {}
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.requests = 0
        self.bytes = 0
        self.pages = 0
        self.synced_at: str | None = None
        self.status = "not_synced"
        self.error: str | None = None
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------
    def _token(self) -> str:
        if self.token:
            return self.token
        env = os.environ.get("NAV_JOBS_FEED_TOKEN", "").strip()
        if env:
            self.token, self.token_source = env, "env:NAV_JOBS_FEED_TOKEN"
            return env
        request = urllib.request.Request(PUBLIC_TOKEN_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
        self._count(len(text))
        match = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", text)
        if not match:
            raise RuntimeError("public token not found")
        self.token, self.token_source = match.group(0), "nav_published_public_token"
        return self.token

    def _count(self, n: int) -> None:
        with self._lock:
            self.requests += 1
            self.bytes += n

    def _get(self, path: str, *, since: datetime | None = None, attempts: int = 3) -> tuple[int, Any, bytes]:
        last: Exception | None = None
        for attempt in range(attempts):
            headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json", "User-Agent": UA}
            if since is not None:
                headers["If-Modified-Since"] = format_datetime(since.astimezone(timezone.utc), usegmt=True)
            request = urllib.request.Request(BASE + path, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                self._count(len(raw))
                return response.status, json.loads(raw), raw
            except urllib.error.HTTPError as exc:
                self._count(0)
                if exc.code in (404, 410):
                    return exc.code, None, b""
                if exc.code == 401 and self.token_source == "nav_published_public_token":
                    self.token = None  # rotated; fetch again
                last = exc
            except Exception as exc:  # noqa: BLE001
                self._count(0)
                last = exc
            time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"Nav feed request failed: {type(last).__name__}: {str(last)[:120]}")

    # -- feed sync ---------------------------------------------------------
    def _segment(self, start: datetime, end: datetime | None, max_pages: int) -> list[dict[str, Any]]:
        status, body, _ = self._get("/api/v1/feed", since=start)
        items: list[dict[str, Any]] = []
        pages = 0
        while body and pages < max_pages:
            pages += 1
            page_items = body.get("items") or []
            items.extend(page_items)
            last_ts = _parse_ts((page_items[-1].get("_feed_entry") or {}).get("sistEndret")) if page_items else None
            next_url = body.get("next_url")
            if not next_url or not page_items or (end is not None and last_ts is not None and last_ts >= end):
                break
            status, body, _ = self._get(next_url)
        with self._lock:
            self.pages += pages
        return items

    def sync(self, *, now: datetime | None = None, max_pages_per_segment: int = 80) -> "NavJobsFeed":
        started = time.monotonic()
        now = now or datetime.now(timezone.utc)
        try:
            self._token()
            start = now - timedelta(days=self.window_days)
            cached_until = self._load_cache()
            if cached_until and cached_until > start:
                start = cached_until - timedelta(hours=1)
            span = (now - start) / max(1, self.workers)
            span = max(span, timedelta(hours=6))
            bounds = []
            cursor = start
            while cursor < now:
                bounds.append((cursor, cursor + span if cursor + span < now else None))
                cursor += span
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(lambda b: self._segment(b[0], b[1], max_pages_per_segment), bounds))
            for items in results:
                for item in items:
                    entry = item.get("_feed_entry") or {}
                    uuid = entry.get("uuid") or item.get("id")
                    if not uuid:
                        continue
                    record = {"uuid": uuid, "status": entry.get("status"), "title": entry.get("title"), "businessName": entry.get("businessName"),
                              "municipal": entry.get("municipal"), "sistEndret": entry.get("sistEndret"), "url": item.get("url")}
                    prior = self.headers.get(uuid)
                    if prior is None or str(record["sistEndret"] or "") >= str(prior.get("sistEndret") or ""):
                        self.headers[uuid] = record
            self.synced_at = utc_now()
            try:
                self._save_cache(now)
            except Exception as exc:  # noqa: BLE001 - a cache write failure must never fail the feed
                self.cache_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.by_name = {}
            self.by_token = {}
            for record in self.headers.values():
                if record.get("status") != "ACTIVE":
                    continue
                key = normalize_name(record.get("businessName"))
                if key:
                    self.by_name.setdefault(key, []).append(record)
                    for token in set(key.split()):
                        if len(token) >= 4 and token not in GENERIC:
                            self.by_token.setdefault(token, []).append(record)
            self.status = "synced"
        except Exception as exc:  # noqa: BLE001 - recorded; every company then gets source_error
            self.status = "source_error"
            self.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        self.sync_seconds = round(time.monotonic() - started, 1)
        return self

    def _load_cache(self) -> datetime | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            with gzip.open(self.cache_path, "rt", encoding="utf-8") as handle:
                meta = json.loads(handle.readline())
                if meta.get("window_days") != self.window_days:
                    return None
                for line in handle:
                    record = json.loads(line)
                    self.headers[record["uuid"]] = record
            return _parse_ts(meta.get("synced_until"))
        except Exception:  # noqa: BLE001 - a broken cache is ignored and rebuilt
            self.headers = {}
            return None

    def _save_cache(self, now: datetime) -> None:
        if not self.cache_path:
            return
        cutoff = now - timedelta(days=self.window_days)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per process/thread: concurrent runs sharing one cache must not clobber each other.
        tmp = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"window_days": self.window_days, "synced_until": _iso(now), "source": BASE + "/api/v1/feed"}) + "\n")
                for record in self.headers.values():
                    ts = _parse_ts(record.get("sistEndret"))
                    if record.get("status") == "ACTIVE" or (ts and ts >= cutoff):
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            tmp.replace(self.cache_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "error": self.error, "token_source": self.token_source, "window_days": self.window_days,
                "headers": len(self.headers), "active_ads": sum(len(v) for v in self.by_name.values()), "pages": self.pages,
                "requests": self.requests, "bytes": self.bytes, "cache_error": getattr(self, "cache_error", None), "sync_seconds": getattr(self, "sync_seconds", None), "synced_at": self.synced_at}

    # -- per company -------------------------------------------------------
    def candidates(self, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
        org = str(profile.get("organisation_number"))
        allowed = {org}
        names = {normalize_name(profile.get("name"))}
        locations = ((profile.get("evidence", {}).get("locations") or {}).get("value") or {}).get("locations") or []
        for location in locations:
            if location.get("organisation_number"):
                allowed.add(str(location["organisation_number"]))
            if location.get("name"):
                names.add(normalize_name(location["name"]))
        names.discard("")
        found: dict[str, dict[str, Any]] = {}
        for name in names:
            for record in self.by_name.get(name, []):
                found[record["uuid"]] = record
            # Token-level candidates (brand forms such as "Ro Catering" for "RO CATERING OG EVENTER AS"):
            # all distinctive words of one name occur in the other. Still only candidates for the orgnr gate.
            distinctive = {t for t in name.split() if len(t) >= 4 and t not in GENERIC}
            if not distinctive:
                continue
            rarest = min(distinctive, key=lambda t: len(getattr(self, "by_token", {}).get(t, [])))
            for record in getattr(self, "by_token", {}).get(rarest, []):
                other = set(normalize_name(record.get("businessName")).split())
                other_distinctive = {t for t in other if len(t) >= 4 and t not in GENERIC}
                if distinctive <= other or (other_distinctive and other_distinctive <= set(name.split())):
                    found[record["uuid"]] = record
        return list(found.values()), allowed, sorted(names)


def collect_nav_jobs(profile: dict[str, Any], feed: NavJobsFeed | None, *, max_details: int = 25) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    source = BASE + "/api/v1/feed"
    if feed is None or feed.status != "synced":
        return evidence("nav_jobs", "source_error", SOURCE_TYPE, source, note=f"Nav feed not available: {getattr(feed, 'error', None) or 'not synced'}"), metrics
    org = str(profile["organisation_number"])
    candidates, allowed, names = feed.candidates(profile)
    candidates.sort(key=lambda r: str(r.get("sistEndret") or ""), reverse=True)
    postings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in candidates[:max_details]:
        started = time.monotonic()
        before_requests, before_bytes = feed.requests, feed.bytes
        try:
            status, body, raw = feed._get(record["url"] or f"/api/v1/feedentry/{record['uuid']}")
        except Exception as exc:  # noqa: BLE001
            rejected.append({"uuid": record["uuid"], "reason": f"detail fetch failed: {str(exc)[:80]}"})
            continue
        finally:
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        metrics["requests"] += 1
        metrics["bytes"] += len(raw)
        content = (body or {}).get("ad_content") or {}
        employer = content.get("employer") or {}
        employer_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        if (body or {}).get("status") != "ACTIVE" or not content:
            rejected.append({"uuid": record["uuid"], "reason": "ad not active or masked in detail"})
            continue
        if employer_org not in allowed:
            rejected.append({"uuid": record["uuid"], "reason": "employer.orgnr is not this entity or one of its subunits", "employer_orgnr": employer_org or None})
            continue
        locations = [
            {k: loc.get(k) for k in ("city", "municipal", "county", "postalCode", "country") if loc.get(k)}
            for loc in content.get("workLocations") or []
        ]
        uuid = content.get("uuid") or record["uuid"]
        postings.append({
            "uuid": uuid,
            "title": content.get("title"),
            "job_title": content.get("jobtitle"),
            "employer_name": employer.get("name"),
            "employer_orgnr": employer_org,
            "matched_via": "entity_orgnr" if employer_org == org else "subunit_orgnr",
            "published_at": content.get("published"),
            "expires_at": content.get("expires"),
            "application_due": content.get("applicationDue"),
            "updated_at": content.get("updated"),
            "work_locations": locations[:5],
            "engagement_type": content.get("engagementtype"),
            "extent": content.get("extent"),
            "position_count": content.get("positioncount"),
            "sector": content.get("sector"),
            "occupations": [{"level1": o.get("level1"), "level2": o.get("level2")} for o in (content.get("occupationCategories") or [])][:3],
            "ad_url": AD_PAGE.format(uuid=uuid),
            "original_source": content.get("source"),
            "employer_homepage": employer.get("homepage"),
            "evidence": {
                "source_url": AD_PAGE.format(uuid=uuid),
                "source_identifier": BASE + (record["url"] or f"/api/v1/feedentry/{uuid}"),
                "retrieved_at": utc_now(),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "claim_span": f'employer.orgnr={employer_org}; employer.name={employer.get("name")}; title={str(content.get("title"))[:120]}',
                "extraction_method": "nav_feed_detail_orgnr_exact_v1",
            },
        })
    value = {
        "postings": postings,
        "active_count": len(postings),
        "candidates_checked": min(len(candidates), max_details),
        "candidates_total": len(candidates),
        "rejected_candidates": rejected[:20],
        "match_policy": "candidate by normalized legal/subunit name in feed header; publish only when ad detail employer.orgnr is the entity or its own Brreg subunit",
        "names_matched": names,
        "feed": {"window_days": feed.window_days, "synced_at": feed.synced_at, "token_source": feed.token_source},
        "terms": "https://arbeidsplassen.nav.no/vilkar-api",
    }
    status = "available" if postings else "not_found"
    note = None if postings else "No active Nav feed ad whose employer.orgnr is this entity (name-prefiltered check over the synced window); not proof of zero vacancies"
    return evidence("nav_jobs", status, SOURCE_TYPE, source, value=value, note=note, retrieved_at=feed.synced_at), metrics

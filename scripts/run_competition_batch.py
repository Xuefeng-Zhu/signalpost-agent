#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website_discovery import resolve_official_website  # noqa: E402
from norway_company_agent.site_signals import collect_site_signals  # noqa: E402
from norway_company_agent.nav_jobs import NavJobsFeed, collect_nav_jobs  # noqa: E402
from norway_company_agent.refresh import classify_refresh_failures, diff_profile  # noqa: E402
from norway_company_agent.synthesis import build_synthesis, llm_config, polish_with_llm  # noqa: E402


def attach_refresh_and_synthesis(profiles: list[dict], previous_path: str | None, use_llm: bool = True, workers: int = 8) -> dict:
    """Diff against the previous snapshot (if supplied) and build the evidence-bounded synthesis.

    Pure post-processing on data already fetched: no network unless the optional LLM polish is configured.
    """
    import hashlib
    import time
    clock = time.monotonic()
    previous: dict[str, dict] = {}
    previous_meta = None
    if previous_path:
        raw = Path(previous_path).read_bytes()
        previous_meta = {"path": str(previous_path), "sha256": hashlib.sha256(raw).hexdigest()}
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                row = row.get("profile", row)  # accept profiles or envelopes
                previous[row["organisation_number"]] = row
    stats = {"previous_snapshot": previous_meta, "companies_compared": 0, "companies_with_changes": 0, "changes": 0, "fields_changed": {}}
    for profile in profiles:
        for key in ("synthesis", "refresh", "changes"):
            profile.pop(key, None)
        org = profile["organisation_number"]
        prior = previous.get(org)
        changes = classify_refresh_failures(prior, profile, diff_profile(prior, profile)) if prior else []
        material = [c for c in changes if c.get("material", True)]
        refresh = {
            "previous_snapshot_supplied": bool(previous_path),
            "company_in_previous_snapshot": prior is not None,
            "previous": ({**previous_meta, "previous_registry_retrieved_at": ((prior.get("evidence") or {}).get("registry_live") or {}).get("retrieved_at")} if prior else previous_meta),
            "tracked_fields_method": "norway_company_agent.refresh.diff_profile (stable claim keys; idempotent)",
            "change_count": len(material),
            "non_material_refresh_failures": len(changes) - len(material),
        }
        if prior:
            stats["companies_compared"] += 1
            stats["companies_with_changes"] += 1 if material else 0
            stats["changes"] += len(material)
            stats["non_material_refresh_failures"] = stats.get("non_material_refresh_failures", 0) + len(changes) - len(material)
            for change in material:
                stats["fields_changed"][change["field"]] = stats["fields_changed"].get(change["field"], 0) + 1
        profile["changes"] = changes
        profile["refresh"] = refresh
        profile["synthesis"] = build_synthesis(profile, changes=changes, refresh=refresh)
    config = llm_config() if use_llm else None
    llm = {"configured": bool(config), "accepted": 0, "rejected": 0, "failed": 0}
    if config:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda p: polish_with_llm(p["synthesis"], config), profiles))
        for p in profiles:
            status = p["synthesis"]["llm_polish"]["status"]
            key = "accepted" if status == "accepted" else "rejected" if status.startswith("rejected") else "failed"
            llm[key] += 1
        llm["model"] = config["model"]
    statements = [len(p["synthesis"]["statements"]) for p in profiles]
    stats["synthesis"] = {
        "method": profiles[0]["synthesis"]["method"] if profiles else None,
        "statements_published": sum(statements),
        "statements_dropped_by_support_check": sum(len(p["synthesis"]["validation"]["dropped"]) for p in profiles),
        "min_statements_per_company": min(statements) if statements else 0,
        "companies_with_unknowns": sum(1 for p in profiles if p["synthesis"]["unknowns"]),
        "llm_polish": llm,
        "runtime_ms": round((time.monotonic() - clock) * 1000),
    }
    return stats


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def website_summary(profiles: list[dict]) -> dict:
    from collections import Counter
    states = Counter((p["evidence"].get("website") or {}).get("status") for p in profiles)
    published = [p for p in profiles if (p["evidence"].get("website") or {}).get("official_website")]
    leads = Counter(p["evidence"]["website"]["official_website"].get("lead_source") for p in published)
    methods = Counter(p["evidence"]["website"]["official_website"].get("identity_method") for p in published)
    return {"published_official_websites": len(published), "website_states": dict(states), "published_by_lead_source": dict(leads), "published_by_identity_method": dict(methods)}


def signal_summary(profiles: list[dict]) -> dict:
    """Per-module / per-field company coverage and fact counts for the new signal modules."""
    from collections import Counter
    out: dict = {}
    for module in ("site_signals", "nav_jobs"):
        records = [(p["evidence"].get(module) or {}) for p in profiles]
        out[module] = {"states": dict(Counter(r.get("status") for r in records))}
    fields = Counter()
    facts = Counter()
    for p in profiles:
        v = (p["evidence"].get("site_signals") or {}).get("value") or {}
        if (p["evidence"].get("site_signals") or {}).get("status") != "available":
            continue
        counts = {
            "social_profiles": len(v.get("social_profiles") or []),
            "news_posts": len(v.get("news_posts") or []),
            "dated_news_posts": len([x for x in v.get("news_posts") or [] if x.get("published_at")]),
            "careers_pages": len((v.get("careers") or {}).get("pages") or []),
            "site_job_postings": len((v.get("careers") or {}).get("postings") or []),
            "emails": len((v.get("contact") or {}).get("emails") or []),
            "phones": len((v.get("contact") or {}).get("phones") or []),
            "business_description": 1 if (v.get("business_description") or v.get("services")) else 0,
        }
        for key, n in counts.items():
            if n:
                fields[key] += 1
                facts[key] += n
    out["site_signals"]["companies_with"] = dict(fields)
    out["site_signals"]["facts"] = dict(facts)
    jobs = [((p["evidence"].get("nav_jobs") or {}).get("value") or {}) for p in profiles]
    out["nav_jobs"]["companies_with_postings"] = sum(1 for j in jobs if j.get("postings"))
    out["nav_jobs"]["postings"] = sum(len(j.get("postings") or []) for j in jobs)
    out["nav_jobs"]["candidates_rejected_by_orgnr_gate"] = sum(len(j.get("rejected_candidates") or []) for j in jobs)
    any_jobs = sum(1 for p in profiles if ((p["evidence"].get("nav_jobs") or {}).get("value") or {}).get("postings")
                   or (((p["evidence"].get("site_signals") or {}).get("value") or {}).get("careers") or {}).get("postings"))
    out["companies_with_any_job_posting"] = any_jobs
    return out


def _publish_social_split(profile: dict) -> None:
    """Keep only identity-gated profiles in the website record's social_links; the rest become candidates."""
    site = profile["evidence"].get("site_signals") or {}
    website = profile["evidence"].get("website") or {}
    value = website.get("value")
    if not isinstance(value, dict) or "social_links" not in value:
        return
    if site.get("status") == "available":
        published = {item["url"] for item in (site.get("value") or {}).get("social_profiles") or []}
    else:
        published = set()
    original = value.get("social_links") or []
    value["social_links"] = [item for item in original if item.get("url") in published]
    value["social_link_candidates_unpublished"] = [item for item in original if item.get("url") not in published]
    value["social_links_policy"] = "only links that pass site_signals handle/JSON-LD matching on an owned, verified site are published"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-website-discovery", action="store_true", help="Only check the registry hjemmeside")
    parser.add_argument("--website-request-cap", type=int, default=14, help="Per-company HTTP cap for website discovery probes")
    parser.add_argument("--site-signals-request-cap", type=int, default=8, help="Per-company HTTP cap for verified-site signal pages")
    parser.add_argument("--nav-window-days", type=int, default=183, help="Nav job feed sync window (ads are active at most ~6 months)")
    parser.add_argument("--nav-cache", default=".cache/nav-job-feed-headers.jsonl.gz", help="Declared external cache of Nav feed headers ('' disables)")
    parser.add_argument("--previous-profiles", default=None, help="Previous run's profiles (or envelopes) JSONL; enables material-change output")
    parser.add_argument("--no-llm-synthesis", action="store_true", help="Never call the optional LLM polish even if SIGNALPOST_LLM_API_KEY is set")
    parser.add_argument("--site-dir", default=None, help="Also build the static review site into this directory")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,site_signals,nav_jobs")
    args = parser.parse_args()

    started_at = utc_now()
    wall_clock = __import__("time").monotonic()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    nav_feed = None
    nav_thread = None
    if "nav_jobs" in requested_modules:
        import threading
        nav_feed = NavJobsFeed(window_days=args.nav_window_days, workers=16, cache_path=args.nav_cache or None)
        nav_thread = threading.Thread(target=nav_feed.sync, daemon=True)
        nav_thread.start()  # overlaps with the bulk CSV scan
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "site_signals", "nav_jobs"}
    if "nav_jobs" in requested_modules:
        fetch_modules.add("locations")  # subunit org numbers are part of the exact-employer gate
    nav_sync_summary = None
    operations = {"requests": 0, "bytes": 0, "latencies_ms": [], "dns_lookups": 0, "website_requests": 0, "site_signals_requests": 0, "nav_detail_requests": 0}
    run_clock = __import__("time").monotonic()

    def enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": [], "dns_lookups": 0}
        if "website" in requested_modules:
            try:
                website_record, website_metrics = resolve_official_website(
                    profile, discovery=not args.no_website_discovery, max_requests=args.website_request_cap,
                )
            except Exception as exc:  # never drop a company because website discovery crashed
                website_record = evidence("website", "source_error", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note=f"website step failed: {type(exc).__name__}: {str(exc)[:160]}")
            profile["evidence"]["website"] = website_record
        extra = {"requests": 0, "bytes": 0, "latencies_ms": []}
        site_requests = nav_requests = 0
        if "site_signals" in requested_modules:
            try:
                site_record, site_metrics = collect_site_signals(profile, profile["evidence"].get("website"), max_requests=args.site_signals_request_cap)
            except Exception as exc:  # never drop a company because a signal module crashed
                site_record, site_metrics = evidence("site_signals", "source_error", "company_owned_site_signals", (profile["evidence"].get("website") or {}).get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter", note=f"site_signals failed: {type(exc).__name__}: {str(exc)[:160]}"), {"requests": 0, "bytes": 0, "latencies_ms": []}
            profile["evidence"]["site_signals"] = site_record
            _publish_social_split(profile)
            site_requests = site_metrics["requests"]
            for key in extra:
                extra[key] += site_metrics[key]
        if "nav_jobs" in requested_modules:
            try:
                if nav_thread is not None:
                    nav_thread.join()  # feed sync overlaps with registry/website work; joined only when needed
                jobs_record, jobs_metrics = collect_nav_jobs(profile, nav_feed)
            except Exception as exc:
                jobs_record, jobs_metrics = evidence("nav_jobs", "source_error", "nav_job_vacancy_feed", "https://pam-stilling-feed.nav.no/api/v1/feed", note=f"nav_jobs failed: {type(exc).__name__}: {str(exc)[:160]}"), {"requests": 0, "bytes": 0, "latencies_ms": []}
            profile["evidence"]["nav_jobs"] = jobs_record
            nav_requests = jobs_metrics["requests"]
            for key in extra:
                extra[key] += jobs_metrics[key]
        metric = {
            "requests": len(metrics) + website_metrics["requests"] + extra["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + extra["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + extra["latencies_ms"],
            "dns_lookups": website_metrics.get("dns_lookups", 0),
            "website_requests": website_metrics["requests"],
            "site_signals_requests": site_requests,
            "nav_detail_requests": nav_requests,
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            operations["dns_lookups"] += metric.get("dns_lookups", 0)
            operations["website_requests"] += metric.get("website_requests", 0)
            operations["site_signals_requests"] += metric.get("site_signals_requests", 0)
            operations["nav_detail_requests"] += metric.get("nav_detail_requests", 0)
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    if nav_thread is not None:
        nav_thread.join()
        nav_sync_summary = nav_feed.summary()
    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    refresh_summary = attach_refresh_and_synthesis(ordered_profiles, args.previous_profiles, use_llm=not args.no_llm_synthesis, workers=args.workers)
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["runtime_s"] = round(__import__("time").monotonic() - run_clock, 1)
    operations["wall_s_including_bulk_scan_and_nav_sync"] = round(__import__("time").monotonic() - wall_clock, 1)
    operations["third_party_cost_usd"] = 0
    if nav_sync_summary:
        operations["nav_feed_sync"] = nav_sync_summary
        nav_sync_summary["sync_requests"] = nav_sync_summary["requests"] - operations["nav_detail_requests"]
        operations["requests_including_shared_nav_sync"] = operations["requests"] + nav_sync_summary["sync_requests"]
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
        "website_summary": website_summary(ordered_profiles),
        "signal_summary": signal_summary(ordered_profiles),
        "refresh_and_synthesis": refresh_summary,
    }
    if args.site_dir:
        sys.path.insert(0, str(ROOT / "scripts"))
        from build_site import build_site  # noqa: E402
        report["site"] = build_site([(args.run_id, Path(args.profiles_output))], Path(args.site_dir))
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()

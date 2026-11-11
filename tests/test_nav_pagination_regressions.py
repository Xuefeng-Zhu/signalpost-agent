from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.nav_jobs import BASE, NavJobsFeed, collect_nav_jobs
from norway_company_agent.refresh import diff_profile

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
CHECKPOINT = NOW - timedelta(hours=2)
PROFILE = {"organisation_number": "123456789", "name": "Example AS"}


def item(uuid="old", status="ACTIVE", ts=CHECKPOINT):
    return {"_feed_entry": {"uuid": uuid, "status": status, "businessName": "Example AS", "sistEndret": ts.isoformat()}, "url": f"/api/v1/feedentry/{uuid}"}


def feed_with_cache(tmp_path, monkeypatch):
    feed = NavJobsFeed(workers=1, cache_path=tmp_path / "feed.gz")
    feed.headers["old"] = {**item()["_feed_entry"], "url": item()["url"]}
    feed._save_cache(CHECKPOINT)
    monkeypatch.setattr(feed, "_token", lambda: "test-token")
    return feed


def assert_no_removal(feed):
    result, _ = collect_nav_jobs(PROFILE, feed)
    assert result["status"] == "source_error"
    previous = {**PROFILE, "evidence": {"nav_jobs": {"status": "available", "value": {"postings": [{"uuid": "old"}]}}}}
    current = {**PROFILE, "evidence": {"nav_jobs": result}}
    changes = diff_profile(previous, current)
    assert changes and all(not c["material"] for c in changes)


def test_page_cap_keeps_checkpoint_and_discards_partial_deletion(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs))
        return 200, {"items": [item(status="INACTIVE", ts=NOW)], "next_url": "/api/v1/feed?page=2"}, b""

    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW, max_pages_per_segment=1)
    assert feed.status == "incomplete"
    assert "page limit" in feed.error
    assert len(calls) == 1  # Do not fetch an extra page only to discard it.
    assert feed.cache_path.read_bytes() == original
    assert feed.headers["old"]["status"] == "ACTIVE"
    assert_no_removal(feed)

    # A successful retry starts from the unchanged checkpoint and sees unread ads.
    calls.clear()
    def complete(path, **kwargs):
        calls.append((path, kwargs))
        return 200, {"items": [item("unread", ts=NOW)], "next_url": None}, b""
    monkeypatch.setattr(feed, "_get", complete)
    feed.sync(now=NOW)
    assert calls[0][1]["since"] == CHECKPOINT - timedelta(hours=1)
    assert feed.status == "synced" and feed.error is None
    assert "unread" in feed.headers
    with gzip.open(feed.cache_path, "rt") as handle:
        assert json.loads(handle.readline())["synced_until"] == NOW.isoformat()


@pytest.mark.parametrize("next_url", ["/api/v1/feed", BASE + "/api/v1/feed", "https://evil.example/api/v1/feed", "//evil.example/api/v1/feed", "/api/publicToken", "/api/v1/feed\n?x=1"])
def test_bad_pagination_never_advances_cache(tmp_path, monkeypatch, next_url):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    calls = []
    def get(path, **kwargs):
        calls.append(path)
        return 200, {"items": [item()], "next_url": next_url}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW)
    assert feed.status == "incomplete"
    assert len(calls) == 1
    assert feed.cache_path.read_bytes() == original
    assert_no_removal(feed)


def test_page_error_preserves_cache(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    def get(path, **kwargs):
        if "page=2" in path:
            raise RuntimeError("network failed")
        return 200, {"items": [item(status="INACTIVE")], "next_url": "/api/v1/feed?page=2"}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW)
    assert feed.status == "source_error"
    assert feed.cache_path.read_bytes() == original
    assert feed.headers["old"]["status"] == "ACTIVE"
    assert_no_removal(feed)


def test_empty_page_with_next_is_not_treated_as_complete(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    calls = []
    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            return 200, {"items": [], "next_url": BASE + "/api/v1/feed?page=2"}, b""
        return 200, {"items": [item("unread")], "next_url": None}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW, max_pages_per_segment=2)
    assert feed.status == "synced"
    assert len(calls) == 2 and "unread" in feed.headers


@pytest.mark.parametrize("status,body", [(404, None), (200, {}), (200, {"items": [None]}), (200, {"items": "invalid"})])
def test_invalid_pages_are_incomplete(tmp_path, monkeypatch, status, body):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (status, body, b""))
    feed.sync(now=NOW)
    assert feed.status == "incomplete"
    assert feed.cache_path.read_bytes() == original
    assert_no_removal(feed)


def test_segment_boundary_completes_without_following_next(monkeypatch):
    feed = NavJobsFeed(workers=1)
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [item(ts=NOW)], "next_url": "/api/v1/feed?page=2"}, b""))
    assert feed._segment(CHECKPOINT, NOW, 1) == [item(ts=NOW)]


def test_partial_initial_sync_does_not_create_checkpoint(tmp_path, monkeypatch):
    feed = NavJobsFeed(workers=1, cache_path=tmp_path / "new.gz")
    monkeypatch.setattr(feed, "_token", lambda: "test-token")
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [item()], "next_url": "/api/v1/feed?page=2"}, b""))
    feed.sync(now=NOW, max_pages_per_segment=1)
    assert feed.status == "incomplete" and not feed.cache_path.exists()
    assert feed.headers == {}


def test_any_incomplete_segment_discards_all_segment_results(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    feed.workers = 2
    start = NOW - timedelta(hours=20)
    feed._save_cache(start)
    original = feed.cache_path.read_bytes()
    def get(path, since=None):
        if since == start - timedelta(hours=1):
            return 200, {"items": [item(status="INACTIVE")], "next_url": None}, b""
        return 200, {"items": [item("unread")], "next_url": "/api/v1/feed?page=2"}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW, max_pages_per_segment=1)
    assert feed.status == "incomplete"
    assert feed.cache_path.read_bytes() == original
    assert feed.headers["old"]["status"] == "ACTIVE"
    assert "unread" not in feed.headers


@pytest.mark.parametrize("bad_item", [
    {}, {"id": "event-only"}, {"_feed_entry": []}, {"_feed_entry": "bad"},
    *[{"id": "event-id", "_feed_entry": {**item()["_feed_entry"], "uuid": value}}
      for value in (None, "", " ", [], {}, 123, "../other")],
    *[{"_feed_entry": {**item()["_feed_entry"], "status": value}}
      for value in (None, "", "UNKNOWN", [], {})],
    *[{"_feed_entry": {**item()["_feed_entry"], "sistEndret": value}}
      for value in (None, "", "not-a-date", "2026-11-10", "2026-11-10T00:00:00", 123, {})],
])
def test_malformed_header_never_advances_checkpoint(tmp_path, monkeypatch, bad_item):
    feed = feed_with_cache(tmp_path, monkeypatch)
    original = feed.cache_path.read_bytes()
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [item(status="INACTIVE"), bad_item], "next_url": None}, b""))
    feed.sync(now=NOW)
    assert feed.status == "incomplete"
    assert feed.cache_path.read_bytes() == original
    assert feed.headers["old"]["status"] == "ACTIVE"
    assert_no_removal(feed)


@pytest.mark.parametrize("status", ["ACTIVE", "INACTIVE"])
def test_documented_production_status_values_sync(tmp_path, monkeypatch, status):
    feed = feed_with_cache(tmp_path, monkeypatch)
    incoming = item(status=status, ts=NOW)
    incoming["_feed_entry"]["sistEndret"] = NOW.isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [incoming], "next_url": None}, b""))
    feed.sync(now=NOW)
    assert feed.status == "synced"
    assert feed.headers["old"]["status"] == status


@pytest.mark.parametrize("checkpoint", [NOW + timedelta(days=5), NOW.replace(tzinfo=None)])
def test_unusable_cache_horizon_does_not_skip_feed(tmp_path, monkeypatch, checkpoint):
    feed = feed_with_cache(tmp_path, monkeypatch)
    feed._save_cache(checkpoint)
    calls = []
    def get(path, since=None):
        calls.append(since)
        return 200, {"items": [], "next_url": None}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW)
    assert calls == [NOW - timedelta(days=feed.window_days)]
    assert feed.status == "synced"


def test_same_origin_page_id_url_is_followed(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    calls = []
    def get(path, **kwargs):
        calls.append(path)
        return 200, {"items": [item()], "next_url": BASE + "/api/v1/feed/page-2" if len(calls) == 1 else None}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW)
    assert feed.status == "synced"
    assert calls == ["/api/v1/feed", "/api/v1/feed/page-2"]


def test_header_updates_compare_instants_not_timezone_strings(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    incoming = item(status="INACTIVE")
    # Local hour is later but actual instant is earlier than cached UTC timestamp.
    incoming["_feed_entry"]["sistEndret"] = "2026-11-09T23:00:00+02:00"
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [incoming], "next_url": None}, b""))
    feed.sync(now=NOW)
    assert feed.status == "synced" and feed.headers["old"]["status"] == "ACTIVE"


@pytest.mark.parametrize("horizon", [None, "invalid", (NOW + timedelta(days=5)).isoformat(), NOW.replace(tzinfo=None).isoformat()])
def test_untrusted_checkpoint_discards_poisoned_cached_headers(tmp_path, monkeypatch, horizon):
    feed = feed_with_cache(tmp_path, monkeypatch)
    poisoned = {**item(status="INACTIVE", ts=NOW + timedelta(days=10))["_feed_entry"], "url": item()["url"]}
    with gzip.open(feed.cache_path, "wt") as handle:
        handle.write(json.dumps({"window_days": feed.window_days, "synced_until": horizon}) + "\n")
        handle.write(json.dumps(poisoned) + "\n")
        handle.write(json.dumps({**poisoned, "uuid": "stale-only"}) + "\n")
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [item(status="ACTIVE", ts=NOW)], "next_url": None}, b""))
    feed.sync(now=NOW)
    assert feed.status == "synced"
    assert feed.headers["old"]["status"] == "ACTIVE"
    assert set(feed.headers) == {"old"}


def test_untrusted_cache_remains_on_disk_if_rebuild_fails(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    feed._save_cache(NOW + timedelta(days=5))
    original = feed.cache_path.read_bytes()
    monkeypatch.setattr(feed, "_get", lambda *a, **kw: (200, {"items": [{}], "next_url": None}, b""))
    feed.sync(now=NOW)
    assert feed.status == "incomplete"
    assert feed.headers == {}
    assert feed.cache_path.read_bytes() == original


def test_quiet_tail_404_is_confirmed_by_a_widened_read(tmp_path, monkeypatch):
    feed = feed_with_cache(tmp_path, monkeypatch)
    calls = []
    def get(path, since=None):
        calls.append(since)
        if len(calls) == 1:
            return 404, None, b""
        return 200, {"items": [item("fresh", status="ACTIVE", ts=NOW - timedelta(hours=5))], "next_url": None}, b""
    monkeypatch.setattr(feed, "_get", get)
    feed.sync(now=NOW)
    assert feed.status == "synced"
    assert calls[1] == calls[0] - timedelta(hours=24)
    assert "fresh" in feed.headers

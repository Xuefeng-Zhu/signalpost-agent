"""Offline end-to-end coverage for checkpoint and optional-output failures."""
from __future__ import annotations

import importlib.util
import json
import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("competition_batch_regressions", ROOT / "scripts/run_competition_batch.py")
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    bulk = tmp_path / "bulk.csv"
    bulk.write_bytes(gzip.compress((ROOT / "tests/fixtures/bulk_quoted_row.csv").read_bytes()))
    orgs = tmp_path / "orgs.txt"
    orgs.write_text("818711832\n", encoding="utf-8")
    # Fail closed if an offline test accidentally asks for official enrichment.
    def official(org, modules):
        assert not modules
        return {}, []
    monkeypatch.setattr(batch, "fetch_official_modules", official)
    def run(*extra):
        argv = ["run_competition_batch", "--organisations", str(orgs), "--bulk", str(bulk),
                "--output", str(tmp_path / "envelopes.jsonl"),
                "--profiles-output", str(tmp_path / "profiles.jsonl"),
                "--report", str(tmp_path / "report.json"), "--run-id", "offline",
                "--modules", "registry,accounting_obligation", "--expected-count", "1",
                "--no-llm-synthesis", *map(str, extra)]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as result:
            batch.main()
        assert result.value.code == 0
        return json.loads((tmp_path / "report.json").read_text())
    return run


def test_bulk_content_mutation_invalidates_resume(runner, tmp_path):
    runner()
    assert runner("--resume")["resumed_profiles"] == 1
    bulk = tmp_path / "bulk.csv"
    bulk.write_bytes(gzip.compress(gzip.decompress(bulk.read_bytes()).replace(b"LUFTAMBULANSETJENESTEN HF", b"CHANGED COMPANY HF")))
    report = runner("--resume")
    assert report["resumed_profiles"] == 0
    assert "fingerprint" in report["resume_note"]
    assert json.loads((tmp_path / "profiles.jsonl").read_text())["name"] == "CHANGED COMPANY HF"


@pytest.mark.parametrize("option,value", [("--site-signals-request-cap", "3"), ("--nav-window-days", "7"),
                                           ("--website-request-cap", "2"), ("--no-website-discovery", None)])
def test_configuration_mutation_invalidates_resume(runner, option, value):
    runner()
    report = runner("--resume", option, *([] if value is None else [value]))
    assert report["resumed_profiles"] == 0
    assert report["profiles_fetched_this_run"] == 1


def test_synthesis_failure_still_writes_site_and_envelopes(runner, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected synthesis failure")
    monkeypatch.setattr(batch, "build_synthesis", fail)
    report = runner("--site-dir", tmp_path / "site")
    assert report["refresh_and_synthesis"]["synthesis"]["companies_failed"] == 1
    envelope = json.loads((tmp_path / "envelopes.jsonl").read_text())
    assert any(e["field"] == "synthesis" for e in envelope["errors"])
    assert (tmp_path / "site/index.html").exists()
    assert report["site"].get("status") != "failed"


def test_site_failure_does_not_lose_report(runner, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site
    def fail(*args):
        assert (tmp_path / "report.json").exists()
        raise RuntimeError("injected renderer failure")
    monkeypatch.setattr(build_site, "build_site", fail)
    report = runner("--site-dir", tmp_path / "site")
    assert report["site"]["status"] == "failed"
    assert "injected renderer" in report["site"]["error"]
    assert (tmp_path / "envelopes.jsonl").exists()


@pytest.mark.parametrize("data", [b'{"profile":null}\n{"profile":[]}\n[]\nnull\n{bad json\n', b'\xff\xfe'])
def test_malformed_optional_prior_does_not_abort(runner, tmp_path, data):
    previous = tmp_path / "previous.jsonl"
    previous.write_bytes(data)
    report = runner("--previous-profiles", previous)
    meta = report["refresh_and_synthesis"]["previous_snapshot"]
    assert meta.get("unreadable_rows", 0) or meta.get("error")
    assert report["emitted_envelopes"] == 1


@pytest.mark.parametrize("data", [b'null\n', b'[]\n', b'{"profile":null}\n', b'\xff',
                                  b'{"organisation_number":"818711832","evidence":[]}\n',
                                  b'{"organisation_number":"818711832","evidence":{"registry":null}}\n'])
def test_malformed_checkpoint_is_recollected(runner, tmp_path, data):
    runner()
    (tmp_path / "profiles.jsonl").write_bytes(data)
    report = runner("--resume")
    assert report["resumed_profiles"] == 0
    assert "checkpoint" in report["resume_note"]
    assert report["emitted_envelopes"] == 1


def test_nav_cache_identity_preserves_own_writes_but_rejects_edits(runner, tmp_path, monkeypatch):
    cache = tmp_path / "nav-cache"
    cache.write_text("initial cache")
    syncs = []
    class Feed:
        def __init__(self, **kwargs):
            self.cache = Path(kwargs["cache_path"])
        def sync(self):
            syncs.append(True)
            self.cache.write_text("own synchronized cache")
        def summary(self):
            return {"requests": 0}
    monkeypatch.setattr(batch, "NavJobsFeed", Feed)
    monkeypatch.setattr(batch, "fetch_official_modules", lambda org, modules: ({}, []))
    monkeypatch.setattr(batch, "collect_nav_jobs", lambda profile, feed: (
        batch.evidence("nav_jobs", "not_found", "test", "https://example.invalid", value={}),
        {"requests": 0, "bytes": 0, "latencies_ms": []}))
    opts = ("--modules", "registry,accounting_obligation,nav_jobs", "--nav-cache", cache)
    first = runner(*opts)
    assert first["run_fingerprint"]["nav_cache"]["sha256"] != batch.file_digest(cache)
    assert runner(*opts, "--resume")["resumed_profiles"] == 1
    assert len(syncs) == 1  # a complete resume does not sync or mutate the cache
    cache.write_text("external change")
    assert runner(*opts, "--resume")["resumed_profiles"] == 0
    other = tmp_path / "other-cache"
    other.write_text(cache.read_text())
    assert runner("--modules", "registry,accounting_obligation,nav_jobs", "--nav-cache", other,
                  "--resume")["resumed_profiles"] == 0


@pytest.mark.parametrize("data", [b'[]', b'null', b'{bad json', b'\xff'])
def test_malformed_fingerprint_is_ignored(runner, tmp_path, data):
    runner()
    (tmp_path / "profiles.jsonl.fingerprint.json").write_bytes(data)
    report = runner("--resume")
    assert report["resumed_profiles"] == 0
    assert "fingerprint" in report["resume_note"]


def test_unreadable_optional_prior_is_reported(runner, tmp_path):
    report = runner("--previous-profiles", tmp_path / "missing.jsonl")
    assert "FileNotFoundError" in report["refresh_and_synthesis"]["previous_snapshot"]["error"]


@pytest.mark.parametrize("module,record", [
    ("registry", {"status": "available", "retry_count": "bad", "value": {}}),
    ("registry", {"status": ["available"], "value": {}}),
    ("registry", {"status": "available", "value": ["not a registry object"]}),
    ("website", {"status": "available", "official_website": ["not an object"]}),
    ("site_signals", {"status": "available", "value": ["not an object"]}),
    ("site_signals", {"status": "available", "value": {"news_posts": ["not an object"]}}),
    ("site_signals", {"status": "available", "value": {"careers": ["not an object"]}}),
    ("nav_jobs", {"status": "available", "value": ["not an object"]}),
])
def test_malformed_nested_checkpoint_is_recollected(runner, tmp_path, module, record):
    runner()
    checkpoint = tmp_path / "profiles.jsonl"
    profile = json.loads(checkpoint.read_text())
    profile["evidence"][module] = record
    checkpoint.write_text(json.dumps(profile) + "\n")
    report = runner("--resume")
    assert report["resumed_profiles"] == 0
    assert "invalid profile" in report["resume_note"]
    assert report["emitted_envelopes"] == 1
    assert json.loads((tmp_path / "envelopes.jsonl").read_text())["organisation_number"] == "818711832"

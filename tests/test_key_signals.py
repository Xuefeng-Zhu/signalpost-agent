from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_synthesis import profile  # noqa: E402
from norway_company_agent.synthesis import build_synthesis, validate_synthesis  # noqa: E402


def _signals(syn):
    return [s for s in syn["statements"] if s["section"] == "key_signals"]


def test_key_signals_are_cited_derived_and_validated():
    p = profile()
    syn = build_synthesis(p)
    texts = [s["text"] for s in _signals(syn)]
    assert syn["validation"]["passed"], syn["validation"]
    # 2025 filing has no revenue -> no margin; equity 482486 / assets 844291 = 57.1 %.
    assert not any(t.startswith("Operating margin") for t in texts)
    assert any(t.startswith("Equity ratio: 57.1%") for t in texts)
    assert any("a net loss in the latest filed year, after a profit the year before" in t for t in texts)
    assert all(s["citations"] for s in _signals(syn))
    assert syn["sections"][1]["key"] == "key_signals"


def test_tampered_ratio_and_overview_are_rejected():
    p = profile()
    syn = build_synthesis(p)
    bad = copy.deepcopy(syn)
    for s in bad["statements"]:
        if s["text"].startswith("Equity ratio"):
            s["text"] = s["text"].replace("57.1%", "75.1%")
            s["derived"][0]["value"] = 75.1
    assert validate_synthesis(bad, p)["failures"]
    bad = copy.deepcopy(syn)
    bad["overview"]["text"] += " It is the market leader."
    assert any("overview" in str(f) for f in validate_synthesis(bad, p)["failures"])
    assert not validate_synthesis(syn, p)["failures"]


def test_negative_equity_and_stale_filing_signals():
    p = profile()
    rec = p["evidence"]["financials"]["value"]["records"][1]
    rec.update({"equity": -1000.0, "period": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"}})
    p["evidence"]["financials"]["value"]["records"][0]["period"] = {"fraDato": "2022-01-01", "tilDato": "2022-12-31"}
    p["evidence"]["financials"]["retrieved_at"] = "2026-11-10T17:51:00+00:00"
    texts = [s["text"] for s in _signals(build_synthesis(p))]
    assert any("equity is negative" in t for t in texts)
    assert any(t.startswith("Filing recency") for t in texts)

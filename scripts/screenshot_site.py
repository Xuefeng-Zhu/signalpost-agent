#!/usr/bin/env python3
"""Render the static site in headless Chromium and save desktop + mobile screenshots.

    uv run --with playwright python scripts/screenshot_site.py --site site --org 931015737 --compare 931015737,985589003

Also checks for horizontal overflow and JS errors on every view (exit 1 if any).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": {"width": 1366, "height": 900}, "mobile": {"width": 390, "height": 844}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="site")
    parser.add_argument("--org", required=True)
    parser.add_argument("--compare", required=True, help="orgA,orgB")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    site = Path(args.site).resolve()
    out = Path(args.out or site / "screenshots")
    out.mkdir(parents=True, exist_ok=True)
    a, b = args.compare.split(",")
    views = {"list": "#/", "search": "#/", "company": f"#/c/{args.org}", "compare": f"#/compare/{a}/{b}", "about": "#/about"}
    report = {"views": [], "errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for device, viewport in VIEWPORTS.items():
            context = browser.new_context(viewport=viewport, device_scale_factor=1 if device == "desktop" else 2, is_mobile=device == "mobile", has_touch=device == "mobile")
            page = context.new_page()
            page.on("pageerror", lambda exc, d=device: report["errors"].append(f"{d}: {exc}"))
            for name, hash_ in views.items():
                page.goto(site.joinpath("index.html").as_uri() + hash_)
                page.wait_for_timeout(250)
                if name == "search":
                    page.fill("#q", "bemanning")
                    page.wait_for_timeout(150)
                overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                path = out / f"{device}-{name}.png"
                page.screenshot(path=str(path), full_page=name in ("company", "compare"))
                if name == "company":
                    page.screenshot(path=str(out / f"{device}-company-top.png"))
                report["views"].append({"device": device, "view": name, "horizontal_overflow_px": overflow, "screenshot": str(path)})
            context.close()
        browser.close()
    print(json.dumps(report, indent=2))
    bad = report["errors"] or any(v["horizontal_overflow_px"] > 0 for v in report["views"])
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()

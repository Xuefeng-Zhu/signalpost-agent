"""Optional headless-Chromium rendering for JS-only company sites (empty static HTML shells).

Used only as a bounded fallback inside website discovery: when a promising candidate's static
homepage carries almost no text (a React/Vue/Lovable/Wix shell), the page is rendered once so the
organisation number in its footer or contact section can be read. Identity rules are unchanged:
the rendered HTML goes through exactly the same org-number / legal-name+address gate.

Playwright is an *optional* dependency (``uv sync --extra browser`` then
``playwright install chromium``). When it is not importable, or Chromium cannot launch, every call
returns ``None`` and discovery simply continues with the static result. ``SIGNALPOST_JS_FALLBACK=0``
turns rendering off. Images, fonts and media are not downloaded; robots.txt is checked by the
caller before a page is rendered.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"checked": False, "available": False, "reason": None}
_SLOTS = threading.BoundedSemaphore(int(os.environ.get("SIGNALPOST_JS_CONCURRENCY", "3") or 3))
_STATS = {"renders": 0, "failures": 0, "ms": 0}
_STATS_LOCK = threading.Lock()

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome Safari/537.36 builderr-signalpost-poc/0.1 (+https://builderr.ai)"
JS_SHELL_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", 'id="__next"', "__nuxt", "ng-version", "data-reactroot", "lovable", "wix.com", "squarespace", "webflow", "framer")


def enabled() -> bool:
    return os.environ.get("SIGNALPOST_JS_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}


def available() -> bool:
    """True when Playwright imports and Chromium launches (checked once per process)."""
    if not enabled():
        return False
    with _LOCK:
        if _STATE["checked"]:
            return _STATE["available"]
        _STATE["checked"] = True
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - optional dependency
            _STATE["reason"] = f"playwright not installed: {type(exc).__name__}"
            return False
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                browser.close()
            _STATE["available"] = True
        except Exception as exc:  # noqa: BLE001 - browser binary or system libs missing
            _STATE["reason"] = f"chromium launch failed: {type(exc).__name__}: {str(exc)[:120]}"
        return _STATE["available"]


def status() -> dict[str, Any]:
    with _STATS_LOCK:
        return {"enabled": enabled(), "available": _STATE["available"], "checked": _STATE["checked"], "reason": _STATE["reason"], **_STATS}


def looks_like_js_shell(raw: bytes | str, text_length: int) -> bool:
    """Static HTML with (almost) no readable text, or a known SPA/site-builder shell with little text."""
    head = raw[:200_000].decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)[:200_000]
    lowered = head.casefold()
    scripts = lowered.count("<script")
    if text_length < 400 and scripts >= 1:
        return True
    return text_length < 1500 and scripts >= 3 and any(marker in lowered for marker in JS_SHELL_MARKERS)


def _private_host(url: str) -> bool:
    import ipaddress
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https", "data", "blob"}:
        return True
    host = (parsed.hostname or "").strip("[]").casefold()
    if parsed.scheme in {"data", "blob"}:
        return False
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _route(route: Any) -> None:
    request = route.request
    if request.resource_type in {"image", "media", "font"} or _private_host(request.url):
        route.abort()
    else:
        route.continue_()


def render(url: str, *, timeout_s: float = 15.0, settle_ms: int = 1500) -> dict[str, Any] | None:
    """Render ``url`` and return ``{"final_url", "html", "links", "elapsed_ms"}`` or ``None``."""
    if not available():
        return None
    acquired = _SLOTS.acquire(timeout=timeout_s)
    if not acquired:
        return None
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(user_agent=USER_AGENT, java_script_enabled=True, locale="nb-NO")
                context.route("**/*", _route)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=int(min(6.0, timeout_s) * 1000))
                except Exception:  # noqa: BLE001 - long-polling sites never go idle; use what rendered
                    pass
                page.wait_for_timeout(settle_ms)
                html = page.content()
                final_url = page.url
                links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim().slice(0, 80)])")
            finally:
                browser.close()
        elapsed = int((time.monotonic() - started) * 1000)
        with _STATS_LOCK:
            _STATS["renders"] += 1
            _STATS["ms"] += elapsed
        return {"final_url": final_url, "html": html, "links": [tuple(item) for item in links or []], "elapsed_ms": elapsed}
    except Exception:  # noqa: BLE001 - a render failure is just "no extra evidence"
        with _STATS_LOCK:
            _STATS["failures"] += 1
        return None
    finally:
        _SLOTS.release()

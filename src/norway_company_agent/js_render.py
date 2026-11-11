"""JS-shell detection and fail-closed browser fallback.

Browser rendering is currently disabled, including when Playwright is installed or
``SIGNALPOST_JS_FALLBACK=1`` is set. Chromium resolves names independently of Python;
URL filtering and DNS prechecks cannot prevent DNS rebinding / resolution-to-connect
races, proxy-side resolution, or browser traffic outside HTTP route interception
(service workers, WebSockets, WebRTC, and browser background requests).

Re-enabling rendering requires a separately enforced, tested network boundary for
*all* browser egress, including redirects and subresources: only public destinations
at connection time, no direct/proxy bypass, and no access to local/private networks.
No such boundary is provided by this module. Static discovery remains available.
The URL/DNS helpers below are defense in depth, NOT that network boundary.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Any

_DISABLED_REASON = "browser rendering disabled: no enforced public-only browser egress boundary"

JS_SHELL_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", 'id="__next"', "__nuxt", "ng-version", "data-reactroot", "lovable", "wix.com", "squarespace", "webflow", "framer")


def enabled() -> bool:
    """False until a connection-level browser egress boundary is implemented."""
    return False


def available() -> bool:
    """Never probe or launch Chromium in an environment with unbounded egress."""
    return False


def status() -> dict[str, Any]:
    return {"enabled": False, "available": False, "checked": True,
            "reason": _DISABLED_REASON, "renders": 0, "failures": 0, "ms": 0}


def looks_like_js_shell(raw: bytes | str, text_length: int) -> bool:
    """Static HTML with little readable text and JS / known SPA markers."""
    head = raw[:200_000].decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)[:200_000]
    lowered = head.casefold()
    scripts = lowered.count("<script")
    if text_length < 400 and scripts >= 1:
        return True
    return text_length < 1500 and scripts >= 3 and any(marker in lowered for marker in JS_SHELL_MARKERS)


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # Reject transition / scoped addresses rather than relying on differences in
    # Python's classification of mapped IPv4, 6to4 and Teredo addresses.
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id or address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return address.is_global and not address.is_multicast


def _url_target(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("ambiguous URL")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        raise ValueError("unsupported scheme or userinfo")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host or "%" in host:
        raise ValueError("missing or scoped/encoded hostname")
    port = parsed.port
    if port == 0:
        raise ValueError("invalid port")
    return host, port or (443 if parsed.scheme == "https" else 80)


def _private_host(url: str) -> bool:
    """Cheap lexical rejection only; False does NOT establish a public target."""
    try:
        host, _ = _url_target(url)
        if host in {"localhost", "local", "internal", "home", "lan"} or host.endswith((".localhost", ".internal", ".local", ".home", ".lan")):
            return True
        try:
            return not _public_address(host)
        except ValueError:
            # A single label can be resolved through local DNS search domains.
            return "." not in host
    except (ValueError, TypeError):
        return True


def _public_request(url: str) -> bool:
    """Check every returned DNS address without caching; not a connection guarantee."""
    if _private_host(url):
        return False
    try:
        host, port = _url_target(url)
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return bool(answers) and all(
            family in {socket.AF_INET, socket.AF_INET6} and _public_address(sockaddr[0])
            for family, _, _, _, sockaddr in answers
        )
    except (OSError, ValueError, TypeError, IndexError):
        return False


def _route(route: Any) -> None:
    """Defense-in-depth filter for navigation, redirects and subresources.

    Not used to enable rendering: Playwright interception is not an egress firewall.
    """
    request = route.request
    if request.resource_type in {"image", "media", "font"} or not _public_request(request.url):
        route.abort()
    else:
        route.continue_()


def render(url: str, *, timeout_s: float = 15.0, settle_ms: int = 1500) -> dict[str, Any] | None:
    """Return no browser evidence until safe browser networking is implemented.

    Deliberately do not import Playwright, resolve the URL, or start a browser.
    This independent guard cannot be enabled by stale availability state or env vars.
    """
    return None

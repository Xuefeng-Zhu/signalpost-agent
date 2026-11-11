"""Offline tests: no DNS queries, browser startup, or private network probes."""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent import js_render


def answer(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


class BrowserNetworkSecurityTests(unittest.TestCase):
    def test_lexical_rejection(self):
        urls = [
            "http://localhost./", "http://LOCALHOST.:8000/", "http://a.localhost./",
            "http://host.internal./", "http://host.local./", "http://intranet/",
            "http://127.0.0.1/", "http://[::1]/", "http://[fe80::1%25eth0]/",
            "http://[::ffff:127.0.0.1]/", "http://224.0.0.1/", "http://0.0.0.0/",
            "file:///etc/passwd", "data:text/html,hi", "blob:https://example.com/id",
            "ws://example.com/", "http://", "http://[bad]/", "http://example.com:bad/",
            "http://example.com:0/", "http://example.com:65536/",
            "http://user:secret@example.com/", "http://example.com\\@127.0.0.1/",
            "http://example.com\n/", "http://%6cocalhost/",
        ]
        with patch.object(socket, "getaddrinfo") as dns:
            for url in urls:
                with self.subTest(url=url):
                    self.assertFalse(js_render._public_request(url))
            dns.assert_not_called()

    def test_private_dns_and_alias_forms_are_rejected(self):
        for host in ("127.0.0.1.nip.io", "company.example", "127.1", "0177.0.0.1", "0x7f.0.0.1"):
            for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "::1", "fc00::1", "fe80::1"):
                with self.subTest(host=host, address=address), patch.object(socket, "getaddrinfo", return_value=[answer(address)]):
                    self.assertFalse(js_render._public_request(f"https://{host}/"))

    def test_all_dns_answers_must_be_public(self):
        public = answer("93.184.216.34")
        for answers in ([public, answer("::1")], [answer("10.0.0.1"), public], []):
            with patch.object(socket, "getaddrinfo", return_value=answers):
                self.assertFalse(js_render._public_request("https://company.example/"))
        with patch.object(socket, "getaddrinfo", return_value=[public, answer("2606:4700:4700::1111")]):
            self.assertTrue(js_render._public_request("https://company.example./"))

    def test_resolution_errors_fail_closed(self):
        for error in (socket.gaierror("missing"), TimeoutError("timeout")):
            with patch.object(socket, "getaddrinfo", side_effect=error):
                self.assertFalse(js_render._public_request("https://company.example/"))

    def test_every_routed_resource_and_redirect_is_checked(self):
        for resource in ("document", "script", "stylesheet", "xhr", "fetch", "other"):
            with self.subTest(resource=resource), patch.object(socket, "getaddrinfo", side_effect=[
                [answer("93.184.216.34")], [answer("10.0.0.1")]
            ]) as dns:
                route = Mock(request=SimpleNamespace(url="https://company.example/", resource_type=resource))
                js_render._route(route)
                route.continue_.assert_called_once_with()
                # Same name resolving differently on a redirected/subsequent request.
                js_render._route(route)
                route.abort.assert_called_once_with()
                self.assertEqual(dns.call_count, 2)

    def test_unneeded_resources_are_aborted_without_dns(self):
        with patch.object(socket, "getaddrinfo") as dns:
            for resource in ("image", "font", "media"):
                route = Mock(request=SimpleNamespace(url="https://company.example/", resource_type=resource))
                js_render._route(route)
                route.abort.assert_called_once_with()
                route.continue_.assert_not_called()
            dns.assert_not_called()

    def test_renderer_disabled_even_with_playwright_and_enable_flag(self):
        browser_api = Mock()
        with patch.dict("os.environ", {"SIGNALPOST_JS_FALLBACK": "1"}), patch.dict(sys.modules, {
            "playwright": Mock(), "playwright.sync_api": browser_api
        }), patch.object(socket, "getaddrinfo") as dns:
            self.assertFalse(js_render.enabled())
            self.assertFalse(js_render.available())
            # Stale/mocked availability cannot bypass the independent render guard.
            with patch.object(js_render, "available", return_value=True):
                for url in ("https://company.example/", "http://localhost./", "http://127.0.0.1.nip.io/"):
                    self.assertIsNone(js_render.render(url))
            browser_api.sync_playwright.assert_not_called()
            dns.assert_not_called()
            self.assertFalse(js_render.status()["available"])
            self.assertIn("egress boundary", js_render.status()["reason"])


if __name__ == "__main__":
    unittest.main()

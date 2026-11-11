#!/usr/bin/env python3
"""Run the unit suite using bundled suffix data and fail-closed network stubs.

Use ``uv run --offline --with pytest python scripts/run_offline_tests.py -q``
after dependencies are cached. Tests may replace these stubs with explicit mocks.
This is a test aid, not an operating-system sandbox or production egress firewall.
The existing subprocess fixtures only transform local JSON files.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest
import tldextract


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path[:0] = [str(root), str(root / "src"), str(root / "tests"), str(root / "scripts")]
    # Ambient credentials must not opt a deterministic test into a paid request.
    os.environ.pop("SIGNALPOST_LLM_API_KEY", None)
    tldextract.extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

    def blocked(*args, **kwargs):
        raise AssertionError("Real network disabled in offline tests; provide an explicit fixture")

    socket.getaddrinfo = blocked
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.socket.sendto = blocked
    return pytest.main(sys.argv[1:] or ["-q"])


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared fixtures: a real sandbox server and helpers for scripted-LLM runs."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("VAULT_ERP_USER", "ap.clerk")
os.environ.setdefault("VAULT_ERP_PASSWORD", "erp-demo-pass")
os.environ.setdefault("VAULT_MAIL_USER", "ap@northwind.test")
os.environ.setdefault("VAULT_MAIL_PASSWORD", "mail-demo-pass")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def sandbox_url():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT)
    url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            httpx.get(url + "/__admin__/state", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError("sandbox did not start")
    yield url
    proc.terminate()


@pytest.fixture
def world(sandbox_url):
    """Reset the sandbox with the given faults; returns a function."""
    def _reset(**faults):
        httpx.post(sandbox_url + "/__admin__/reset", json={"faults": faults}, timeout=5)
        return sandbox_url
    return _reset


def bills(url: str) -> list[dict]:
    return httpx.get(url + "/__admin__/state").json()["bills"]


def ref(view: str, pattern: str, kind: str = "") -> int:
    """Find the element number whose line matches `pattern` in the newest snapshot of the rendered view."""
    snap = view.split("ELEMENTS:")[-1]
    for line in snap.splitlines():
        m = re.match(r"\[(\d+)\] (\S+) \"(.*?)\"", line.strip())
        if m and re.search(pattern, line, re.I) and (not kind or kind in m.group(2)):
            return int(m.group(1))
    raise AssertionError(f"no element matching {pattern!r} in view:\n{view[-1500:]}")

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve_portal(drift: bool) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "DRIFT": "1" if drift else "0",
        "PORTAL_SECRET": "test-secret",
        "PYTHONPATH": str(ROOT),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "portal.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                return process, base
        except httpx.HTTPError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError("portal did not start")


@pytest.fixture(scope="session")
def portal():
    process, base = _serve_portal(drift=False)
    try:
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def drifted_portal():
    process, base = _serve_portal(drift=True)
    try:
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("VENDOR_PORTAL_USERNAME", "demo-user")
    monkeypatch.setenv("VENDOR_PORTAL_PASSWORD", "demo-pass")
    from app.governance import secrets as secrets_module

    secrets_module._secret.cache_clear()
    yield
    secrets_module._secret.cache_clear()


@pytest.fixture
def registry_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKWRIGHT_HOME", str(tmp_path))
    return tmp_path

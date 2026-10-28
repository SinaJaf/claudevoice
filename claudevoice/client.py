"""Thin stdlib client for the claudevoice server, shared by the hook and the chat loop.

Must stay stdlib-only: the Stop hook runs it with the system python so it starts
in a few milliseconds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _request(url: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def health(base: str | None = None) -> dict | None:
    base = base or config.server_url()
    try:
        return _request(f"{base}/health", timeout=1.5)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None


def _python_argv() -> list[str] | None:
    """The project's venv interpreter when it exists, else `uv run` (which creates it)."""
    for candidate in (PROJECT_DIR / ".venv" / "bin" / "python", PROJECT_DIR / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return [str(candidate)]
    uv = _find_uv()
    return [uv, "run", "--quiet", "--project", str(PROJECT_DIR), "python"] if uv else None


def ensure_server(base: str | None = None, *, startup_timeout: float = 300.0) -> bool:
    """Return True once the server answers /health, launching it if needed."""
    base = base or config.server_url()
    if health(base):
        return True
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    argv = _python_argv()
    if argv is None:
        print("claudevoice: uv not found (https://docs.astral.sh/uv/); start the server manually", file=sys.stderr)
        return False
    logf = open(config.SERVER_LOG_PATH, "ab")
    subprocess.Popen(
        [*argv, "-m", "claudevoice.tts_server"],
        cwd=str(PROJECT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        **config.background(),  # survives the hook process exiting, with no window
    )
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if health(base):
            return True
        time.sleep(0.5)
    return False


def _find_uv() -> str | None:
    from shutil import which

    found = which("uv")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local/bin/uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"),
                      home / ".cargo/bin/uv", home / ".local/bin/uv.exe"):
        if candidate.exists():
            return str(candidate)
    return None


def speak(text: str, *, voice: str | None = None, speed: float | None = None,
          interrupt: bool = True, wait: bool = False, raw: bool = False,
          max_chars: int | None = None, base: str | None = None) -> dict:
    base = base or config.server_url()
    body = {"text": text, "interrupt": interrupt, "wait": wait, "raw": raw}
    if voice:
        body["voice"] = voice
    if speed:
        body["speed"] = speed
    if max_chars:
        body["max_chars"] = max_chars
    return _request(f"{base}/speak", body, timeout=600 if wait else 10)


def stop(base: str | None = None) -> None:
    base = base or config.server_url()
    try:
        _request(f"{base}/stop", {}, timeout=2)
    except (urllib.error.URLError, OSError):
        pass


def wait_until_idle(base: str | None = None, poll: float = 0.1) -> None:
    base = base or config.server_url()
    while True:
        h = health(base)
        if not h or not h.get("speaking"):
            return
        time.sleep(poll)

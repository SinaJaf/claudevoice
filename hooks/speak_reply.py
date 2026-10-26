#!/usr/bin/env python3
"""Claude Code Stop hook: read the reply aloud with Kokoro.

Runs with the system python (stdlib only) so it adds no noticeable delay.
Claude Code pipes the hook payload on stdin; `last_assistant_message` holds
the final reply text. Speaking happens on the server, so this script returns
as soon as the text is queued and never blocks Claude Code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudevoice import client, config  # noqa: E402


def main() -> int:
    if not config.is_enabled():
        return 0
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    text = (payload.get("last_assistant_message") or "").strip()
    if not text:
        return 0
    cfg = config.load()
    if not client.ensure_server():
        _log("server did not come up; see " + str(config.SERVER_LOG_PATH))
        return 0
    try:
        client.speak(text, interrupt=True, max_chars=int(cfg["max_chars"]))
    except Exception as exc:  # noqa: BLE001 - never fail the session over audio
        _log(f"speak failed: {exc}")
    return 0


def _log(msg: str) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_PATH, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())

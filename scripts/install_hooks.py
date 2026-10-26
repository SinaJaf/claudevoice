"""Add (or refresh) the claudevoice hooks in ~/.claude/settings.json without
disturbing anything else in the file. Safe to run repeatedly."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SETTINGS = Path(os.environ.get("CLAUDE_SETTINGS", Path.home() / ".claude" / "settings.json"))
PY = str(HERE / ".venv" / "bin" / "python3") if (HERE / ".venv" / "bin" / "python3").exists() else sys.executable
MARK = "claudevoice"

HOOKS = {
    "Stop": {"type": "command", "command": f"{PY} {HERE}/hooks/speak_reply.py", "timeout": 120},
    "UserPromptSubmit": {"type": "command", "command": f"{PY} {HERE}/hooks/stop_speaking.py", "timeout": 10},
}


def main() -> int:
    settings = {}
    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(".json.claudevoice-backup")
        shutil.copy(SETTINGS, backup)
        settings = json.loads(SETTINGS.read_text() or "{}")
    hooks = settings.setdefault("hooks", {})
    for event, handler in HOOKS.items():
        groups = hooks.setdefault(event, [])
        # Drop any earlier claudevoice entry, then add the current one.
        groups[:] = [g for g in groups if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        groups.append({"hooks": [handler]})
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"  hooks written to {SETTINGS} (backup kept alongside)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: stop any speech still playing when you send
a new prompt, and remind Claude to answer in a spoken style while voice is on."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudevoice import client, config  # noqa: E402

SPOKEN_STYLE = (
    "Voice mode is on: your final reply will be read aloud by a text-to-speech "
    "engine. Write it the way you would say it out loud: short sentences, "
    "a warm conversational tone, no markdown, no bullet lists, no headers, no "
    "code blocks unless the user asked for code, and spell out file names "
    "plainly. Keep spoken replies under about 150 words unless more is needed."
)


def main() -> int:
    if not config.is_enabled():
        return 0
    if client.health():
        client.stop()
    # Stdout of a UserPromptSubmit hook is added to Claude's context.
    print(SPOKEN_STYLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

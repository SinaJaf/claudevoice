"""`claudevoice` command-line entry point."""
from __future__ import annotations

import json
import sys

from . import client, config

USAGE = """claudevoice <command>

  chat [opts] [-- claude args]   local speech-to-speech loop (Whisper -> claude -p -> Kokoro)
  server                         run the Kokoro TTS server in the foreground
  on | off | status              always-on mode: every Claude Code reply is read aloud
  start                          make sure the voice server is running (no always-on)
  stop-server                    stop the voice server (it restarts on the next use)
  say "text" [--voice V]         speak text through the server (starts it if needed)
  stop                           cut off whatever is playing
  voices                         list the English Kokoro voices
  config [key value]             show or set a setting in ~/.claudevoice/config.json
"""

VOICES = {
    "American female": ["af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore", "af_sarah", "af_nova", "af_sky", "af_alloy", "af_jessica", "af_river"],
    "American male": ["am_michael", "am_fenrir", "am_puck", "am_echo", "am_eric", "am_liam", "am_onyx", "am_adam", "am_santa"],
    "British female": ["bf_emma", "bf_isabella", "bf_alice", "bf_lily"],
    "British male": ["bm_george", "bm_fable", "bm_lewis", "bm_daniel"],
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "chat":
        from .chat import main as chat_main

        return chat_main(rest)
    if cmd == "server":
        from .tts_server import main as server_main

        return server_main(rest)
    if cmd == "stop-server":
        try:
            client._request(f"{config.server_url()}/shutdown", {}, timeout=3)
            print("server stopped")
        except Exception:  # noqa: BLE001
            print("server was not running")
        return 0
    if cmd == "start":
        ok = client.ensure_server()
        print("server running" if ok else "server failed to start; see ~/.claudevoice/server.log")
        return 0 if ok else 1
    if cmd == "on":
        config.set_enabled(True)
        ok = client.ensure_server()
        print("voice replies: on" + ("" if ok else " (server failed to start; see ~/.claudevoice/server.log)"))
        return 0 if ok else 1
    if cmd == "off":
        config.set_enabled(False)
        client.stop()
        print("voice replies: off")
        return 0
    if cmd == "status":
        h = client.health()
        print(f"replies read aloud: {'on' if config.is_enabled() else 'off'}")
        print(f"server: {'running, voice ' + h['voice'] if h else 'not running'}")
        return 0
    if cmd == "say":
        voice = None
        if "--voice" in rest:
            i = rest.index("--voice")
            voice = rest[i + 1]
            rest = rest[:i] + rest[i + 2 :]
        text = " ".join(rest) or sys.stdin.read()
        if not client.ensure_server():
            print("server failed to start", file=sys.stderr)
            return 1
        client.speak(text, voice=voice, wait=True)
        return 0
    if cmd == "stop":
        client.stop()
        return 0
    if cmd == "voices":
        for group, names in VOICES.items():
            print(f"{group}: {', '.join(names)}")
        print("\nBlend voices with commas, e.g. --voice af_heart,af_bella  (full list: VOICES.md on the model page)")
        return 0
    if cmd == "config":
        cfg = config.load()
        if len(rest) == 2:
            key, value = rest
            if key not in config.DEFAULTS:
                print(f"unknown key {key}; known: {', '.join(config.DEFAULTS)}", file=sys.stderr)
                return 2
            cfg[key] = type(config.DEFAULTS[key])(value)
            config.save(cfg)
            print(f"{key} = {cfg[key]!r}  (restart the server for voice/lang/port changes)")
        else:
            print(json.dumps(cfg, indent=2))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

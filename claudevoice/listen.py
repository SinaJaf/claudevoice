"""One-shot listener for the Claude Code voice panel.

Records from the microphone until the speaker pauses, transcribes with local
Whisper, and prints one JSON line: {"text": "...", "seconds": 3.2}.
The panel runs this as a subprocess for each turn of a call.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from . import config
from .record import record
from .stt import make_transcriber


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", default=None, help="override the local Whisper model")
    ap.add_argument("--max-seconds", type=float, default=cfg["max_record_seconds"])
    ap.add_argument("--warm", action="store_true", help="just load Whisper and exit")
    ap.add_argument("--manual", action="store_true",
                    help="hold mode: keep recording until the done flag appears, ignore pauses")
    args = ap.parse_args(argv)

    stt = make_transcriber(cfg, args.whisper)
    if args.warm:
        stt.warm()
        print(json.dumps({"ok": True}))
        return 0
    t0 = time.time()
    stop_event = threading.Event()
    config.LISTEN_STOP_FLAG.unlink(missing_ok=True)
    config.LISTEN_DONE_FLAG.unlink(missing_ok=True)

    def watch_flags() -> None:
        while not stop_event.is_set():
            if config.LISTEN_STOP_FLAG.exists() or config.LISTEN_DONE_FLAG.exists():
                stop_event.set()
            time.sleep(0.1)

    threading.Thread(target=watch_flags, daemon=True).start()
    # Hold mode: pauses never end the recording; only the Done button does (or the cap).
    silence = 1e9 if args.manual else cfg["silence_seconds"]
    max_seconds = min(args.max_seconds, 570.0) if args.manual else args.max_seconds
    audio = record(silence_seconds=silence, threshold=cfg["vad_threshold"],
                   max_seconds=max_seconds, stop_event=stop_event)
    cancelled = config.LISTEN_STOP_FLAG.exists()
    stop_event.set()
    config.LISTEN_STOP_FLAG.unlink(missing_ok=True)
    config.LISTEN_DONE_FLAG.unlink(missing_ok=True)
    if cancelled:
        print(json.dumps({"text": "", "seconds": 0, "cancelled": True}))
        return 0
    seconds = round(time.time() - t0, 1)
    text = stt.transcribe(audio) if len(audio) else ""
    print(json.dumps({"text": text, "seconds": seconds}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

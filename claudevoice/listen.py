"""One-shot listener for the Claude Code voice panel.

Records from the microphone until the speaker pauses, transcribes with local
Whisper, and prints one JSON line: {"text": "...", "seconds": 3.2}.
The panel runs this as a subprocess for each turn of a call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

from . import config
from .record import device_name, input_devices, record, resolve_device
from .stt import make_transcriber


def _take_lock():
    """Hold an exclusive lock for this recording; None if another listener has it.

    Two listeners on one microphone would transcribe the same words twice and
    send them twice. The OS drops the lock when the process ends, even on a crash.
    """
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(config.LISTEN_LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", default=None, help="override the local Whisper model")
    ap.add_argument("--max-seconds", type=float, default=cfg["max_record_seconds"])
    ap.add_argument("--warm", action="store_true", help="just load Whisper and exit")
    ap.add_argument("--manual", action="store_true",
                    help="hold mode: keep recording until the done flag appears, ignore pauses")
    ap.add_argument("--devices", action="store_true", help="list microphones as JSON and exit")
    args = ap.parse_args(argv)

    if args.devices:
        print(json.dumps(input_devices()))
        return 0

    if not args.warm:
        lock = _take_lock()
        if lock is None:
            # Another listener is already recording and will send what was said.
            # Wait for it to finish before answering, so a caller that simply
            # listens again never spins in a tight loop.
            while lock is None:
                time.sleep(0.25)
                lock = _take_lock()
            lock.close()
            print(json.dumps({"text": "", "seconds": 0, "busy": True}))
            return 0
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
    device = resolve_device(cfg.get("input_device", ""))
    audio = record(silence_seconds=silence, threshold=cfg["vad_threshold"],
                   max_seconds=max_seconds, stop_event=stop_event, device=device)
    cancelled = config.LISTEN_STOP_FLAG.exists()
    stop_event.set()
    config.LISTEN_STOP_FLAG.unlink(missing_ok=True)
    config.LISTEN_DONE_FLAG.unlink(missing_ok=True)
    if cancelled:
        print(json.dumps({"text": "", "seconds": 0, "cancelled": True}))
        return 0
    seconds = round(time.time() - t0, 1)
    mic = device_name(device)
    # A stream of exact zeros is a dead input (unplugged, lid closed, permission denied).
    if len(audio) >= 1600 and float(np.abs(audio).max()) == 0.0:
        print(json.dumps({"text": "", "seconds": seconds, "silent": True, "device": mic}))
        return 0
    text = stt.transcribe(audio) if len(audio) else ""
    print(json.dumps({"text": text, "seconds": seconds, "device": mic}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

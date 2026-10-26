"""One-shot listener for the Claude Code voice panel.

Records from the microphone until the speaker pauses (or, in hold mode, until
the Done flag appears), transcribes, and prints one JSON line:
{"text": "...", "seconds": 3.2}. With --result PATH the same JSON is also
written to PATH. With --detach the listener runs in its own background process
and returns at once, so a hold-mode take is not bound by the caller's command
timeout; the plugin then watches PATH for the result.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

from . import config
from .record import input_devices, record
from .stt import make_transcriber


HOLD_MAX_SECONDS = 3600.0  # one hour: a runaway guard; only Done ends a hold-mode take
RESULT_MAX_AGE = 24 * 3600  # result files older than this are swept


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


def _detach(argv: list[str]) -> None:
    """Run this listener again, minus --detach, in its own background process."""
    child = [sys.executable, "-m", "claudevoice.listen", *[a for a in argv if a != "--detach"]]
    flags = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.CONFIG_DIR / "listen.log", "ab") as log:
        subprocess.Popen(child, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, **flags)


def _emit(result: dict, path: str | None) -> None:
    line = json.dumps(result)
    print(line, flush=True)
    if path:
        target = os.path.expanduser(path)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(line)
        os.replace(tmp, target)  # the watcher never sees a half-written file


def _sweep_results() -> None:
    now = time.time()
    for old in config.CONFIG_DIR.glob("listen-*.json"):
        try:
            if now - old.stat().st_mtime > RESULT_MAX_AGE:
                old.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--detach" in argv:
        _detach(argv)
        print(json.dumps({"started": True}))
        return 0
    result_path = None
    if "--result" in argv:
        i = argv.index("--result")
        result_path = argv[i + 1] if i + 1 < len(argv) else None
    try:
        return _listen(argv, result_path)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - always answer, or the watcher waits forever
        _emit({"text": "", "seconds": 0, "error": f"{type(exc).__name__}: {exc}"}, result_path)
        return 1


def _listen(argv: list[str], result_path: str | None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", default=None, help="override the local Whisper model")
    ap.add_argument("--max-seconds", type=float, default=cfg["max_record_seconds"])
    ap.add_argument("--warm", action="store_true", help="just load Whisper and exit")
    ap.add_argument("--manual", action="store_true",
                    help="hold mode: keep recording until the done flag appears, ignore pauses")
    ap.add_argument("--devices", action="store_true", help="list microphones as JSON and exit")
    # Per-chat overrides from the plugin; absent means the config file's value.
    ap.add_argument("--stt", choices=["local", "openai"], default=None, help="speech-to-text engine")
    ap.add_argument("--stt-model", default=None, help="cloud transcription model")
    ap.add_argument("--device", default=None, help="microphone name; empty for the system default")
    ap.add_argument("--result", default=None, help="also write the JSON answer to this file")
    args = ap.parse_args(argv)
    if args.stt:
        cfg["stt_engine"] = args.stt
    if args.stt_model:
        cfg["stt_model"] = args.stt_model
    if args.device is not None:
        cfg["input_device"] = args.device

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
            _emit({"text": "", "seconds": 0, "busy": True}, result_path)
            return 0
    stt = make_transcriber(cfg, args.whisper)
    if args.warm:
        stt.warm()
        print(json.dumps({"ok": True}))
        return 0
    _sweep_results()
    t0 = time.time()
    stop_event = threading.Event()
    config.LISTEN_STOP_FLAG.unlink(missing_ok=True)
    config.LISTEN_DONE_FLAG.unlink(missing_ok=True)

    muted = threading.Event()

    def watch_flags() -> None:
        while not stop_event.is_set():
            if config.LISTEN_STOP_FLAG.exists() or config.LISTEN_DONE_FLAG.exists():
                stop_event.set()
            try:
                is_muted = config.LISTEN_MUTE_FLAG.read_text().strip() == "1"
            except OSError:
                is_muted = False
            if is_muted:
                muted.set()
            else:
                muted.clear()
            time.sleep(0.1)

    threading.Thread(target=watch_flags, daemon=True).start()
    # Hold mode: pauses never end the recording; only the Done button does (or the cap).
    silence = 1e9 if args.manual else cfg["silence_seconds"]
    # Hold mode ignores the auto-mode cap (max_record_seconds): only Done ends the take.
    max_seconds = HOLD_MAX_SECONDS if args.manual else args.max_seconds
    used: dict = {}
    audio = record(silence_seconds=silence, threshold=cfg["vad_threshold"],
                   max_seconds=max_seconds, stop_event=stop_event,
                   device=cfg.get("input_device", ""), info=used, paused=muted.is_set)
    cancelled = config.LISTEN_STOP_FLAG.exists()
    stop_event.set()
    config.LISTEN_STOP_FLAG.unlink(missing_ok=True)
    config.LISTEN_DONE_FLAG.unlink(missing_ok=True)
    if cancelled:
        _emit({"text": "", "seconds": 0, "cancelled": True}, result_path)
        return 0
    seconds = round(time.time() - t0, 1)
    mic = used.get("device", "")
    # A stream of exact zeros is a dead input (unplugged, lid closed, permission denied).
    if len(audio) >= 1600 and float(np.abs(audio).max()) == 0.0:
        _emit({"text": "", "seconds": seconds, "silent": True, "device": mic}, result_path)
        return 0
    text = stt.transcribe(audio) if len(audio) else ""
    _emit({"text": text, "seconds": seconds, "device": mic}, result_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

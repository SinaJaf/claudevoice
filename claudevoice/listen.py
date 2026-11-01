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
import shutil
import signal
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
CHAT_MAX_AGE = 14 * 24 * 3600  # a chat's folder untouched for this long is swept
HEARTBEAT_STALE = 20.0  # no heartbeat from the plugin for this long: nobody is waiting, stop
_preempted = threading.Event()  # a newer listener in this chat took over, or the plugin went away


def _try_lock(files: config.ListenFiles):
    files.dir.mkdir(parents=True, exist_ok=True)
    handle = open(files.lock, "a+")
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


def _take_lock(files: config.ListenFiles, timeout: float = 10.0):
    """Hold this chat's recording, taking it over from any older listener in the same chat.

    One listener at a time in a chat, so the same words are never sent twice. The newest
    one wins: an older listener (a reloaded plugin) is told to stop and gives up its take.
    Other chats have listeners of their own, muted while you are not in them. The OS drops
    the lock when a process ends.
    """
    handle = _try_lock(files)
    if handle is None:
        try:
            old = int(files.pid.read_text().strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid():
            try:
                if os.name == "nt":
                    # /T: its PowerShell recognizer goes too
                    subprocess.run(["taskkill", "/PID", str(old), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    os.kill(old, signal.SIGTERM)
            except (OSError, subprocess.SubprocessError):
                pass
        deadline = time.time() + timeout
        while handle is None and time.time() < deadline:
            time.sleep(0.1)
            handle = _try_lock(files)
    if handle is not None:
        files.pid.write_text(str(os.getpid()))
    return handle


def _detach(argv: list[str]) -> None:
    """Run this listener again, minus --detach, in its own background process."""
    child = [sys.executable, "-m", "claudevoice.listen", *[a for a in argv if a != "--detach"]]
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.CONFIG_DIR / "listen.log", "ab") as log:
        subprocess.Popen(child, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, **config.background())


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
    if config.CHATS_DIR.is_dir():
        for chat in config.CHATS_DIR.iterdir():
            try:
                if now - chat.stat().st_mtime > CHAT_MAX_AGE:
                    shutil.rmtree(chat, ignore_errors=True)
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
    ap.add_argument("--stt", choices=["auto", "parakeet", "apple", "windows", "local", "openai"], default=None, help="speech-to-text engine")
    ap.add_argument("--stt-model", default=None, help="cloud transcription model")
    ap.add_argument("--parakeet", default=None, choices=["v2", "v3"], help="Parakeet model")
    ap.add_argument("--device", default=None, help="microphone name; empty for the system default")
    ap.add_argument("--result", default=None, help="also write the JSON answer to this file")
    ap.add_argument("--chat", default="", help="the chat this listener belongs to; its flags are its own")
    ap.add_argument("--live", action="store_true",
                    help="live mode: end the turn when you have finished talking (Smart Turn)")
    ap.add_argument("--patience", choices=["short", "normal", "long"] + [f"long{n}" for n in range(2, 11)], default="normal",
                    help="live mode: how long a pause runs before your turn can end")
    ap.add_argument("--talk-over", dest="talk_over", action="store_true", default=True,
                    help="live mode: talking while Claude speaks stops its voice (default)")
    ap.add_argument("--no-talk-over", dest="talk_over", action="store_false")
    ap.add_argument("--heartbeat", default=None,
                    help="a file the caller keeps touching while it waits; stale means give up")
    args = ap.parse_args(argv)
    if args.stt:
        cfg["stt_engine"] = args.stt
    if args.stt_model:
        cfg["stt_model"] = args.stt_model
    if args.parakeet:
        cfg["parakeet_model"] = args.parakeet
    if args.device is not None:
        cfg["input_device"] = args.device

    if args.devices:
        print(json.dumps(input_devices()))
        return 0

    files = config.ListenFiles(args.chat)
    if not args.warm:
        # SIGTERM from a newer listener: give up this take quietly.
        if os.name != "nt":
            signal.signal(signal.SIGTERM, lambda *_: _preempted.set())
        lock = _take_lock(files)
        if lock is None:
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
    files.stop.unlink(missing_ok=True)
    files.done.unlink(missing_ok=True)

    muted = threading.Event()

    heartbeat = os.path.expanduser(args.heartbeat) if args.heartbeat else None

    def orphaned() -> bool:
        if not heartbeat:
            return False
        try:
            return time.time() - os.path.getmtime(heartbeat) > HEARTBEAT_STALE
        except OSError:
            return True

    def watch_flags() -> None:
        while not stop_event.is_set():
            if _preempted.is_set() or orphaned():
                _preempted.set()
                stop_event.set()
            if files.stop.exists() or files.done.exists():
                stop_event.set()
            try:
                is_muted = files.mute.read_text().strip() == "1"
            except OSError:
                is_muted = False
            if is_muted:
                muted.set()
            else:
                muted.clear()
            time.sleep(0.1)

    if getattr(stt, "live", False):
        # Windows: the system recognizer listens to the mic itself and watches the same flags.
        result = stt.listen(done_flag=files.done, stop_flag=files.stop,
                            mute_flag=files.mute, heartbeat=heartbeat,
                            max_seconds=HOLD_MAX_SECONDS if args.manual else args.max_seconds)
        files.stop.unlink(missing_ok=True)
        files.done.unlink(missing_ok=True)
        result.setdefault("text", "")
        result["seconds"] = round(time.time() - t0, 1)
        result["device"] = "default microphone"
        _emit(result, result_path)
        return 0

    threading.Thread(target=watch_flags, daemon=True).start()

    if args.live:
        return _live(args, cfg, stt, stop_event, muted, t0, result_path, files)

    # Hold mode: pauses never end the recording; only the Done button does (or the cap).
    silence = 1e9 if args.manual else cfg["silence_seconds"]
    # Hold mode ignores the auto-mode cap (max_record_seconds): only Done ends the take.
    max_seconds = HOLD_MAX_SECONDS if args.manual else args.max_seconds
    used: dict = {}
    audio = record(silence_seconds=silence, threshold=cfg["vad_threshold"],
                   max_seconds=max_seconds, stop_event=stop_event,
                   device=cfg.get("input_device", ""), info=used, paused=muted.is_set)
    cancelled = files.stop.exists() or _preempted.is_set()
    stop_event.set()
    files.stop.unlink(missing_ok=True)
    files.done.unlink(missing_ok=True)
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


def _live(args, cfg: dict, stt, stop_event: threading.Event, muted: threading.Event, t0: float,
          result_path: str | None, files: config.ListenFiles) -> int:
    """Live mode: one conversational turn, ended by Smart Turn, a long pause, or Done."""
    from .live import live_turn

    def on_state(state: str) -> None:
        try:
            files.state.write_text(state)
        except OSError:
            pass

    # Load the transcriber while you talk, so your words are ready the moment you finish.
    threading.Thread(target=stt.warm, daemon=True).start()
    turn = live_turn(stop_event=stop_event, paused=muted.is_set, device=cfg.get("input_device", ""),
                     patience=args.patience, talk_over=args.talk_over, server=config.server_url(cfg),
                     max_seconds=HOLD_MAX_SECONDS, on_state=on_state,
                     settings_file=str(files.settings), owner=args.chat)
    cancelled = files.stop.exists() or _preempted.is_set()
    stop_event.set()
    files.stop.unlink(missing_ok=True)
    files.done.unlink(missing_ok=True)
    if cancelled:
        on_state("")
        _emit({"text": "", "seconds": 0, "cancelled": True}, result_path)
        return 0
    seconds = round(time.time() - t0, 1)
    info = {"seconds": seconds, "device": turn.device, "ended_by": turn.reason, "patience": turn.patience,
            "longest_pause": turn.longest_pause, "talked_over": turn.talked_over, "smart_turn": turn.smart_turn}
    if turn.reason == "silent":
        on_state("")
        _emit({"text": "", "silent": True, **info}, result_path)
        return 0
    on_state("transcribing")
    text = stt.transcribe(turn.audio) if len(turn.audio) else ""
    on_state("")
    _emit({"text": text, **info}, result_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

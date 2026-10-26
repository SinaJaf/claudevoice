"""Fully local speech-to-speech loop around `claude -p`.

    you speak -> Whisper (local) -> claude -p --resume ... -> Kokoro (local) -> speakers

Press Enter to start talking; recording stops on its own when you pause, or
press Enter again. Press Enter while Claude is speaking to cut it off and talk.
Type a line instead of pressing Enter to send text. `q` quits.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time

try:
    import termios
    import tty
except ImportError:  # Windows: the terminal chat loop is POSIX-only
    termios = tty = None  # type: ignore[assignment]

from . import client, config
from .clean import StreamingSentenceSplitter, markdown_to_speech
from .record import record
from .stt import make_transcriber

SPOKEN_STYLE = (
    "You are having a spoken conversation: the user talks to you through a "
    "microphone and your reply is read aloud by a text-to-speech engine. "
    "Answer the way a thoughtful colleague would speak: natural, warm, direct, "
    "with short sentences and a little personality. No markdown, no bullet "
    "lists, no headers, no tables. Do not read out code unless asked; describe "
    "what it does instead. Say file names plainly. Keep most replies under "
    "120 words, and ask a short follow-up question when you need a decision."
)

RESET, DIM, BOLD, GREEN, CYAN, YELLOW, RED = "\033[0m", "\033[2m", "\033[1m", "\033[32m", "\033[36m", "\033[33m", "\033[31m"


class KeyReader:
    """Line/keypress reader on a raw tty, usable from a background thread."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self.fd)

    def __enter__(self):
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def poll(self, timeout: float = 0.05) -> str | None:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, 1).decode(errors="ignore")

    def read_line(self, first: str) -> str:
        """Collect a typed line after the first non-Enter key, echoing it."""
        buf = first
        sys.stdout.write(first)
        sys.stdout.flush()
        while True:
            ch = self.poll(1.0)
            if ch is None:
                continue
            if ch in ("\n", "\r"):
                sys.stdout.write("\n")
                return buf
            if ch in ("\x7f", "\b"):
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
            elif ch == "\x03":
                raise KeyboardInterrupt
            else:
                buf += ch
                sys.stdout.write(ch)
            sys.stdout.flush()


class ClaudeSession:
    """Drives `claude -p` with streaming JSON and keeps the session id between turns."""

    def __init__(self, *, resume: str | None, continue_last: bool, extra_args: list[str],
                 permission_mode: str | None, style: str, cwd: str) -> None:
        self.session_id = resume
        self.continue_last = continue_last
        self.extra_args = extra_args
        self.permission_mode = permission_mode
        self.style = style
        self.cwd = cwd
        self.claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

    def _argv(self, prompt: str) -> list[str]:
        argv = [self.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--append-system-prompt", self.style]
        if self.session_id:
            argv += ["--resume", self.session_id]
        elif self.continue_last:
            argv += ["--continue"]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        argv += self.extra_args
        return argv

    def ask(self, prompt: str, *, on_text, on_tool, cancel: threading.Event) -> str:
        proc = subprocess.Popen(
            self._argv(prompt), cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        final = ""
        try:
            for line in proc.stdout:
                if cancel.is_set():
                    proc.terminate()
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "stream_event" and ev.get("parent_tool_use_id") is None:
                    delta = (ev.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        on_text(delta.get("text", ""))
                    block = (ev.get("event") or {}).get("content_block") or {}
                    if block.get("type") == "tool_use":
                        on_tool(block.get("name", "tool"))
                elif kind == "result":
                    self.session_id = ev.get("session_id") or self.session_id
                    self.continue_last = False
                    final = ev.get("result") or ""
                    if ev.get("is_error"):
                        raise RuntimeError(final or "claude returned an error")
        finally:
            if proc.poll() is None:
                proc.terminate()
            err = proc.stderr.read() if proc.stderr else ""
            if proc.returncode not in (0, None, -15) and err:
                raise RuntimeError(err.strip()[-800:])
        return final


def status(msg: str, color: str = DIM) -> None:
    sys.stdout.write(f"\r\033[K{color}{msg}{RESET}")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(prog="claudevoice chat", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", "-r", help="resume a Claude Code session id")
    ap.add_argument("--continue", "-c", dest="continue_last", action="store_true",
                    help="continue the most recent conversation in this directory")
    ap.add_argument("--permission-mode", default=None,
                    help="passed to claude -p (default, acceptEdits, plan, auto, ...)")
    ap.add_argument("--voice", default=cfg["voice"], help="Kokoro voice or blend, e.g. af_heart,am_michael")
    ap.add_argument("--speed", type=float, default=cfg["speed"])
    ap.add_argument("--whisper", default=cfg["whisper_model"], help="mlx-whisper model repo")
    ap.add_argument("--no-style", action="store_true", help="don't ask Claude to answer in spoken style")
    ap.add_argument("--cwd", default=os.getcwd(), help="project directory for Claude")
    ap.add_argument("claude_args", nargs="*", help="extra args after -- are passed to claude")
    args = ap.parse_args(argv)

    if not sys.stdin.isatty():
        print("claudevoice chat needs an interactive terminal", file=sys.stderr)
        return 2
    if termios is None:
        print("claudevoice chat runs on macOS and Linux terminals; on Windows use the voice controls in Claude Code", file=sys.stderr)
        return 2

    status("starting Kokoro server…")
    if not client.ensure_server():
        print(f"\n{RED}could not start the TTS server; see {config.SERVER_LOG_PATH}{RESET}")
        return 1
    status(f"loading Whisper ({args.whisper})…")
    stt = make_transcriber(cfg, args.whisper)
    stt.warm()
    status("")

    session = ClaudeSession(resume=args.resume, continue_last=args.continue_last,
                            extra_args=args.claude_args, permission_mode=args.permission_mode,
                            style="" if args.no_style else SPOKEN_STYLE, cwd=args.cwd)
    print(f"{BOLD}claudevoice{RESET} {DIM}voice={args.voice} whisper={args.whisper} cwd={args.cwd}{RESET}")
    print(f"{DIM}Enter: talk · Enter again: stop early · type text + Enter: send text · q: quit{RESET}\n")

    with KeyReader() as keys:
        while True:
            status(f"{GREEN}● press Enter to talk{RESET}")
            ch = keys.poll(0.1)
            if ch is None:
                continue
            if ch in ("\x03", "\x04") or ch.lower() == "q":
                print()
                break
            if ch in ("\n", "\r"):
                prompt = _listen(keys, stt, cfg)
            else:
                status("")
                prompt = keys.read_line(ch).strip()
            if not prompt:
                continue
            print(f"{CYAN}you:{RESET} {prompt}")
            _answer(session, prompt, keys, args)
    return 0


def _listen(keys: KeyReader, stt, cfg: dict) -> str:
    client.stop()
    stop_event = threading.Event()

    def watch_keys():
        while not stop_event.is_set():
            ch = keys.poll(0.05)
            if ch in ("\n", "\r", "\x03"):
                stop_event.set()

    t = threading.Thread(target=watch_keys, daemon=True)
    t.start()

    def on_level(level: float, heard: bool):
        bars = "▁▂▃▄▅▆▇█"
        idx = min(len(bars) - 1, int(level / max(cfg["vad_threshold"], 1e-6) * 2))
        status(f"{RED}● listening{RESET} {bars[idx]} {DIM}{'(speak…)' if not heard else '(pause to send)'}{RESET}")

    audio = record(silence_seconds=cfg["silence_seconds"], threshold=cfg["vad_threshold"],
                   max_seconds=cfg["max_record_seconds"], stop_event=stop_event, on_level=on_level)
    stop_event.set()
    t.join(timeout=0.5)
    status(f"{YELLOW}transcribing…{RESET}")
    text = stt.transcribe(audio)
    status("")
    return text


def _answer(session: ClaudeSession, prompt: str, keys: KeyReader, args) -> None:
    splitter = StreamingSentenceSplitter(min_chars=30)
    cancel = threading.Event()
    spoken_any = False
    printed_label = False

    def say(sentence: str) -> None:
        nonlocal spoken_any
        text = markdown_to_speech(sentence)
        if text:
            client.speak(text, voice=args.voice, speed=args.speed, interrupt=not spoken_any, raw=True)
            spoken_any = True

    def on_text(delta: str) -> None:
        nonlocal printed_label
        if not printed_label:
            status("")
            sys.stdout.write(f"{BOLD}claude:{RESET} ")
            printed_label = True
        sys.stdout.write(delta)
        sys.stdout.flush()
        for sentence in splitter.feed(delta):
            say(sentence)

    def on_tool(name: str) -> None:
        status(f"{DIM}… using {name}{RESET}")

    def watch_interrupt():
        while not cancel.is_set():
            ch = keys.poll(0.1)
            if ch in ("\n", "\r", "\x03"):
                cancel.set()
                client.stop()

    watcher = threading.Thread(target=watch_interrupt, daemon=True)
    watcher.start()
    status(f"{DIM}thinking…{RESET}")
    try:
        session.ask(prompt, on_text=on_text, on_tool=on_tool, cancel=cancel)
        for sentence in splitter.flush():
            say(sentence)
        # Let the user barge in while the tail of the reply plays.
        while not cancel.is_set():
            h = client.health()
            if not h or not h.get("speaking"):
                break
            time.sleep(0.1)
    except RuntimeError as exc:
        print(f"\n{RED}claude error:{RESET} {exc}")
    finally:
        cancel.set()
        watcher.join(timeout=0.5)
    print("\n")


if __name__ == "__main__":
    sys.exit(main())

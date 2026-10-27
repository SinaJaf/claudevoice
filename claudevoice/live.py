"""One live turn: listen until you have finished talking, like a phone conversation.

Silero VAD hears speech start and stop; at each pause Smart Turn judges from how you
sound whether you have finished or are only thinking. Your turn ends when it says you
have finished (after a short pause), or when a pause runs long, or when you press
Done. While Claude is speaking, talking over it stops its voice at once (talk-over).
"""
from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass, field

import numpy as np

from .record import _Mic
from .turn import RATE, VAD_CHUNK, SileroVAD, SmartTurn

log = logging.getLogger("claudevoice.live")

# Smart Turn judges right after you stop (it is trained on ~0.2 s of trailing silence;
# a longer tail makes an unfinished sentence look finished).
ASK_AFTER = 0.2
# Then, by patience: how long a pause after a finished-sounding sentence runs before your
# turn ends, and how long an unfinished-sounding pause may run before it ends anyway.
PATIENCE = {
    "short": (0.3, 3.0),
    "normal": (0.6, 5.0),
    "long": (1.2, 9.0),
    # For long thinking pauses, after finished sentences too: Long two to four times
    # over mid-thought, and nearly as long after a finished sentence. Done answers at once.
    "long2": (12.0, 18.0),
    "long3": (20.0, 27.0),
    "long4": (30.0, 36.0),
    # Very long thinking: Claude waits this long after you go quiet, finished or not.
    "long5": (60.0, 60.0),
    "long6": (90.0, 90.0),
    "long7": (120.0, 120.0),
    "long8": (180.0, 180.0),
    "long9": (240.0, 240.0),
    "long10": (300.0, 300.0),
}
FINISHED = 0.5  # Smart Turn probability that counts as "finished"
SPEECH_ON = 0.5  # VAD probability that counts as speech
SPEECH_OFF = 0.35  # below this, a chunk is silence (hysteresis)
START_CHUNKS = 3  # ~100 ms of speech starts your turn
# Talking over Claude's voice needs to be clearer and longer, so its own voice leaking
# from speakers into the mic does not interrupt it.
TALK_OVER_PROB = 0.85
TALK_OVER_CHUNKS = 9  # ~290 ms
PRE_ROLL = 0.4  # seconds kept from before speech was detected
CHUNK_SECONDS = VAD_CHUNK / RATE
LAST_8S = int(8 / CHUNK_SECONDS) + 1


@dataclass
class TurnResult:
    audio: np.ndarray
    reason: str  # finished | long_pause | done | cap | silent
    talked_over: bool = False
    device: str = ""
    smart_turn: list[float] = field(default_factory=list)
    patience: str = ""
    longest_pause: float = 0.0


class _ClaudeVoice:
    """Watches whether Claude's voice is playing, and can stop it."""

    def __init__(self, server: str) -> None:
        self.server = server.rstrip("/")
        self.speaking = False
        self._halt = threading.Event()
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self) -> None:
        while not self._halt.is_set():
            try:
                with urllib.request.urlopen(f"{self.server}/health", timeout=0.5) as r:
                    self.speaking = bool(json.loads(r.read()).get("speaking"))
            except Exception:  # noqa: BLE001 - no server: nothing is playing
                self.speaking = False
            self._halt.wait(0.15)

    def stop(self) -> None:
        try:
            req = urllib.request.Request(f"{self.server}/stop", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=1.0).read()
        except Exception:  # noqa: BLE001
            pass
        self.speaking = False

    def close(self) -> None:
        self._halt.set()


class _LiveSettings:
    """Pause length and talk-over, re-read from a file the plugin rewrites on every change,
    so a setting changed mid-turn applies to the turn already listening."""

    def __init__(self, path: str | None, patience: str, talk_over: bool) -> None:
        self.path, self.patience, self.talk_over = path, patience, talk_over
        self._seen = 0.0
        self._checked = 0.0

    def refresh(self) -> None:
        now = time.time()
        if not self.path or now - self._checked < 0.5:
            return
        self._checked = now
        try:
            import os

            mtime = os.path.getmtime(self.path)
            if mtime == self._seen:
                return
            self._seen = mtime
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("patience") in PATIENCE and data["patience"] != self.patience:
            log.info("pause setting now %s", data["patience"])
            self.patience = data["patience"]
        if isinstance(data.get("talk_over"), bool):
            self.talk_over = data["talk_over"]

    @property
    def waits(self) -> tuple[float, float]:
        return PATIENCE.get(self.patience, PATIENCE["normal"])


def live_turn(*, stop_event: threading.Event, paused, device: str = "", patience: str = "normal",
              talk_over: bool = True, server: str = "http://127.0.0.1:8765", max_seconds: float = 3600.0,
              on_state=None, settings_file: str | None = None) -> TurnResult:
    """Record one turn. `stop_event` ends it early (Done or Stop); `paused()` is Mute."""
    live = _LiveSettings(settings_file, patience, talk_over)
    vad, smart = SileroVAD(), SmartTurn()
    claude = _ClaudeVoice(server)
    q: "queue.Queue[np.ndarray]" = queue.Queue()
    mic = _Mic(device, VAD_CHUNK, q)
    mic.open(refresh=False)

    state = ""

    def set_state(new: str) -> None:
        nonlocal state
        if new != state:
            state = new
            if on_state:
                on_state(new)

    pre_roll: collections.deque = collections.deque(maxlen=int(PRE_ROLL / CHUNK_SECONDS))
    turn: list[np.ndarray] = []
    started = False
    talked_over = False
    run = 0  # consecutive speech chunks before the turn starts
    silence = 0.0  # seconds of trailing silence after speech
    asked = False  # Smart Turn already asked during this pause
    finished = False  # ...and it said "finished"
    scores: list[float] = []
    longest = 0.0
    pending = np.zeros(0, dtype=np.float32)
    began = time.time()
    loudest = 0.0  # a mic that delivers exact zeros is dead (lid closed, unplugged, no permission)
    reason = "cap"
    set_state("waiting")
    try:
        while True:
            if stop_event.is_set():
                reason = "done"
                break
            if time.time() - began > max_seconds:
                reason = "cap"
                break
            try:
                data = q.get(timeout=0.2)
            except queue.Empty:
                data = None
            if mic.needs_reopen(time.time()):
                mic.reopen()
            if data is None:
                continue
            pending = np.concatenate([pending, data.astype(np.float32)])
            loudest = max(loudest, float(np.abs(data).max()) if len(data) else 0.0)
            if loudest == 0.0 and time.time() - began > 3.0:
                reason = "silent"
                break
            while len(pending) >= VAD_CHUNK:
                chunk, pending = pending[:VAD_CHUNK], pending[VAD_CHUNK:]
                live.refresh()
                end_after, give_up_after = live.waits
                talk_over = live.talk_over
                if paused():
                    # Muted: nothing is heard or kept, and a pause cannot end the turn.
                    set_state("muted")
                    continue
                p = vad.prob(chunk)

                if not started:
                    pre_roll.append(chunk)
                    if claude.speaking:
                        # Claude is talking: only a clear, sustained voice counts, and only with talk-over.
                        run = run + 1 if (talk_over and p >= TALK_OVER_PROB) else 0
                        if run >= TALK_OVER_CHUNKS:
                            claude.stop()
                            talked_over = True
                        else:
                            continue
                    else:
                        run = run + 1 if p >= SPEECH_ON else 0
                        if run < START_CHUNKS:
                            set_state("waiting")
                            continue
                    started = True
                    turn.extend(pre_roll)
                    pre_roll.clear()
                    silence, asked, finished = 0.0, False, False
                    set_state("hearing")
                    continue

                turn.append(chunk)
                if p >= SPEECH_ON:
                    silence, asked, finished = 0.0, False, False
                    set_state("hearing")
                    continue
                if p < SPEECH_OFF:
                    silence += CHUNK_SECONDS
                    longest = max(longest, silence)
                if silence <= 0:
                    continue
                set_state("pause")
                if not asked and silence >= ASK_AFTER:
                    asked = True
                    # Smart Turn hears only the last 8 s: join just that, not the whole turn.
                    score = smart.finished(np.concatenate(turn[-LAST_8S:]))
                    scores.append(round(score, 3))
                    finished = score >= FINISHED
                if finished and silence >= end_after:
                    reason = "finished"
                    raise StopIteration
                if silence >= give_up_after:
                    reason = "long_pause"
                    raise StopIteration
    except StopIteration:
        pass
    finally:
        mic.close()
        claude.close()
    device_used = mic.name
    audio = np.concatenate(turn) if turn else np.zeros(0, dtype=np.float32)
    if reason in ("finished", "long_pause") and len(audio):
        # Keep a little of the trailing pause; drop the rest.
        keep = len(audio) - int(max(0.0, silence - 0.3) * RATE)
        audio = audio[: max(keep, 0)]
    if not started:
        audio = np.zeros(0, dtype=np.float32)
    return TurnResult(audio=audio, reason=reason, talked_over=talked_over, device=device_used, smart_turn=scores,
                      patience=live.patience, longest_pause=round(longest, 1))

"""Microphone capture with simple energy-based voice activity detection."""
from __future__ import annotations

import queue
import threading
import time

import numpy as np

MIC_RATE = 16_000  # what Whisper expects


def input_devices() -> list[dict]:
    """The microphones sounddevice sees: [{"index", "name", "default": bool}, ...]."""
    import sounddevice as sd

    default_index = sd.default.device[0]
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"], "default": i == default_index})
    return out


def resolve_device(name: str) -> int | None:
    """The device index for a configured name (prefix match), or None for the system default."""
    if not name:
        return None
    for d in input_devices():
        if d["name"] == name or d["name"].lower().startswith(name.lower()):
            return d["index"]
    return None


def device_name(index: int | None) -> str:
    import sounddevice as sd

    try:
        return sd.query_devices(index if index is not None else sd.default.device[0])["name"]
    except Exception:  # noqa: BLE001
        return "default input"


class _Mic:
    """One input stream that reopens itself when the device situation changes.

    It follows the system default input unless a preferred microphone is set
    and present. It reopens when the system default changes (while following
    it), or when the stream dies or stops delivering audio (device unplugged).
    """

    STALL_SECONDS = 1.5

    def __init__(self, preference: str, block: int, q: "queue.Queue[np.ndarray]") -> None:
        self.preference = preference
        self.block = block
        self.q = q
        self.stream = None
        self.name = "default input"
        self.following_default = True
        self.default_id: int | None = None
        self.last_data = time.time()
        self.last_check = 0.0

    def _callback(self, indata, frames, t, status):  # noqa: ARG002
        self.last_data = time.time()
        self.q.put(indata[:, 0].copy())

    def open(self, refresh: bool) -> None:
        import sounddevice as sd

        from . import devices

        if refresh:
            devices.refresh()
        index = resolve_device(self.preference)
        self.following_default = index is None
        self.default_id = devices.default_input_id()
        self.stream = sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32",
                                     blocksize=self.block, callback=self._callback, device=index)
        self.stream.start()
        self.name = device_name(index)
        self.last_data = time.time()

    def close(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass
        self.stream = None

    def needs_reopen(self, now: float) -> bool:
        from . import devices

        if self.stream is None or not self.stream.active:
            return True
        if now - self.last_data > self.STALL_SECONDS:
            return True  # no audio arriving: the device went away
        if now - self.last_check >= 0.25:
            self.last_check = now
            if self.following_default:
                current = devices.default_input_id()
                if current is not None and current != self.default_id:
                    return True
        return False

    def reopen(self) -> None:
        self.close()
        try:
            self.open(refresh=True)
        except Exception:  # noqa: BLE001 - try again on the next tick
            self.stream = None
            self.last_data = time.time()


def record(*, silence_seconds: float, threshold: float, max_seconds: float,
           stop_event: threading.Event | None = None,
           on_level=None, device: str = "", info: dict | None = None,
           paused=None) -> np.ndarray:
    """Record from the microphone until the speaker goes quiet.

    `device` names a preferred microphone; empty (or absent) follows the system
    default, switching mid-recording when the default changes. Recording ends
    when `silence_seconds` of quiet follow detected speech, when `stop_event` is
    set (e.g. the user pressed Done), or at `max_seconds`. Returns float32 mono
    audio at 16 kHz; `info["device"]` receives the microphone used last.
    While `paused()` is true (the mic is muted) incoming audio is dropped and
    the take stays open; what was said before the mute is kept.
    """
    q: "queue.Queue[np.ndarray]" = queue.Queue()
    chunks: list[np.ndarray] = []
    heard_speech = False
    last_voice = time.time()
    started = time.time()
    block = int(MIC_RATE * 0.05)  # 50 ms blocks
    ended_by_silence = False
    was_paused = False
    mic = _Mic(device, block, q)
    mic.open(refresh=False)
    try:
        while True:
            try:
                data = q.get(timeout=0.2)
            except queue.Empty:
                data = None
            now = time.time()
            is_paused = bool(paused and paused())
            if is_paused:
                data = None  # muted: record nothing
                was_paused = True
            elif was_paused:
                was_paused = False
                last_voice = now  # a pause while muted is not the end of the take
            if data is not None:
                chunks.append(data)
                level = float(np.sqrt(np.mean(data ** 2)))
                if on_level:
                    on_level(level, heard_speech)
                if level >= threshold:
                    heard_speech = True
                    last_voice = now
            if stop_event is not None and stop_event.is_set():
                break
            if not is_paused and heard_speech and now - last_voice >= silence_seconds:
                ended_by_silence = True
                break
            if now - started >= max_seconds:
                break
            if mic.needs_reopen(now):
                mic.reopen()
    finally:
        mic.close()
        if info is not None:
            info["device"] = mic.name
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(chunks)
    if not ended_by_silence:
        return audio  # ended by Done or the cap: the last words may be right at the end
    # A pause ended the take: trim that trailing silence so Whisper doesn't hallucinate on it.
    keep = len(audio) - int(max(0.0, min(silence_seconds, 2.0) - 0.3) * MIC_RATE)
    return audio[: max(keep, 0)]

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


def record(*, silence_seconds: float, threshold: float, max_seconds: float,
           stop_event: threading.Event | None = None,
           on_level=None, device: int | None = None) -> np.ndarray:
    """Record from the default microphone until the speaker goes quiet.

    Recording ends when `silence_seconds` of quiet follow detected speech, when
    `stop_event` is set (e.g. the user pressed Enter), or at `max_seconds`.
    Returns float32 mono audio at 16 kHz.
    """
    import sounddevice as sd

    q: "queue.Queue[np.ndarray]" = queue.Queue()

    def callback(indata, frames, t, status):  # noqa: ARG001
        q.put(indata[:, 0].copy())

    chunks: list[np.ndarray] = []
    heard_speech = False
    last_voice = time.time()
    started = time.time()
    block = int(MIC_RATE * 0.05)  # 50 ms blocks
    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32", blocksize=block, callback=callback,
                        device=device):
        while True:
            try:
                data = q.get(timeout=0.2)
            except queue.Empty:
                data = None
            now = time.time()
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
            if heard_speech and now - last_voice >= silence_seconds:
                break
            if now - started >= max_seconds:
                break
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(chunks)
    # Trim trailing silence so Whisper doesn't hallucinate on it.
    keep = len(audio) - int(max(0.0, min(silence_seconds, 2.0) - 0.3) * MIC_RATE)
    return audio[: max(keep, 0)] if heard_speech else audio

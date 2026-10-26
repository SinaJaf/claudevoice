"""Microphone capture with simple energy-based voice activity detection."""
from __future__ import annotations

import queue
import threading
import time

import numpy as np

MIC_RATE = 16_000  # what Whisper expects


def record(*, silence_seconds: float, threshold: float, max_seconds: float,
           stop_event: threading.Event | None = None,
           on_level=None) -> np.ndarray:
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
    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32", blocksize=block, callback=callback):
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

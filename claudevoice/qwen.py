"""Qwen3-TTS, on the engine this computer runs it with.

Apple Silicon uses MLX. Windows and Linux with a recent NVIDIA card use qwentts.cpp, whose
ready-made packages need no compiler and no PyTorch. A processor alone is too slow to read
without stopping, so other computers are not offered this voice.
"""
from __future__ import annotations

import logging
import platform
import subprocess
import sys
from collections.abc import Iterator

import numpy as np

from . import config

MLX_MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
MLX_CHUNK = 1.0  # seconds of speech between deliveries, so it starts sounding at once

NATIVE_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
NATIVE_QUANT = "Q8_0"
NATIVE_CHUNK = 0.64  # the setting the package's maintainer validates streaming with

log = logging.getLogger("claudevoice.server")

VOICES = ("Aiden", "Ryan")
SAMPLE_RATE = 24_000
ENGINE_WARNING = 2  # qwentts.cpp log levels: 0 and 1 are every step of every sentence
MIN_COMPUTE = 7.5  # the oldest cards qwentts.cpp is built for: GTX 16 and RTX 20 series
MIN_DRIVER = 525  # the first NVIDIA driver for CUDA 12
MIN_GPU_MB = 3500  # the voice takes 1 to 2 GB; a 4 GB card reports a little under 4096


def uses_mlx() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def check() -> dict:
    """Can this computer run the voice, and if not, why not (for the settings pane)."""
    if uses_mlx():
        return {"can": True, "why": "", "gpu": "Apple Silicon"}
    if sys.platform == "darwin":
        return {"can": False, "why": "it needs Apple Silicon on a Mac", "gpu": ""}
    needs = "it needs an NVIDIA graphics card from 2018 or later"
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return {"can": False, "why": needs + " on a 64-bit Intel or AMD processor", "gpu": ""}
    try:
        ran = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, **config.no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return {"can": False, "why": needs, "gpu": ""}
    if ran.returncode != 0:
        return {"can": False, "why": needs + ", with a recent driver", "gpu": ""}
    cards = [_card(line) for line in ran.stdout.splitlines() if line.strip()]
    cards = [c for c in cards if c is not None]
    if not cards:
        return {"can": False, "why": needs, "gpu": ""}
    name, memory, compute, driver = max(cards, key=lambda c: (c[2], c[1]))
    if compute < MIN_COMPUTE:
        return {"can": False, "why": f"{name} is too old; {needs}", "gpu": name}
    if memory < MIN_GPU_MB:
        return {"can": False, "why": f"{name} has too little memory; it needs 4 GB", "gpu": name}
    if driver < MIN_DRIVER:
        return {"can": False, "why": f"it needs a newer NVIDIA driver for {name}", "gpu": name}
    return {"can": True, "why": "", "gpu": name}


def _card(line: str) -> tuple[str, float, float, int] | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 4:
        return None
    try:
        return parts[0], float(parts[1]), float(parts[2]), int(parts[3].split(".")[0])
    except ValueError:
        return None


def fetch() -> None:
    """Download the voice. Fetching only what this engine uses matters: the qwentts.cpp
    repository holds every size of every model, about 18 GB."""
    if uses_mlx():
        from huggingface_hub import snapshot_download

        snapshot_download(MLX_MODEL)
    else:
        from qwentts_cpp import resolve_gguf_paths

        resolve_gguf_paths(NATIVE_MODEL, quant=NATIVE_QUANT)


class _MlxVoice:
    def __init__(self) -> None:
        from mlx_audio.tts.utils import load_model

        self.model = load_model(MLX_MODEL)

    def stream(self, text: str, speaker: str) -> Iterator[np.ndarray]:
        results = self.model.generate_custom_voice(text=text, speaker=speaker, language="English",
                                                   stream=True, streaming_interval=MLX_CHUNK)
        for result in results:
            yield np.array(result.audio, dtype=np.float32).reshape(-1)

    def close(self) -> None:
        import gc

        import mlx.core as mx

        self.model = None
        gc.collect()
        mx.clear_cache()


def _engine_note(level: int, message: str) -> None:
    if level >= ENGINE_WARNING:
        log.warning("qwentts: %s", message.strip())


class _NativeVoice:
    def __init__(self) -> None:
        from qwentts_cpp import QwenTTS

        self.model = QwenTTS.from_pretrained(NATIVE_MODEL, quant=NATIVE_QUANT, codec_chunk_sec=NATIVE_CHUNK)
        self.model.set_log_callback(_engine_note)

    def stream(self, text: str, speaker: str) -> Iterator[np.ndarray]:
        for audio, rate in self.model.stream(text=text, speaker=speaker.lower(), lang="english"):
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            if rate != SAMPLE_RATE and len(samples):
                count = int(len(samples) * SAMPLE_RATE / rate)
                samples = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples).astype(np.float32)
            yield samples

    def close(self) -> None:
        self.model.close()


def load() -> _MlxVoice | _NativeVoice:
    return _MlxVoice() if uses_mlx() else _NativeVoice()


def speaker_for(voice: str) -> str:
    """The voice asked for, or the first one when another engine's voice name is in the settings."""
    for known in VOICES:
        if voice.lower() == known.lower():
            return known
    return VOICES[0]


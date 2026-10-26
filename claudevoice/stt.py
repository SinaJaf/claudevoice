"""Speech-to-text: local mlx-whisper, or an OpenAI-compatible cloud endpoint."""
from __future__ import annotations

import io
import json
import logging
import os
import urllib.request
import uuid
import warnings

import numpy as np

log = logging.getLogger("claudevoice.stt")

# Phrases Whisper tends to invent on silence or breath noise.
_HALLUCINATIONS = {
    "", "you", "thank you.", "thanks for watching!", "thank you for watching.",
    "bye.", "subtitles by the amara.org community", ".",
}


def _clean(text: str) -> str:
    text = text.strip()
    return "" if text.lower() in _HALLUCINATIONS else text


def _short(name: str) -> str:
    """'mlx-community/whisper-large-v3-mlx' -> 'large-v3'; short names pass through."""
    return name.rsplit("/whisper-", 1)[-1].removesuffix("-mlx")


def _mlx_repo(name: str) -> str:
    if "/" in name:
        return name
    short = _short(name)
    return f"mlx-community/whisper-{short}" if short.endswith("turbo") else f"mlx-community/whisper-{short}-mlx"


def _has_mlx() -> bool:
    try:
        import mlx_whisper  # noqa: F401

        return True
    except ImportError:
        return False


class Transcriber:
    """Local Whisper: mlx-whisper on Apple Silicon, faster-whisper (CTranslate2) elsewhere."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._loaded = False
        self._fw = None
        self.backend = "mlx" if _has_mlx() else "faster-whisper"

    def warm(self) -> None:
        if self._loaded:
            return
        warnings.filterwarnings("ignore")
        if self.backend == "mlx":
            import mlx_whisper  # noqa: F401 - the import is the expensive part
        else:
            from faster_whisper import WhisperModel

            self._fw = WhisperModel(_short(self.model), device="auto", compute_type="int8")
        self.transcribe(np.zeros(16_000, dtype=np.float32))
        self._loaded = True

    def transcribe(self, audio: np.ndarray, language: str | None = "en") -> str:
        if len(audio) < 1600:  # under 0.1 s
            return ""
        if self.backend == "mlx":
            import mlx_whisper

            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=_mlx_repo(self.model),
                language=language,
                fp16=True,
                condition_on_previous_text=False,
                no_speech_threshold=0.5,
            )
            return _clean(result.get("text") or "")
        if self._fw is None:
            from faster_whisper import WhisperModel

            self._fw = WhisperModel(_short(self.model), device="auto", compute_type="int8")
        segments, _info = self._fw.transcribe(audio, language=language, vad_filter=True, condition_on_previous_text=False)
        return _clean(" ".join(seg.text.strip() for seg in segments))


class CloudTranscriber:
    """OpenAI-compatible POST /audio/transcriptions (OpenAI, Groq, a local server...)."""

    def __init__(self, *, base_url: str, model: str, api_key_env: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env

    def warm(self) -> None:
        if not os.environ.get(self.api_key_env):
            raise RuntimeError(f"cloud listening needs the {self.api_key_env} environment variable")

    def transcribe(self, audio: np.ndarray, language: str | None = "en") -> str:
        import soundfile as sf

        if len(audio) < 1600:
            return ""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"cloud listening needs the {self.api_key_env} environment variable")
        wav = io.BytesIO()
        sf.write(wav, audio, 16_000, format="WAV", subtype="PCM_16")
        boundary = uuid.uuid4().hex
        fields = {"model": self.model}
        if language:
            fields["language"] = language
        body = b""
        for name, value in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n").encode() + wav.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions", data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return _clean(data.get("text") or "")


def make_transcriber(cfg: dict, whisper_model: str | None = None):
    if cfg.get("stt_engine", "local") == "openai":
        return CloudTranscriber(base_url=cfg["stt_base_url"], model=cfg["stt_model"], api_key_env=cfg["stt_api_key_env"])
    return Transcriber(whisper_model or cfg["whisper_model"])

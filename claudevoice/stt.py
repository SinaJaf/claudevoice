"""Speech-to-text: NVIDIA Parakeet (local, default), the system recognizers, Whisper, or a cloud endpoint."""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
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


_APPLE_SRC = Path(__file__).parent / "apple"
PIECE_SECONDS = 50  # Apple's recognizer returns only the last stretch of longer audio


def apple_helper() -> Path:
    """The ClaudeVoiceSTT.app helper, built from the shipped Swift source when missing or stale.

    macOS grants speech recognition only to an app that declares why it wants it, so
    the recognizer runs in this small signed app rather than in Python.
    """
    from . import config

    app = config.CONFIG_DIR / "ClaudeVoiceSTT.app"
    exe = app / "Contents" / "MacOS" / "ClaudeVoiceSTT"
    source = _APPLE_SRC / "ClaudeVoiceSTT.swift"
    plist = _APPLE_SRC / "Info.plist"
    newest = max(source.stat().st_mtime, plist.stat().st_mtime)
    if exe.exists() and exe.stat().st_mtime >= newest:
        return app
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not Path(swiftc).exists():
        raise RuntimeError("the apple engine needs the Xcode command line tools (xcode-select --install)")
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(plist, app / "Contents" / "Info.plist")
    subprocess.run([swiftc, "-O", "-o", str(exe), str(source)], check=True, capture_output=True, timeout=600)
    subprocess.run(["codesign", "--force", "-s", "-", str(app)], check=True, capture_output=True, timeout=60)
    return app


def _pieces(audio: np.ndarray, rate: int) -> list[np.ndarray]:
    """Cut a long take into pieces under PIECE_SECONDS, each cut at the quietest moment nearby."""
    out: list[np.ndarray] = []
    start, limit, window = 0, PIECE_SECONDS * rate, int(0.3 * rate)
    while len(audio) - start > limit:
        lo, hi = start + int(0.6 * limit), start + limit - window
        best, best_level = hi, float("inf")
        for i in range(lo, hi, window // 2):
            level = float(np.sqrt(np.mean(audio[i : i + window] ** 2)))
            if level < best_level:
                best, best_level = i + window // 2, level
        out.append(audio[start:best])
        start = best
    out.append(audio[start:])
    return out


class AppleTranscriber:
    """The Mac's own speech recognizer (what macOS dictation uses), on-device."""

    def __init__(self, language: str = "") -> None:
        self.language = language

    def warm(self) -> None:
        apple_helper()

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:  # noqa: ARG002
        import soundfile as sf

        if len(audio) == 0:
            return ""
        app = apple_helper()
        with tempfile.TemporaryDirectory(prefix="claudevoice-") as tmp:
            paths = []
            for k, piece in enumerate(_pieces(audio, 16_000)):
                path = os.path.join(tmp, f"piece{k}.wav")
                sf.write(path, piece, 16_000, subtype="PCM_16")
                paths.append(path)
            result = os.path.join(tmp, "result.json")
            # `open` launches it as its own app, so the speech permission is its own.
            subprocess.run(["open", "-W", "-n", "-g", "-a", str(app), "--args", result, self.language, *paths],
                           check=True, capture_output=True, timeout=900)
            deadline = time.time() + 5
            while not os.path.exists(result) and time.time() < deadline:
                time.sleep(0.05)
            data = json.loads(Path(result).read_text()) if os.path.exists(result) else {"error": "no answer from ClaudeVoiceSTT"}
        if data.get("error"):
            raise RuntimeError(data["error"])
        return (data.get("text") or "").strip()


def resolve_engine(cfg: dict) -> str:
    """auto: Parakeet, on every system."""
    engine = cfg.get("stt_engine", "auto")
    return "parakeet" if engine == "auto" else engine


PARAKEET_MODELS = {
    "v2": "nemo-parakeet-tdt-0.6b-v2",  # English, the most accurate for English
    "v3": "nemo-parakeet-tdt-0.6b-v3",  # 25 European languages
}


class ParakeetTranscriber:
    """NVIDIA Parakeet TDT 0.6B (CC-BY-4.0), int8 ONNX on the CPU through onnx-asr.

    Accurate, punctuated, and steady on long talk; about a second for a minute of
    speech on a laptop. Downloads ~650 MB once.
    """

    def __init__(self, version: str = "v2") -> None:
        self.name = PARAKEET_MODELS.get(version, PARAKEET_MODELS["v2"])
        self._model = None

    def _load(self):
        if self._model is None:
            import onnx_asr
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.log_severity_level = 3
            self._model = onnx_asr.load_model(self.name, quantization="int8",
                                              providers=["CPUExecutionProvider"], sess_options=so)
        return self._model

    def warm(self) -> None:
        self._load()

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:  # noqa: ARG002
        if len(audio) < 1600:
            return ""
        model = self._load()
        from .turn import squeeze_silence

        audio = squeeze_silence(audio)  # long thinking pauses invite invented words
        # Long turns go in pieces cut at the quietest moments, to keep memory flat.
        parts = [model.recognize(piece.astype(np.float32), sample_rate=16_000) for piece in _pieces(audio, 16_000)]
        return _clean(" ".join(p.strip() for p in parts if p and p.strip()))


WINDOWS_SCRIPT = Path(__file__).parent / "windows" / "dictate.ps1"


class WindowsDictation:
    """Windows' own speech recognizer (the engine behind voice typing).

    It listens to the microphone itself, so listen.py hands it the whole turn
    (see listen_windows) instead of passing it a recording to transcribe.
    """

    live = True

    def __init__(self, language: str = "") -> None:
        self.language = language

    def warm(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the windows engine runs only on Windows")

    def transcribe(self, audio, language=None) -> str:  # noqa: ARG002
        raise RuntimeError("the windows engine records by itself; use listen_windows")

    def listen(self, *, done_flag: Path, stop_flag: Path, mute_flag: Path, heartbeat: str | None,
               max_seconds: float) -> dict:
        with tempfile.TemporaryDirectory(prefix="claudevoice-") as tmp:
            out = os.path.join(tmp, "result.json")
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(WINDOWS_SCRIPT), "-Out", out, "-DoneFlag", str(done_flag),
                    "-StopFlag", str(stop_flag), "-MuteFlag", str(mute_flag),
                    "-MaxSeconds", str(int(max_seconds))]
            if heartbeat:
                argv += ["-Heartbeat", heartbeat]
            if self.language:
                argv += ["-Language", self.language]
            ran = subprocess.run(argv, capture_output=True, text=True, timeout=max_seconds + 120)
            if not os.path.exists(out):
                tail = (ran.stderr or ran.stdout or "").strip().splitlines()[-1:] or ["no answer"]
                return {"error": f"Windows speech recognition failed: {tail[0]}"}
            return json.loads(Path(out).read_text(encoding="utf-8-sig"))


def _whisper_installed() -> bool:
    import importlib.util

    return any(importlib.util.find_spec(m) is not None for m in ("mlx_whisper", "faster_whisper"))


def make_transcriber(cfg: dict, whisper_model: str | None = None):
    engine = resolve_engine(cfg)
    if engine == "parakeet":
        return ParakeetTranscriber(str(cfg.get("parakeet_model", "v2")))
    if engine == "apple":
        return AppleTranscriber(cfg.get("stt_language", ""))
    if engine == "windows":
        return WindowsDictation(cfg.get("stt_language", ""))
    if engine == "local" and not _whisper_installed():
        raise RuntimeError("Whisper is not installed; pick it again in the voice settings to install it, "
                           "or run: uv sync --extra whisper --inexact")
    if engine == "openai":
        return CloudTranscriber(base_url=cfg["stt_base_url"], model=cfg["stt_model"], api_key_env=cfg["stt_api_key_env"])
    return Transcriber(whisper_model or cfg["whisper_model"])

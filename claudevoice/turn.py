"""Live turn-taking: Silero VAD hears speech, Smart Turn decides when you've finished.

Both are small ONNX models run with onnxruntime on the CPU:
- Silero VAD (MIT, onnx-community/silero-vad): speech probability per 32 ms chunk.
- Smart Turn v3.2 (BSD-2, pipecat-ai/smart-turn-v3): from the last 8 s of audio,
  the probability that the speaker has finished their turn. It listens to how you
  speak (intonation, trailing off), so a thinking pause mid-thought is not an end.

The Whisper log-mel features Smart Turn expects are computed here with numpy, so
the large `transformers` package is not needed.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("claudevoice.turn")

RATE = 16_000
VAD_CHUNK = 512  # Silero expects 512-sample chunks at 16 kHz
SMART_TURN_REPO = "pipecat-ai/smart-turn-v3"
SMART_TURN_FILE = "smart-turn-v3.2-cpu.onnx"
SMART_TURN_REVISION = "f766f81d3cfdf7737ac64aad813d91bbfd56bf93"
SILERO_REPO = "onnx-community/silero-vad"
SILERO_FILE = "onnx/model.onnx"
SILERO_REVISION = "e71cae966052b992a7eca6b17738916ce0eca4ec"


def _session(path: str):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = 1
    so.log_severity_level = 3
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


def _download(repo: str, filename: str, revision: str) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo_id=repo, filename=filename, revision=revision)


class SileroVAD:
    """Speech probability for each 512-sample chunk (16 kHz mono float32)."""

    CONTEXT = 64

    def __init__(self) -> None:
        self.session = _session(_download(SILERO_REPO, SILERO_FILE, SILERO_REVISION))
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def prob(self, chunk: np.ndarray) -> float:
        x = np.concatenate((self._context, chunk.reshape(1, -1).astype(np.float32)), axis=1)
        out, self._state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(RATE, dtype=np.int64)})
        self._context = x[:, -self.CONTEXT:]
        return float(out[0][0])


# ---- Whisper log-mel features (as WhisperFeatureExtractor(chunk_length=8)) -----------

N_FFT = 400
HOP = 160
N_MELS = 80
SECONDS = 8


def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    # Slaney mel scale: linear below 1 kHz, logarithmic above.
    f = np.asarray(f, dtype=np.float64)
    mel = 3.0 * f / 200.0
    log_region = f >= 1000.0
    mel = np.where(log_region, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0), mel)
    return mel


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float64)
    f = 200.0 * m / 3.0
    log_region = m >= 15.0
    return np.where(log_region, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), f)


def _mel_filters() -> np.ndarray:
    """(n_freqs, n_mels) triangular filters, Slaney-normalised, 0 to 8 kHz."""
    n_freqs = N_FFT // 2 + 1
    fft_freqs = np.linspace(0, RATE // 2, n_freqs)
    mel_pts = np.linspace(_hz_to_mel(0.0), _hz_to_mel(8000.0), N_MELS + 2)
    hz_pts = _mel_to_hz(mel_pts)
    diff = np.diff(hz_pts)
    slopes = hz_pts[None, :] - fft_freqs[:, None]  # (n_freqs, n_mels+2)
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    enorm = 2.0 / (hz_pts[2: N_MELS + 2] - hz_pts[:N_MELS])
    return fb * enorm[None, :]


_FILTERS = None
_WINDOW = None


def whisper_features(audio: np.ndarray) -> np.ndarray:
    """(1, 80, 800) log-mel features of exactly 8 s of 16 kHz audio."""
    global _FILTERS, _WINDOW
    if _FILTERS is None:
        _FILTERS = _mel_filters()
        n = np.arange(N_FFT)
        _WINDOW = 0.5 - 0.5 * np.cos(2 * np.pi * n / N_FFT)  # periodic Hann
    x = audio.astype(np.float64)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)  # do_normalize
    x = np.pad(x, (N_FFT // 2, N_FFT // 2), mode="reflect")  # center=True
    n_frames = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * _WINDOW[None, :]
    power = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2  # (frames, freqs)
    mel = power @ _FILTERS  # (frames, mels)
    log_spec = np.log10(np.maximum(mel, 1e-10)).T[:, :-1]  # drop the last frame
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec[None, :, :].astype(np.float32)


def last_seconds(audio: np.ndarray, seconds: int = SECONDS) -> np.ndarray:
    """The last `seconds` of audio, zero-padded at the start when shorter."""
    n = seconds * RATE
    if len(audio) >= n:
        return audio[-n:]
    return np.pad(audio, (n - len(audio), 0))


class SmartTurn:
    """P(the speaker has finished) from the end of their audio so far."""

    def __init__(self) -> None:
        self.session = _session(_download(SMART_TURN_REPO, SMART_TURN_FILE, SMART_TURN_REVISION))
        self.input_name = self.session.get_inputs()[0].name

    def finished(self, audio: np.ndarray) -> float:
        feats = whisper_features(last_seconds(audio))
        out = self.session.run(None, {self.input_name: feats})
        return float(np.asarray(out[0]).reshape(-1)[0])


def squeeze_silence(audio: np.ndarray, keep: float = 0.5, lead_in: float = 0.35,
                    threshold: float = 0.35) -> np.ndarray:
    """Shorten every pause longer than `keep` seconds.

    A long thinking pause inside one turn gives a speech model room to invent words;
    with the pauses squeezed it hears only speech and short gaps. The `lead_in` before
    speech resumes is kept too, since a word's first sound often reads as silence.
    """
    import collections

    if len(audio) < RATE:
        return audio
    vad = SileroVAD()
    keep_chunks = max(1, int(keep * RATE / VAD_CHUNK))
    dropped: collections.deque = collections.deque(maxlen=max(1, int(lead_in * RATE / VAD_CHUNK)))
    out: list[np.ndarray] = []
    quiet = 0
    for i in range(0, len(audio) - VAD_CHUNK + 1, VAD_CHUNK):
        chunk = audio[i : i + VAD_CHUNK]
        if vad.prob(chunk) < threshold:
            quiet += 1
            if quiet > keep_chunks:
                dropped.append(chunk)
                continue
        else:
            quiet = 0
            out.extend(dropped)  # the lead-in to this speech
            dropped.clear()
        out.append(chunk)
    tail = audio[(len(audio) // VAD_CHUNK) * VAD_CHUNK :]
    if len(tail):
        out.append(tail)
    return np.concatenate(out) if out else audio

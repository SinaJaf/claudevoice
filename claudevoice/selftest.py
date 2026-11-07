"""`claudevoice self-test`: proves the voice works on this computer, no speakers or mic needed.

Kokoro speaks a sentence into memory, and Parakeet, the listening engine, writes down what it
hears. The test passes when it hears the same words. That covers the two heavy halves a fresh
install can get wrong (the speech packages and the models they download) without sound
hardware, so it also runs on a test machine with none.
"""
from __future__ import annotations

import json
import re
import sys
import time

import numpy as np

from . import config, pacing

SENTENCE = "Testing the voice on this computer: one, two, three."
WORDS_NEEDED = 0.8  # the share of the sentence's words that has to be heard


def _words(text: str) -> list[str]:
    numbers = {"1": "one", "2": "two", "3": "three"}
    return [numbers.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def speak(text: str) -> np.ndarray:
    """The sentence as Kokoro says it, at 24 kHz, with the pauses the plugin adds."""
    import warnings

    warnings.filterwarnings("ignore")
    from kokoro import KPipeline

    cfg = config.load()
    pipeline = KPipeline(lang_code=cfg["lang_code"], repo_id="hexgrad/Kokoro-82M")
    parts = []
    for result in pipeline(text, voice=config.DEFAULTS["voice"], speed=1.0):
        if result.audio is not None:
            parts.append(pacing.pace(result.audio.detach().cpu().numpy().astype(np.float32), result.tokens))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def hear(audio: np.ndarray) -> str:
    """What Parakeet hears in 24 kHz audio."""
    from .stt import make_transcriber

    cfg = config.load()
    cfg["stt_engine"] = "parakeet"
    count = int(len(audio) * 16_000 / pacing.SAMPLE_RATE)
    sixteen = np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio).astype(np.float32)
    return make_transcriber(cfg).transcribe(sixteen)


def run(listen: bool = True) -> dict:
    result: dict = {"sentence": SENTENCE, "ok": False}
    t0 = time.time()
    try:
        audio = speak(SENTENCE)
    except Exception as exc:  # noqa: BLE001 - the report says what failed
        result["error"] = f"speaking failed: {type(exc).__name__}: {exc}"
        return result
    seconds = len(audio) / pacing.SAMPLE_RATE
    result["spoke_seconds"] = round(seconds, 2)
    result["speak_took"] = round(time.time() - t0, 1)
    if seconds < 1.0 or float(np.abs(audio).max(initial=0.0)) < 0.01:
        result["error"] = "speaking made no sound"
        return result
    if not listen:
        result["ok"] = True
        return result
    t0 = time.time()
    try:
        heard = hear(audio)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"listening failed: {type(exc).__name__}: {exc}"
        return result
    result["heard"] = heard
    result["hear_took"] = round(time.time() - t0, 1)
    said, got = _words(SENTENCE), set(_words(heard))
    share = sum(1 for w in said if w in got) / len(said)
    result["words_heard"] = round(share, 2)
    result["ok"] = share >= WORDS_NEEDED
    if not result["ok"]:
        result["error"] = "the words heard did not match what was spoken"
    return result


def main(argv: list[str]) -> int:
    found = run(listen="--no-listen" not in argv)
    print(json.dumps(found, indent=2))
    return 0 if found["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

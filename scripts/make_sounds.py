"""Make the notification sounds in assets/ (generated here, so they carry no third-party license).

done.wav: a soft rising pair of notes, "your result is ready".
attention.wav: a brighter double pulse, higher and quicker, "this needs you".

Run: .venv/bin/python scripts/make_sounds.py
"""
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 44_100
OUT = Path(__file__).resolve().parent.parent / "assets"


def note(freq: float, seconds: float, decay: float, *, bright: float = 0.0) -> np.ndarray:
    """One bell-like note: a fundamental with a soft octave, a quick attack and a fading tail."""
    t = np.arange(int(RATE * seconds)) / RATE
    wave = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t) + bright * np.sin(2 * np.pi * 3 * freq * t)
    attack = np.minimum(t / 0.006, 1.0)
    return wave * attack * np.exp(-t / decay)


def place(parts: list[tuple[float, np.ndarray]], seconds: float, peak: float) -> np.ndarray:
    """Lay notes out at their start times, and bring the whole to a gentle level."""
    out = np.zeros(int(RATE * seconds))
    for start, part in parts:
        at = int(RATE * start)
        out[at:at + len(part)] += part[: len(out) - at]
    fade = np.minimum((len(out) - np.arange(len(out))) / (RATE * 0.02), 1.0)
    out *= fade
    return out / np.abs(out).max() * peak


def main() -> None:
    OUT.mkdir(exist_ok=True)
    done = place([(0.0, note(659.25, 0.45, 0.16)), (0.13, note(987.77, 0.55, 0.20))], 0.70, 0.50)
    attention = place([(t, note(1318.5, 0.16, 0.07, bright=0.2) + 0.6 * note(1760.0, 0.16, 0.06)) for t in (0.0, 0.17)]
                      + [(0.40, note(1318.5, 0.26, 0.10, bright=0.2) + 0.6 * note(1760.0, 0.26, 0.09))], 0.70, 0.60)
    sf.write(OUT / "done.wav", done, RATE, subtype="PCM_16")
    sf.write(OUT / "attention.wav", attention, RATE, subtype="PCM_16")


if __name__ == "__main__":
    main()

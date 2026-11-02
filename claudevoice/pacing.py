"""Pauses where a reader would leave them.

Kokoro hears punctuation but hardly acts on it: measured on its own, "Jack said; hi Mary"
has no gap at the semicolon, and "Jack said. Hi Mary" has 0.06 s at the period. It does say
when each word falls, so the sentence is synthesized whole (which keeps its melody) and
quiet is then added at each mark, up to the length a reader would give it.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

import numpy as np

SAMPLE_RATE = 24_000

# Seconds of quiet at normal speed; a faster voice pauses for less.
PAUSES = {",": 0.18, ";": 0.30, ":": 0.35, "—": 0.30, "(": 0.20, ")": 0.20,
          ".": 0.50, "!": 0.50, "?": 0.50, "…": 0.50}
LINE = 0.65  # after a line of its own: a list item
PARAGRAPH = 0.90  # after a heading or a paragraph
WORD_GAP = 0.05  # a piece cut mid-phrase because it ran long

_QUOTES = "\"“”'’"
_FRAME = 120  # 5 ms: the steps quiet is measured in
_QUIET = 0.01  # below this a frame is a gap between words
_EDGE = 0.003  # below this a piece's start or end is empty
_LOOK = 0.03  # seconds either side of a mark to look for its gap; wider finds the stops inside words
_FADE = 96  # 4 ms each side of added quiet, so it never clicks


def silence(seconds: float) -> np.ndarray:
    return np.zeros(max(0, int(SAMPLE_RATE * seconds)), dtype=np.float32)


def pause_after(piece: str, speed: float = 1.0) -> float:
    """How long to wait after a piece of text, from how it ends. A piece that ended
    a line keeps its newline (see clean.split_sentences); a blank line is a paragraph."""
    if piece.endswith("\n\n"):
        return PARAGRAPH / speed
    if piece.endswith("\n"):
        return LINE / speed
    end = piece.rstrip().rstrip(_QUOTES + ")")[-1:]
    return PAUSES.get(end, WORD_GAP) / speed


def pace(audio: np.ndarray, tokens: list | None, speed: float = 1.0) -> np.ndarray:
    """`audio` with its ends trimmed and a reader's pauses added at the punctuation in `tokens`."""
    if tokens:
        audio = _add_pauses(audio, tokens, speed)
    return _trim(audio)


def _marks(tokens: list) -> list[tuple[float, float, float]]:
    """Each run of punctuation between words: (start, end, pause). Not the text's own ends:
    nothing is waited for before the first word, and the last one's pause is the caller's."""
    marks: list[tuple[float, float, float]] = []
    run: list = []
    spoken = False
    for token in tokens:
        phonemes = getattr(token, "phonemes", "") or ""
        if phonemes and all(c in PAUSES or c in _QUOTES for c in phonemes):
            run.append(token)
            continue
        if not phonemes:
            continue
        if run and spoken:
            pause = max((PAUSES.get(c, 0.0) for t in run for c in t.phonemes), default=0.0)
            start, end = run[0].start_ts, run[-1].end_ts
            if pause and start is not None and end is not None:
                marks.append((start, end, pause))
        run = []
        spoken = True
    return marks


def _add_pauses(audio: np.ndarray, tokens: list, speed: float) -> np.ndarray:
    audio = audio.copy()
    loud = _loudness(audio)
    pieces: list[np.ndarray] = []
    done = 0
    for start, end, pause in _marks(tokens):
        at, existing = _gap_near(loud, start, end)
        extra = pause / speed - existing
        sample = at * _FRAME
        if extra <= 0 or sample <= done:
            continue
        pieces += [_fade(audio[done:sample], out=True), silence(extra)]
        _fade(audio[sample:], out=False)
        done = sample
    pieces.append(audio[done:])
    return np.concatenate(pieces) if len(pieces) > 1 else audio


def _loudness(audio: np.ndarray) -> np.ndarray:
    frames = len(audio) // _FRAME
    if frames == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(audio[: frames * _FRAME].reshape(frames, _FRAME) ** 2, axis=1))


def _gap_near(loud: np.ndarray, start: float, end: float) -> tuple[int, float]:
    """Where to add quiet near a mark, in frames, and how much quiet is there already."""
    per_second = SAMPLE_RATE / _FRAME
    low = max(0, int((start - _LOOK) * per_second))
    high = min(len(loud), int((end + _LOOK) * per_second) + 1)
    if high <= low:
        return min(low, len(loud)), 0.0
    best_at, best_run, run = 0, 0, 0
    for i in range(low, high):
        run = run + 1 if loud[i] < _QUIET else 0
        if run > best_run:
            best_run, best_at = run, i - run + 1
    if best_run == 0:  # no gap at all: the softest moment between the two words
        return low + int(np.argmin(loud[low:high])), 0.0
    # The gap may run on past where we looked.
    first, last = best_at, best_at + best_run
    while first > 0 and loud[first - 1] < _QUIET:
        first -= 1
    while last < len(loud) and loud[last] < _QUIET:
        last += 1
    return (first + last) // 2, (last - first) / per_second


def _fade(audio: np.ndarray, *, out: bool) -> np.ndarray:
    """Fade `audio` (a view of the piece) in place at the end where quiet is added."""
    n = min(_FADE, len(audio))
    if n:
        if out:
            audio[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
        else:
            audio[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
    return audio


def _trim(audio: np.ndarray) -> np.ndarray:
    """Cut the quiet a piece starts and ends with, so the pauses between pieces are the ones asked for."""
    return _trim_end(_trim_start(audio))


def _trim_start(audio: np.ndarray, lead: float = 0.02) -> np.ndarray:
    sounding = np.flatnonzero(_loudness(audio) >= _EDGE)
    if len(sounding) == 0:
        return audio
    return audio[max(0, int(sounding[0]) * _FRAME - int(SAMPLE_RATE * lead)):]


def _trim_end(audio: np.ndarray, tail: float = 0.05) -> np.ndarray:
    sounding = np.flatnonzero(_loudness(audio) >= _EDGE)
    if len(sounding) == 0:
        return audio
    return audio[: min(len(audio), (int(sounding[-1]) + 1) * _FRAME + int(SAMPLE_RATE * tail))]


def trim_stream(chunks: Iterable[np.ndarray]) -> Iterator[np.ndarray]:
    """The same for audio that arrives in chunks. The newest chunk is held back until
    the next one comes, because the last one is the one to trim."""
    held: np.ndarray | None = None
    for chunk in chunks:
        if held is None:
            if not (_loudness(chunk) >= _EDGE).any():
                continue  # nothing but the quiet before speech
            held = _trim_start(chunk)
            continue
        yield held
        held = chunk
    if held is not None:
        yield _trim_end(held)

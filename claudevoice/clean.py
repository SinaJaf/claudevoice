"""Turn Claude's markdown reply into something that sounds natural when read aloud.

Kokoro reads exactly what it is given, so backticks, bullets, headers, URLs and
emoji all turn into noise unless they are stripped first. This module is
stdlib-only on purpose: the Stop hook imports it with the system python.
"""
from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.MULTILINE)
_HR = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_HTML_TAG = re.compile(r"<[^>\n]+>")
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, emoticons, symbols
    "\U00002600-\U000027BF"  # misc symbols, dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F900-\U0001F9FF"
    "‍"
    "]+",
)
_MULTI_SPACE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,!?;:])")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
# Absolute, home-relative, or relative paths with at least one directory segment.
# Segments must start with a letter, dot or underscore so "3/4" is left alone.
_PATH = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/?(?:[A-Za-z_.][\w.\-]*/)+[\w.\-]+")
_FILE_LINE = re.compile(r"(\b[\w.\-]+\.(?:py|ts|tsx|js|jsx|rs|go|rb|md|json|toml|yaml|yml|sh|c|cpp|h|swift)):(\d+)\b")

# Short spoken replacements for symbols Kokoro would otherwise skip or mangle.
_SYMBOLS = [
    (re.compile(r"\s*->\s*"), " to "),
    (re.compile(r"\s*=>\s*"), " gives "),
    (re.compile(r"\s*&&\s*"), " and "),
    (re.compile(r"\s*\|\|\s*"), " or "),
    (re.compile(r"(?<=\w)/(?=\w)"), " slash "),
    (re.compile(r"\s*%\s*"), " percent "),
    (re.compile(r"\s+@\s*"), " at "),
    (re.compile(r"\s*\+\s*(?=\d)"), " plus "),
    (re.compile(r"(?<=\d)x(?=\d)"), " by "),
]


def _table_row_to_speech(match: re.Match) -> str:
    cells = [c.strip() for c in match.group(0).strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    return ", ".join(cells) + "." if cells else ""


_ENDS_WITH_PUNCT = re.compile(r"[.!?:;,]\s*$")


def _end_lines(text: str) -> str:
    """Give short standalone lines (headers, bullets, table rows) a sentence end so
    the voice pauses instead of running them into the next line."""
    lines = text.split("\n")
    for i, line in enumerate(lines[:-1]):
        stripped = line.strip()
        if stripped and not _ENDS_WITH_PUNCT.search(stripped):
            lines[i] = line.rstrip() + "."
    return "\n".join(lines)


def _replace_code_fence(match: re.Match) -> str:
    body = match.group(0).strip("`")
    first_line = body.split("\n", 1)[0].strip()
    lang = first_line if first_line and " " not in first_line else ""
    lines = body.count("\n")
    if lines <= 1:
        return " code snippet. "
    return f" {lang} code block, {lines} lines. " if lang else f" code block, {lines} lines. "


def markdown_to_speech(text: str, *, max_chars: int | None = None) -> str:
    """Strip markdown and code so the text reads naturally when spoken."""
    if not text:
        return ""
    out = text
    out = _CODE_FENCE.sub(_replace_code_fence, out)
    out = _TABLE_RULE.sub("", out)
    out = _TABLE_ROW.sub(_table_row_to_speech, out)
    out = _LINK.sub(r"\1", out)
    out = _BARE_URL.sub(" a link ", out)
    out = _HTML_TAG.sub("", out)
    out = _HEADER.sub("", out)  # header text stays; _end_lines adds the pause
    out = _HR.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _BULLET.sub("", out)
    out = _end_lines(out)
    out = _EMPHASIS.sub(r"\2", out)
    out = _FILE_LINE.sub(lambda m: f"{m.group(1)} line {m.group(2)}", out)
    out = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _EMOJI.sub("", out)
    for pattern, repl in _SYMBOLS:
        out = pattern.sub(repl, out)
    out = _MULTI_SPACE.sub(" ", out)
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _MULTI_NEWLINE.sub("\n\n", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    out = out.strip()
    if max_chars and len(out) > max_chars:
        cut = out[:max_chars]
        # Break at the last sentence end so we don't stop mid-word.
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        out = (cut[: end + 1] if end > max_chars // 2 else cut).rstrip() + " I'll stop there."
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")


def split_sentences(text: str, *, min_chars: int = 40) -> list[str]:
    """Split text into sentence-ish chunks that are long enough to sound natural.

    Very short fragments are merged with the next one so Kokoro doesn't produce
    choppy single-word utterances.
    """
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        buf = f"{buf} {part}".strip() if buf else part
        if len(buf) >= min_chars:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks and len(buf) < min_chars // 2:
            chunks[-1] = f"{chunks[-1]} {buf}"
        else:
            chunks.append(buf)
    return chunks


class StreamingSentenceSplitter:
    """Feed text deltas in, get completed sentences out as soon as they close."""

    def __init__(self, min_chars: int = 40) -> None:
        self.buffer = ""
        self.min_chars = min_chars

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        ready: list[str] = []
        while True:
            m = _SENTENCE_END.search(self.buffer)
            if not m:
                break
            candidate = self.buffer[: m.start()]
            if len(candidate.strip()) < self.min_chars and "\n" not in m.group(0):
                # Too short to speak on its own; wait for the next boundary.
                nxt = _SENTENCE_END.search(self.buffer, m.end())
                if not nxt:
                    break
                candidate = self.buffer[: nxt.start()]
                m = nxt
            ready.append(candidate)
            self.buffer = self.buffer[m.end():]
        return ready

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []

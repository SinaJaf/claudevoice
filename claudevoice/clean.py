"""Turn Claude's markdown reply into something that sounds natural when read aloud.

Kokoro reads exactly what it is given, so backticks, bullets, headers, URLs and
emoji all turn into noise unless they are stripped first. This module is
stdlib-only on purpose: the Stop hook imports it with the system python.
"""
from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_EMPTY_LINK = re.compile(r"\[\s*\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADER = re.compile(r"^\s{0,3}#{1,6}(?:\s+|(?=[^\d\s#]))")
_BULLET = re.compile(r"^\s*[-*+•]\s+", re.MULTILINE)
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
# Curly apostrophes: Kokoro reads the one in "dogs’ bowl" as a closing quote.
_QUOTE_MARK = re.compile("[‘’]")
# Arrows, shapes and keyboard signs left over once the ones with a spoken name are replaced.
_SIGN = re.compile("[\u2190-\u21ff\u2300-\u23ff\u25a0-\u25ff]")
_MULTI_SPACE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,!?;:])")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
# Paths, read by their last part: absolute or home-relative ones, ones with two or more
# directories, and dir/file.ext. A single slash between words ("and/or", "km/s") is no
# path; segments must start with a letter, dot or underscore so "3/4" is left alone.
_PATH = re.compile(
    r"(?<![\w/])(?:"
    r"(?:~|\.{1,2})?/(?:[\w.\-]+/)*[\w.\-]+"
    r"|(?:[A-Za-z_.][\w.\-]*/){2,}[\w.\-]+"
    r"|(?:[A-Za-z_.][\w.\-]*/)+[\w\-]+\.[A-Za-z]\w{0,4}\b"
    r")"
)
_FILE_LINE = re.compile(r"(\b[\w.\-]+\.(?:py|ts|tsx|js|jsx|rs|go|rb|md|json|toml|yaml|yml|sh|c|cpp|h|swift)):(\d+)\b")


def _clock(match: re.Match) -> str:
    """10:30 is "ten thirty" and 3:1 is "three to one"; left alone, Kokoro stops in the middle."""
    left, right = match.group(1), match.group(2)
    if len(right) != 2 or int(left) > 24 or int(right) > 59:
        return f"{left} to {right}"
    if right == "00":
        return left
    return f"{left} oh {int(right)}" if right[0] == "0" else f"{left} {right}"


def _version(match: re.Match) -> str:
    """0.5.1 is "0 point 5 point 1"; Kokoro reads it digit by digit, and loses 0.5.0 entirely."""
    return " point ".join(match.group(0).split("."))


def _commit(match: re.Match) -> str:
    return " ".join(match.group(0)[:7].upper())


_FILE_TYPES = "py|ts|tsx|js|jsx|rs|go|rb|md|json|toml|yaml|yml|sh|c|cpp|h|swift|txt|pdf|html|css|csv|wav|mp3|png|jpg|log|lock"


def _file_name(match: re.Match) -> str:
    """pacing.py is "pacing dot P Y". Left as written, voices guess at it, and Qwen3-TTS can lose the sentence."""
    return f"{match.group(1).replace('_', ' ')} dot {' '.join(match.group(2).upper())}"


# Short spoken replacements for symbols the voices would otherwise skip or mangle.
_SYMBOLS = [
    (re.compile(r"\b([Vv]ersion )v(?=\d)"), r"\1"),
    (re.compile(r"(?<![\w.])v(?=\d+\.\d+\.\d+\b)"), "version "),
    (re.compile(r"(?<![\d.])\d+\.\d+\.\d+\b(?!\.\d)"), _version),
    (re.compile(rf"\b([\w\-]+)\.({_FILE_TYPES})\b", re.IGNORECASE), _file_name),
    (re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b"), _commit),
    (re.compile(r"\bCtrl\s*\+\s*"), "control "),
    (re.compile(r"\bCmd\s*\+\s*"), "command "),
    (re.compile(r"\bAlt\s*\+\s*"), "alt "),
    (re.compile(r"\bShift\s*\+\s*"), "shift "),
    (re.compile("\u2318\\s*"), "command "),
    (re.compile("\u2303\\s*"), "control "),
    (re.compile("\u2325\\s*"), "option "),
    (re.compile("\u21e7\\s*"), "shift "),
    (re.compile("\\s*\u2192\\s*"), " to "),
    (re.compile(r"#L(?=\d)"), " line "),
    (re.compile(r"#(?=\d)"), " number "),
    (re.compile(r"\b(and)/(or)\b", re.IGNORECASE), r"\1 \2"),
    (re.compile(r"(?<=\w)\(s\)"), "s"),
    (re.compile(r"(?<![\d:.])(\d+):(\d+)(?![\d:])"), _clock),
    (re.compile(r"(?<=\d):(?=\d)"), " "),
    # A dash between numbers is a range; Kokoro runs "8-10" together as one word.
    (re.compile(r"(?<=\d)\s?–\s?(?=\d)"), " to "),
    (re.compile(r"(?<![\d\-.])(\d{1,3})-(\d{1,3})(?![\d\-])"), r"\1 to \2"),
    (re.compile(r"(?<=\d)\s+-\s+(?=\d)"), " minus "),
    # A dash between words, however it was typed; Kokoro drops a lone hyphen. A flag
    # ("--help") is no dash.
    (re.compile(r"(?<=\S)\s*—\s*(?=\S)|(?<=\S)\s+(?:-+|–)\s+(?=\S)|(?<=[a-z])--(?=[a-z])"), " — "),
    (re.compile(r"\b[Ee]\.\s?[Gg]\.,?"), "for example,"),
    (re.compile(r"\b[Ii]\.\s?[Ee]\.,?"), "that is,"),
    (re.compile(r"\bFigs\.(?=\s*\d)"), "figures"),
    (re.compile(r"\bFig\.(?=\s*\d)"), "figure"),
    (re.compile(r"\b[Ee]qs?\.(?=\s*[\d(])"), "equation"),
    (re.compile(r"\bSec\.(?=\s*\d)"), "section"),
    (re.compile(r"\bCh\.(?=\s*\d)"), "chapter"),
    (re.compile(r"\bVol\.(?=\s*\d)"), "volume"),
    (re.compile(r"\bpp\.(?=\s*\d)"), "pages"),
    (re.compile(r"\bp\.(?=\s*\d)"), "page"),
    (re.compile(r"\bNos?\.(?=\s*\d)"), "number"),
    (re.compile(r"\bvs\."), "versus"),
    (re.compile(r"\bapprox\."), "approximately"),
    (re.compile(r"\bcf\."), "compare"),
    (re.compile(r"\bet al\."), "et al"),
    (re.compile(r"\betc\.(?=,|\s+[a-z])"), "et cetera"),
    (re.compile(r"\s*->\s*"), " to "),
    (re.compile(r"\s*=>\s*"), " gives "),
    (re.compile(r"\s*&&\s*"), " and "),
    (re.compile(r"\s*\|\|\s*"), " or "),
    (re.compile(r"(?<=\w)/(?=\w)"), " slash "),
    (re.compile(r"\s*%\s*"), " percent "),
    (re.compile(r"(?<![\w.])@\s*"), "at "),
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


_PART = r"chapter|section|part|book|volume|appendix|unit|lesson|article|module|act|scene"
_ROMAN = r"(?=[IVXLC])C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
# A lone C or L, and anything after "appendix", is a letter: "Section C", "Appendix V".
_PART_ROMAN = re.compile(rf"\b((?i:(?!appendix)(?:{_PART})))\s+(?![CL]\b)({_ROMAN})\b")
_ROMAN_ITEM = re.compile(rf"^\s*({_ROMAN})[.)]\s+(?=[A-Z])")
_NUMBER = r"\d{1,3}(?:\.\d+)*"
_NUMBER_ITEM = re.compile(rf"^\s*({_NUMBER})[.)]\s+(?=\S)")
_NUMBER_START = re.compile(rf"^\s*({_NUMBER})\s+(?=\S)")
_LETTER_ITEM = re.compile(r"^\s*([A-Z])[.)]\s+(?=[A-Z])")
_PART_START = re.compile(rf"^\s*((?i:{_PART})\s+(?:{_NUMBER}|[A-Z]))\.?\s+(?=[^\s:—–\-])")
_MAX_HEADING = 80


def _roman(numeral: str) -> int:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    for i, c in enumerate(numeral):
        value = values[c]
        total += -value if i + 1 < len(numeral) and values[numeral[i + 1]] > value else value
    return total


_NUMBER_WORDS = ("One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen "
                 "Fifteen Sixteen Seventeen Eighteen Nineteen Twenty").split()


def _said(number: str) -> str:
    """A list or section number as a word: at the start of a line, voices can drop the digit."""
    return _NUMBER_WORDS[int(number) - 1] if number.isdigit() and 1 <= int(number) <= 20 else number


def _spoken_line(line: str) -> str:
    """A heading's or list item's number, kept and marked off from its title. Read straight on,
    "8 Reports and Analysis" sounds like a count of reports; a reader says "Eight", waits, goes on.
    In a sentence ("we have 8 reports") a number stays where it is. A heading is followed by
    a blank line, for the longer pause."""
    header = _HEADER.match(line)
    text = line[header.end():] if header else line
    if not text.strip():
        return ""
    # Sentences end in punctuation; a short line starting with a capital that does not is a title.
    def titled(rest: str) -> bool:
        return bool(rest) and (header is not None or (
            rest[0].isupper() and len(rest) <= _MAX_HEADING and not _ENDS_WITH_PUNCT.search(rest)))

    marked = False
    if m := _NUMBER_ITEM.match(text):
        text = f"{_said(m.group(1))}. {text[m.end():]}"
    elif (m := _ROMAN_ITEM.match(text)) and titled(text[m.end():]):
        text = f"{_said(str(_roman(m.group(1))))}. {text[m.end():]}"
        marked = True
    elif (m := _NUMBER_START.match(text)) and titled(text[m.end():]):
        text = f"{_said(m.group(1))}. {text[m.end():]}"
        marked = True
    elif (m := _LETTER_ITEM.match(text)) and titled(text[m.end():]):
        text = f"{m.group(1)}: {text[m.end():]}"
        marked = True
    elif (m := _PART_START.match(text)) and titled(text[m.end():]):
        text = f"{m.group(1)}. {text[m.end():]}"
        marked = True
    if header or marked:
        return text.rstrip() + "\n"
    return text


def _inline_code(match: re.Match) -> str:
    """Names in code read as words: `reload-plugins` is "reload plugins", `--help` is "dash dash help"."""
    code = match.group(1)
    dashes = re.match(r"-+", code)
    code = "dash " * len(dashes.group(0)) + code[dashes.end():] if dashes else code
    return re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])|(?<=\w)_(?=\w)", " ", code)


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
    out = _HTML_COMMENT.sub("", out)
    out = _IMAGE.sub("", out)
    out = _EMPTY_LINK.sub("", out)
    out = _TABLE_RULE.sub("", out)
    out = _TABLE_ROW.sub(_table_row_to_speech, out)
    out = _LINK.sub(r"\1", out)
    out = _BARE_URL.sub(" a link ", out)
    out = _HTML_TAG.sub("", out)
    out = _HR.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _BULLET.sub("", out)
    out = _EMPHASIS.sub(r"\2", out)  # first, so a bold "8 Reports" is seen as a heading
    out = _QUOTE_MARK.sub("'", out)
    out = _PART_ROMAN.sub(lambda m: f"{m.group(1)} {_roman(m.group(2))}", out)
    out = "\n".join(_spoken_line(line) for line in out.split("\n"))
    out = _end_lines(out)
    out = _FILE_LINE.sub(lambda m: f"{m.group(1)} line {m.group(2)}", out)
    out = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], out)
    out = _INLINE_CODE.sub(_inline_code, out)
    out = _EMOJI.sub("", out)
    for pattern, repl in _SYMBOLS:
        out = pattern.sub(repl, out)
    out = _SIGN.sub("", out)
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


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(“])|\n+")
# A period here does not end a sentence: "Dr. Smith", "J. Smith", "U.S. law".
_NOT_THE_END = re.compile(r"(?:\b(?:Dr|Mr|Mrs|Ms|Prof|St|Sr|Jr|Gen|Col|Capt|Lt|Sgt|Rev|Gov|Sen|Rep)|\b[A-Z])\.$")


def _next_end(text: str, pos: int = 0) -> re.Match | None:
    while (m := _SENTENCE_END.search(text, pos)):
        if "\n" in m.group(0) or not _NOT_THE_END.search(text[: m.start()]):
            return m
        pos = m.end()
    return None


def _sentences(line: str) -> list[str]:
    parts, pos = [], 0
    while (m := _next_end(line, pos)):
        parts.append(line[pos : m.start()])
        pos = m.end()
    parts.append(line[pos:])
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str, *, min_chars: int = 40) -> list[str]:
    """Split text into sentence-ish chunks that are long enough to sound natural.

    Very short sentences are merged with the next one so Kokoro doesn't produce choppy
    single-word utterances, but a line never runs into the next: a heading stays apart from
    the text below it. A chunk that ends a line keeps its line break (two for a paragraph's
    end), which is how the voice knows to wait longer there (pacing.pause_after).
    """
    chunks: list[str] = []
    for block in re.split(r"(\n+)", text.strip()):
        if not block.strip():
            if chunks and block:
                chunks[-1] += "\n\n" if len(block) > 1 else "\n"
            continue
        buf = ""
        kept: list[str] = []
        for part in _sentences(block):
            buf = f"{buf} {part}".strip() if buf else part
            if len(buf) >= min_chars:
                kept.append(buf)
                buf = ""
        if buf:
            if kept and len(buf) < min_chars // 2:
                kept[-1] = f"{kept[-1]} {buf}"
            else:
                kept.append(buf)
        chunks.extend(kept)
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
            m = _next_end(self.buffer)
            if not m:
                break
            candidate = self.buffer[: m.start()]
            if len(candidate.strip()) < self.min_chars and "\n" not in m.group(0):
                # Too short to speak on its own; wait for the next boundary.
                nxt = _next_end(self.buffer, m.end())
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

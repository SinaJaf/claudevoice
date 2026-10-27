"""Turn a file, a piece of one, or selected text into text to read aloud.

Markdown and plain text are read as written: lines wrapped inside a paragraph are
joined back first (the cleanup that follows treats each line as one spoken line),
then the markdown is cleaned the same way Claude's replies are. A PDF's text comes
from PDFium; its lines are joined back into paragraphs, running headers, footers and
page numbers are dropped, and words hyphenated across lines are mended.
"""
from __future__ import annotations

import collections
import math
import os
import re
import unicodedata
from dataclasses import dataclass

from .clean import markdown_to_speech

MAX_BYTES = 50_000_000  # bigger text files are refused rather than read for hours


class DocumentError(Exception):
    """A file that cannot be read aloud; the message says why, for the person."""


@dataclass
class Document:
    title: str
    text: str  # cleaned, one paragraph or spoken line per line
    pages: int = 0  # a PDF's page count; 0 for text


def load(path: str, *, start: int | None = None, end: int | None = None) -> Document:
    """Read `path` from line (or, for a PDF, page) `start` to `end`, both 1-based and inclusive."""
    path = os.path.abspath(os.path.expanduser(path))
    title = os.path.basename(path)
    if not os.path.isfile(path):
        raise DocumentError(f"{title} was not found")
    if path.lower().endswith(".pdf"):
        return _load_pdf(path, title, start, end)
    if os.path.getsize(path) > MAX_BYTES:
        raise DocumentError(f"{title} is too large to read aloud")
    with open(path, "rb") as f:
        raw = f.read()
    if b"\0" in raw[:8192]:
        raise DocumentError(f"{title} is not a text file")
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if start is not None or end is not None:
        first = max(1, start or 1)
        if first > len(lines):
            raise DocumentError(f"{title} has only {len(lines)} lines")
        lines = lines[first - 1 : end]
    elif lines and lines[0].strip() == "---":
        lines = _drop_front_matter(lines)
    spoken = prepare_text("\n".join(lines))
    if not spoken:
        raise DocumentError(f"{title} has nothing to read")
    return Document(title, spoken)


def prepare_text(text: str) -> str:
    """Selected or written text (markdown or plain) as it should be spoken."""
    text = _LINK_DEFINITION.sub("", text)
    return markdown_to_speech(unwrap(text))


# ---- markdown and plain text ---------------------------------------------------------

_LINK_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s+\S+.*$", re.MULTILINE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_BLOCK_START = re.compile(r"^\s*(#{1,6}\s|[-*+•]\s|\d+[.)]\s|>|\||```|~~~|<)|^\s*([-*_]\s*){3,}$")
_LIST_ITEM = re.compile(r"^\s*([-*+•]|\d+[.)])\s")
_QUOTE = re.compile(r"^\s*>\s?")


def unwrap(text: str) -> str:
    """Join lines wrapped inside a paragraph or a list item, as markdown renders them.

    Headings, list items, quotes, tables, fences and blank lines start a new line;
    code inside fences is left alone.
    """
    out: list[str] = []
    in_fence = False
    joinable = False  # the last line out is paragraph or list-item text
    was_quote = False
    for line in text.split("\n"):
        is_quote = not in_fence and bool(_QUOTE.match(line))
        if is_quote:
            line = _QUOTE.sub("", line, count=1)  # a quote's lines join like a paragraph's
        if is_quote != was_quote:
            joinable = False
        was_quote = is_quote
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            joinable = False
            continue
        if in_fence:
            out.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            out.append("")
            joinable = False
            continue
        if joinable and not _BLOCK_START.match(line):
            out[-1] = out[-1].rstrip() + " " + stripped
            continue
        out.append(line)
        joinable = not _BLOCK_START.match(line) or bool(_LIST_ITEM.match(line))
    return "\n".join(out)


def _drop_front_matter(lines: list[str]) -> list[str]:
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() in ("---", "..."):
            return lines[i + 1 :]
    return lines


# ---- PDF ---------------------------------------------------------------------------

_PAGE_NUMBER = re.compile(r"^(page\s*)?[\divxlc]+(\s*(of|/)\s*\d+)?$|^[-–—]\s*\d+\s*[-–—]$", re.IGNORECASE)
# Dot leaders in a table of contents, and glyphs PDFium could not map to a character.
_LEADER = re.compile(r"(?:[.·…_�] ?){4,}")
_UNREADABLE = re.compile(r"[�\x00-\x08\x0b\x0c\x0e-\x1f]")
_HYPHENS = "-‐‑­"
_CLOSING = _HYPHENS + ".!?:;,…\"')]"


def _load_pdf(path: str, title: str, start: int | None, end: int | None) -> Document:
    pages = pdf_pages(path)
    count = len(pages)
    first = max(1, start or 1)
    if first > count:
        raise DocumentError(f"{title} has only {count} pages")
    last = min(count, end or count)
    # Running headers and footers are found across every page, then the range is taken.
    pages = _drop_running_lines(pages)[first - 1 : last]
    text = "\n".join(reflow([line for page in pages for line in page]))
    if not text.strip():
        raise DocumentError(f"{title} has no text to read; it may be scanned pages")
    return Document(title, markdown_to_speech(text), count)


def pdf_pages(path: str) -> list[list[str]]:
    """Each page's lines, in reading order as PDFium finds it."""
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(path)
    except Exception as exc:  # noqa: BLE001 - a damaged or locked PDF
        raise DocumentError(f"{os.path.basename(path)} could not be opened: {exc}") from exc
    pages: list[list[str]] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            # NFKC turns ligatures (ﬁ) and other presentation forms back into plain letters.
            text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
            text = _UNREADABLE.sub("", _LEADER.sub(" ", text))
            pages.append([line.strip() for line in text.split("\n")])
    finally:
        doc.close()
    return pages


def _drop_running_lines(pages: list[list[str]], edge: int = 3) -> list[list[str]]:
    """Remove page numbers, and lines repeated near the top or bottom of most pages
    (of three pages at least: a shorter document has none worth finding)."""
    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.lower())

    def edges(lines: list[str]) -> set[int]:
        filled = [i for i, line in enumerate(lines) if line]
        return set(filled[:edge] + filled[-edge:])

    seen: collections.Counter[str] = collections.Counter()
    for lines in pages:
        seen.update({key(lines[i]) for i in edges(lines)})
    needed = max(3, math.ceil(0.5 * len(pages)))
    cleaned = []
    for lines in pages:
        near_edge = edges(lines)
        cleaned.append([
            line for i, line in enumerate(lines)
            if not (i in near_edge and (_PAGE_NUMBER.match(line) or seen[key(line)] >= needed))
        ])
    return cleaned


def reflow(lines: list[str]) -> list[str]:
    """Join a PDF's lines back into paragraphs.

    A paragraph ends at a blank line or at a line much shorter than the text's usual
    width (its last line). A short line with no closing punctuation that starts with a
    capital or a number is a heading, on a line of its own. A word broken with a
    hyphen at a line's end is joined back when the next line goes on in lower case.
    """
    widths = sorted(len(line) for line in lines if len(line) > 20)
    usual = widths[int(len(widths) * 0.75)] if widths else 60
    paragraphs: list[str] = []
    current = ""
    for line in lines:
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            continue
        short = len(line) < 0.6 * usual
        is_heading = short and line[-1] not in _CLOSING and (line[0].isupper() or line[0].isdigit())
        if is_heading and not (current and current[-1] in _HYPHENS):
            if current:
                paragraphs.append(current)
                current = ""
            paragraphs.append(line)
            continue
        if not current:
            current = line
        elif current[-1] in _HYPHENS and len(current) > 1 and current[-2].isalpha() and line[0].islower():
            current = current[:-1] + line
        else:
            current = current + " " + line
        if short and line[-1] not in _HYPHENS:
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return paragraphs


# ---- reading in pieces ---------------------------------------------------------------

_BREAKS = ("; ", ": ", ", ", " ")


def split_long(sentences: list[str], limit: int = 400) -> list[str]:
    """Break run-on pieces (a table of contents, a list with no full stops) at a
    semicolon, colon, comma or space, so a pause lands near where it was pressed."""
    out: list[str] = []
    for s in sentences:
        while len(s) > limit:
            cut = next((i + 1 for sep in _BREAKS if (i := s.rfind(sep, limit // 2, limit)) > 0), limit)
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out


def group(sentences: list[str], limit: int) -> list[str]:
    """Join sentences into pieces of up to `limit` characters (for cloud voices, which
    take one request per piece)."""
    pieces: list[str] = []
    buf = ""
    for s in sentences:
        if buf and len(buf) + 1 + len(s) > limit:
            pieces.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        pieces.append(buf)
    return pieces

"""PDF → ln-vox novels-folder converter (`lnvox ingest-pdf`).

Writes the layout `lnvox ingest` reads — the same contract as `ingest-epub`:

    <output_dir>/
        01-the-first-lecture.txt     (first line = chapter title)
        02-...
        images/cover.jpg             (the cover page's image)
        .epub_meta.json              (title, authors, cover, chapter → page /
                                      EPUB-spine map)

plus, optionally, an EPUB built from the cleaned chapters (`epub_writer`).
That EPUB is the book's sync target: Stage 6 aligns the rendered beats onto
it, and since its text IS the chapters' text, the alignment is near-exact.
The sidecar is named after that EPUB's contract: `lnvox ingest` copies its
cover into `00_book_meta.json` for Stage 5, and Stage 6 reads its
chapter → XHTML map.

Only the PDF's text layer is read — a scan without OCR has none (run it
through OCR first, e.g. ocrmypdf). Chapters come from `--chapter FIRST-LAST:
Title` page ranges, else the PDF's top-level bookmarks, else the whole
document (or `--pages`) is one chapter. Each chapter's text is cleaned of
what a narrator must not read — running heads/feet, page numbers, scan specks
at the page edges, the chapter title repeated at the top of its first page —
and its line breaks are undone into paragraphs, re-joining words hyphenated
across lines. Where the printed page numbers jump (pages missing from a scan
or an excerpt) a paragraph break keeps the fragments on either side apart.
For a born-digital PDF, `font_filter` reads each text run's font instead
and keeps only the running text (`read_page`): headings and indents become
paragraph breaks, and what the text loses comes back for the reader app —
figures and tables rendered and placed before their first mention (in the
EPUB, and after the cover in the m4b's pictures), endnotes from the notes
section (`pdf_notes`) tied to their markers as `footnote` visual elements
that Stage 6 times to the beat reading the marker.

The cover is the cover page rendered whole — a publisher's cover is often
layered images, vectors and text — else, without pypdfium2, its largest
image (a scan's page image). pypdf, Pillow and pypdfium2 are imported lazily
and aren't locked dependencies: run this as `uv run --with "pypdf[image]"
--with pypdfium2 lnvox ingest-pdf …`. A re-extraction first removes the
chapter files and the renders the previous run listed in `.epub_meta.json`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import statistics
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from lnvox.ingest.epub import _remove_previous_chapter_files, _slugify
from lnvox.ingest.epub_writer import EpubChapter, EpubFigure, EpubNote, is_generated, write_epub

SIDECAR = ".epub_meta.json"
COVER_MAX_PX = 1600  # long side of the embedded cover — plenty for a player or reader

_SOFT_HYPHEN = "\u00ad"
_LIGATURES = str.maketrans(
    {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}
)
# Some PDFs draw a "fi"/"fl" ligature as one glyph followed by a gap, which
# extraction turns into spaces: "speci\ufb01 c", "dif\ufb01  cult", "ef\ufb01  -" at a line
# end. English words essentially never end in "fi"/"fl", so the gap is closed;
# the "ff" family is left alone ("o\ufb00 the" is a real word boundary).
_LIGATURE_GAP_RE = re.compile("([\ufb01\ufb02])[ \t]+(?=[a-z,.;:!?)\u2019\u201d-])")
_PAGE_NUMBER_RE = re.compile(r"\d{1,4}")
# Strict numeral grammar, so edge words like "did" or "mild" don't qualify.
_ROMAN_RE = re.compile(r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", re.IGNORECASE)
# Rule-line debris glued to the start of a line ("------ - -----there are").
_RULE_PREFIX_RE = re.compile(r"^[-_=~][-_=~\s]{3,}")
_SENTENCE_END = (".", "?", "!", ":", '"', "'", "\u201d", "\u2019")
# Removed from finished paragraphs: soft hyphens and `filter_page`'s line marks.
_SENTINELS = re.compile("[\u00ad\ue000\ue001]")
EDGE_LINES = 2  # lines at the top / bottom of a page that may be running heads


@dataclass
class ChapterSpec:
    first_page: int  # 1-based, inclusive
    last_page: int
    title: str = ""


@dataclass
class PdfMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    pages_without_text: list[int] = field(default_factory=list)
    cover_image: str | None = None  # relative to the output folder
    epub: Path | None = None
    figures: int = 0  # rendered into the EPUB (`font_filter`)
    notes: int = 0  # tied to their markers (`font_filter`)


def parse_page_range(spec: str) -> tuple[int, int]:
    """`N` or `N-M` (1-based, inclusive) → (first, last)."""
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", spec)
    if not m:
        raise ValueError(f"not a page range: {spec!r} (expected N or N-M)")
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if first < 1 or last < first:
        raise ValueError(f"bad page range {spec!r}: pages start at 1 and FIRST <= LAST")
    return first, last


def parse_chapter_spec(spec: str) -> ChapterSpec:
    """`5-12:Title`, `5-12` or `7:Title` — the title may itself contain ':'."""
    rng, _, title = spec.partition(":")
    first, last = parse_page_range(rng)
    return ChapterSpec(first, last, title.strip())


# ----- text cleanup (pure functions) -------------------------------------------


def _key(line: str) -> str:
    """Comparison key for running heads / titles: letters only, lowercased."""
    return re.sub(r"[^a-z]", "", line.lower())


def _is_edge_junk(line: str) -> bool:
    """A page number, a lone roman numeral, or scan specks."""
    s = line.strip()
    if _FIG_OPEN in s:  # a figure at the top or foot of the page
        return False
    return bool(
        _PAGE_NUMBER_RE.fullmatch(s)
        or (s and _ROMAN_RE.fullmatch(s))
        or not re.search(r"[A-Za-z]{2}", s)
    )


def _page_lines(text: str) -> list[str]:
    text = _LIGATURE_GAP_RE.sub(r"\1", text).translate(_LIGATURES)
    return [_RULE_PREFIX_RE.sub("", ln.strip()) for ln in text.splitlines() if ln.strip()]


def _running_keys(pages: list[list[str]]) -> set[str]:
    """Keys of lines that recur at the page edges: running heads and feet.

    Counted per chapter, since a book's recto head is usually the chapter
    title. Digits are ignored by the key, so "12 COLLECTED LECTURES"
    and "COLLECTED LECTURES 13" count as one head.
    """
    counts: dict[str, int] = {}
    for lines in pages:
        edge = set(lines[:EDGE_LINES]) | set(lines[-EDGE_LINES:])
        for k in {_key(ln) for ln in edge}:
            if len(k) >= 3:
                counts[k] = counts.get(k, 0) + 1
    threshold = max(2, math.ceil(0.2 * len(pages)))
    return {k for k, n in counts.items() if n >= threshold}


def _trim_edges(lines: list[str], running: set[str]) -> tuple[list[str], int | None]:
    """Drop heads/feet/junk from both page edges → (lines, printed page no.)."""
    page_no = None
    lines = list(lines)
    for end in (0, -1):
        while lines and (_is_edge_junk(lines[end]) or _key(lines[end]) in running):
            removed = lines.pop(end).strip()
            if _PAGE_NUMBER_RE.fullmatch(removed):
                page_no = int(removed)
    return lines, page_no


# A bookmark title often carries a number or a date the page doesn't print:
# "I. The First Lecture (May 1950)" over a page headed "The First Lecture".
_TITLE_NUMBERING_RE = re.compile(r"^\s*(?:chapter\s+)?(?:[ivxlcdm]+|\d+)[.:)\-]?\s+", re.IGNORECASE)
_TITLE_TRAILER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _drop_title_lines(lines: list[str], title: str) -> list[str]:
    """Drop the chapter title printed at the top of its first page.

    Fuzzy, so an OCR'd "The Flrst Lecture" still matches the title, and the
    title may wrap over several lines. Matched with and without the title's
    leading numbering and trailing parenthetical.
    """
    core = _TITLE_TRAILER_RE.sub("", _TITLE_NUMBERING_RE.sub("", title))
    targets = {k for k in (_key(title), _key(core)) if k}
    if not targets:
        return lines
    longest = max(len(t) for t in targets)
    acc = ""
    for i, line in enumerate(lines[:4]):
        acc += _key(line)
        if any(SequenceMatcher(None, acc, t).ratio() >= 0.85 for t in targets):
            return [ln for ln in lines[:i] if _FIG_LINE_RE.fullmatch(ln)] + lines[i + 1 :]
        if len(acc) > longest + 5:
            break
    return lines


def _join(buf: str, line: str) -> str:
    """Append `line` to a paragraph, re-joining a word hyphenated across lines."""
    if not buf:
        return line
    if buf.endswith(_SOFT_HYPHEN):
        return buf.rstrip(_SOFT_HYPHEN).rstrip("-") + line
    if buf.endswith("-") and buf[-2:-1].isalpha():
        # Before a lowercase word it's a line-break hyphen ("keep-" / "ers");
        # before a capital it's part of the word ("Anglo-" / "Saxon").
        return (buf[:-1] if line[:1].islower() else buf) + line
    return f"{buf} {line}"


# A table-of-contents entry: a dot leader (". . . ." or "....") and a page
# number. LaTeX's per-chapter mini contents come out in the body font.
_LEADER_RE = re.compile(r"(?:\.[  ]?){4,}\s*\d+")
_CONTENTS_HEADING_RE = re.compile(
    r"(?:table of )?contents|table des mati[eè]res|sommaire|[ií]ndice|inhalt(?:sverzeichnis)?",
    re.IGNORECASE,
)
# LaTeX's \ldots, extracted as three spaced periods.
_SPACED_ELLIPSIS_RE = re.compile(r"\.(?:[  ]\.){2}")
# C0 controls: a maths glyph with no Unicode mapping comes out as NUL, which
# XML (the EPUB) can't carry.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _edit_paragraphs(
    paragraphs: list[str], drop: Iterable[str] = (), rewrite: Iterable[tuple[str, str]] = ()
) -> list[str]:
    """Each `drop` regex removed, then each `(regex, replacement)` of
    `rewrite` applied (`re.sub`), in every paragraph. A blank line in a
    result starts a new paragraph; paragraphs left empty go."""
    edits = [(pattern, " ") for pattern in drop] + list(rewrite)
    if not edits:
        return paragraphs
    for pattern, replacement in edits:
        paragraphs = [re.sub(pattern, replacement, p) for p in paragraphs]
    split = (re.sub(r"\s+", " ", q).strip() for p in paragraphs for q in p.split("\n\n"))
    return [q for q in split if q]


def _is_contents(paragraph: str) -> bool:
    """A paragraph of table-of-contents entries, not prose."""
    leaders = _LEADER_RE.findall(paragraph)
    return len(leaders) >= 2 or (bool(leaders) and len(paragraph) < 150)


def clean_chapter(
    pages: list[str],
    title: str = "",
    drop: Iterable[str] = (),
    rewrite: Iterable[tuple[str, str]] = (),
) -> str:
    """One chapter's raw page texts → narration-ready paragraphs.

    Table-of-contents paragraphs (dot leaders and page numbers) are left
    out with a "Contents" heading right before them, LaTeX's spaced ". . ."
    becomes "…", and each `drop` regex is removed from every paragraph
    (book-specific furniture such as margin references). Each `rewrite`
    is a `(regex, replacement)` for `re.sub`; a blank line in the result
    starts a new paragraph.
    """
    raw = [_page_lines(p) for p in pages]
    running = _running_keys(raw)
    trimmed, numbers = [], []
    for lines in raw:
        lines, page_no = _trim_edges(lines, running)
        trimmed.append(lines)
        numbers.append(page_no)
    if trimmed:
        trimmed[0] = _drop_title_lines(trimmed[0], title)

    widths = [len(ln) for lines in trimmed for ln in lines if len(ln) >= 20]
    width = statistics.median(widths) if widths else 60

    paragraphs: list[str] = []
    buf = ""
    # Figure marks met mid-paragraph (`read_page`): they go after it, so a
    # figure never splits a sentence or a hyphenated word.
    pending: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf or pending:
            paragraphs.append(" ".join([buf, *pending]))
        buf = ""
        pending.clear()

    prev_no: int | None = None
    for lines, page_no in zip(trimmed, numbers):
        # A jump in printed page numbers = missing pages: don't glue the
        # sentence fragments on either side into one paragraph.
        if buf and prev_no is not None and page_no is not None and page_no != prev_no + 1:
            flush()
        if page_no is not None:
            prev_no = page_no
        for line in lines:
            if _FIG_LINE_RE.fullmatch(line):
                pending.append(line)
                continue
            if line.startswith(HEADING):  # marked by `filter_page`
                flush()
                paragraphs.append(line)
                continue
            if line.startswith(PARAGRAPH):
                flush()
            buf = _join(buf, line)
            bare = _TOKEN_RE.sub("", line)
            if bare.endswith(_SENTENCE_END) and len(bare) < 0.85 * width:
                flush()
    flush()
    cleaned = [
        re.sub(r"\s+", " ", _CONTROL_RE.sub("", _SENTINELS.sub("", p))).strip() for p in paragraphs
    ]
    cleaned = _edit_paragraphs(cleaned, drop, rewrite)
    kept: list[str] = []
    for p in cleaned:
        if _is_contents(p):
            if kept and _CONTENTS_HEADING_RE.fullmatch(kept[-1]):
                kept.pop()
            continue
        if p:
            kept.append(_SPACED_ELLIPSIS_RE.sub("…", p))
    return "\n\n".join(kept)


# ----- font-aware body filter (born-digital PDFs) ---------------------------------

# Sentinels `filter_page` puts at the start of a line; `clean_chapter`
# turns them into paragraph breaks.
HEADING = "\ue000"  # a heading: a paragraph of its own
PARAGRAPH = "\ue001"  # a paragraph starts here


# Marks `read_page` leaves in a page's text for `extract_pdf`. They ride
# through `clean_chapter` and `_split_marks` takes them out: a note marker
# (holding its original text, so one that no note claims can be put back)
# and the place a figure stood.
_MARK_OPEN, _MARK_CLOSE = chr(0xE002), chr(0xE003)
_FIG_OPEN, _FIG_CLOSE = chr(0xE004), chr(0xE005)
_TOKEN_RE = re.compile(
    f"{_MARK_OPEN}([^{_MARK_CLOSE}]*){_MARK_CLOSE}|{_FIG_OPEN}([^{_FIG_CLOSE}]*){_FIG_CLOSE}"
)
_FIG_LINE_RE = re.compile(rf"(?:\s*{_FIG_OPEN}[^{_FIG_CLOSE}]*{_FIG_CLOSE})+\s*")


class Fragment(NamedTuple):
    """A run of text pypdf reports under one font state."""

    font: str  # PostScript name, subset prefix stripped ("SourceSerif-Italic")
    size: float  # effective size, points
    x: float  # start of the run
    y: float  # baseline
    text: str


_FONT_SUFFIX_RE = re.compile(r"(?:MT|PS|Std|Pro|LT)+$")
# A figure / table label, which starts a caption (and a table's cells).
_CAPTION_LABEL_RE = re.compile(
    r"\s*(?:figure|fig\.|table|example|plate|map|chart)\s+\d+(?:[.\-]\d+)*[a-z]?\b", re.IGNORECASE
)
_SMALL_MARKER_RE = re.compile(r"\s*\d{1,3}(?=\s|$)")
_LIST_ITEM_RE = re.compile(r"\s*\d{1,2}\.\s+\S")
# A note marker raised by text rise at the body's own size, which pypdf
# doesn't report: 1-3 digits right after a sentence ends, before the next
# one starts or the line ends. A word of two lowercase letters (or "1960s")
# must end the sentence, else a closing quote or bracket, so "Op. 27 No. 2",
# "p. 12" and pitch names like "C4" stay untouched.
_NOTE_MARKER_RE = re.compile(
    r"(?:(?<=[0-9a-z][a-z][.?!])|(?<=[.?!][\"'\u2019\u201d)])|(?<=\)[.?!]))"
    r"[ \t]?\d{1,3}(?=[ \t]*(?:\n|$)|[ \t]+[\"\u201c(A-Z])"
)


def _family(font: str) -> str:
    """'SourceSerif-Italic', 'TimesNewRomanPS-BoldMT', 'Arial,Bold' → the family."""
    return _FONT_SUFFIX_RE.sub("", re.split(r"[-,]", font, maxsplit=1)[0])


def body_style(pages: Iterable[list[Fragment]]) -> tuple[str, float, float] | None:
    """The (family, size) carrying the most characters — the running text —
    and its share of all characters. None without text."""
    chars: Counter[tuple[str, float]] = Counter()
    for frags in pages:
        for f in frags:
            if n := len(f.text.strip()):
                chars[(_family(f.font), round(f.size * 2) / 2)] += n
    if not chars:
        return None
    (family, size), n = chars.most_common(1)[0]
    return family, size, n / sum(chars.values())


@dataclass
class FigureZone:
    """A figure or table `read_page` took out of the running text."""

    label: str  # as printed: "Figure 2.1", "Table 9.3"
    frags: list[Fragment]  # its text on the page: lettering, label, caption, cells
    token: str  # its id in the page text's figure mark
    label_at: int = 0  # the label's index in `frags`
    caption: str = ""  # the label and the caption printed with it
    caption_top: float = 0.0  # the label line's top, user space
    caption_bottom: float = 0.0  # the caption's last line's bottom
    caption_end: int = 0  # `frags[label_at:caption_end]` is the caption

    @property
    def drawn_below(self) -> bool:
        """A table's caption heads it; a figure's sits under the drawing."""
        return self.label.lower().startswith("table")

    def split_lettering(self, family: str) -> list[Fragment]:
        """Take off the runs after the caption that aren't in the body
        family: the lettering of a figure set right after this one."""
        k = len(self.frags)
        while k > self.label_at + 1 and _family(self.frags[k - 1].font) != family:
            k -= 1
        lead, self.frags = self.frags[k:], self.frags[:k]
        return lead


@dataclass
class PageText:
    text: str  # with note-marker and figure marks (`_TOKEN_RE`)
    figures: list[FigureZone]
    kept: list[Fragment]  # the text kept: a figure's crop stops at it


def filter_page(frags: list[Fragment], family: str, size: float) -> str:
    """One page's fragments → its text, keeping the running text only.

    Dropped: a figure or table — its "Figure 2.1" / "Table 3" label, all
    that follows (caption, cells) until the running text resumes, and all
    between the last body line and the label (lettering, cells set before
    it) — and any text in another family at another size (running heads,
    figure lettering). Kept: text inline on a body line (a symbol, a term
    set in another font), the body family at a smaller size (block quotes),
    and, on a page with running text, lines in another family at body size
    or larger (section headings, rejoined when set in two styles or
    wrapped), marked as their own paragraph. Note markers are removed.

    A line starts a paragraph when it is indented from its margin, when it
    switches between body and block-quote size, or when it is a numbered
    item ("3. ") after a sentence end.
    """
    return _TOKEN_RE.sub("", read_page(frags, family, size).text)


def read_page(frags: list[Fragment], family: str, size: float, page_no: int = 0) -> PageText:
    """`filter_page`, keeping what it found: the text marks each note marker
    and each figure's place, and every figure comes back with the runs of
    text that make it up, so it can be rendered."""

    def is_body(f: Fragment | None) -> bool:
        return f is not None and _family(f.font) == family and abs(f.size - size) <= 0.5

    def on_body_line(other: Fragment | None, f: Fragment) -> bool:
        return is_body(other) and abs(other.y - f.y) < 0.6 * size

    starts = [i == 0 or frags[i - 1].text.endswith("\n") for i in range(len(frags))]
    following: list[Fragment | None] = []
    upcoming = None
    for f in reversed(frags):
        following.append(upcoming)
        if f.text.strip():
            upcoming = f
    following.reverse()
    # Each size class's left margin: the x most of its lines start at.
    xs: dict[bool, Counter[int]] = {True: Counter(), False: Counter()}
    for f, start in zip(frags, starts):
        if start and f.text.strip() and _family(f.font) == family and f.size < size + 0.5:
            xs[is_body(f)][round(f.x)] += 1
    margin = {cls: c.most_common(1)[0][0] for cls, c in xs.items() if c}

    # A page without running text is a title page, a part title, a figure:
    # its display lines are not headings.
    has_body = any(is_body(f) and f.text.strip() for f in frags)

    out: list[str] = []
    origin: list[int] = []  # out[k] came from frags[origin[k]]; -1: added here
    kept: list[int] = []
    zones: list[FigureZone] = []
    zone: FigureZone | None = None  # the figure being read
    body_end = 0  # len(out) after the last body-size line
    heading: tuple[int, Fragment] | None = None  # the heading just emitted
    prev: Fragment | None = None
    prev_class: bool | None = None  # was the last running-text run body-size?
    last_char = ""

    def emit(text: str, i: int = -1) -> None:
        out.append(text)
        origin.append(i)

    def keep(i: int, start: bool) -> None:
        nonlocal prev_class, last_char
        f = frags[i]
        cls = is_body(f)
        text = f.text
        if start and (
            (prev_class is not None and cls != prev_class)
            or 0.4 * size <= f.x - margin.get(cls, f.x) <= 3 * size
            or (_LIST_ITEM_RE.match(text) and last_char in _SENTENCE_END)
        ):
            text = PARAGRAPH + text.lstrip(" \t")
        emit(text, i)
        kept.append(i)
        prev_class = cls
        last_char = text.rstrip()[-1:] or last_char

    for i, (f, after, start) in enumerate(zip(frags, following, starts)):
        text = f.text
        if not text.strip():
            if zone is None:
                emit(text, i)
            continue
        same_family = _family(f.font) == family
        last_heading, heading = heading, None
        # A label opens a figure; inside one, only a label in another font
        # than the caption's (a caption may run on with "see figure 4.3").
        label = (
            None
            if is_body(f) or (zone is not None and same_family)
            else _CAPTION_LABEL_RE.match(text)
        )
        if is_body(f):
            zone = None
            keep(i, start)
            body_end = len(out)
        elif label:
            if zone is None:
                # The figure's lettering or the table's cells may come before
                # its label in reading order: take back all since the running text.
                taken = [k for k in origin[body_end:] if k >= 0 and frags[k].text.strip()]
                del out[body_end:], origin[body_end:]
                kept = [k for k in kept if k not in taken]
                lead = [frags[k] for k in taken]
            else:
                lead = zone.split_lettering(family)
            zone = FigureZone(" ".join(label.group().split()), [*lead, f], f"p{page_no}f{len(zones)}", len(lead))
            zones.append(zone)
            emit(f"\n{_FIG_OPEN}{zone.token}{_FIG_CLOSE}\n")
        elif zone is not None:
            zone.frags.append(f)
        elif same_family and f.size < size and on_body_line(prev, f) and (m := _SMALL_MARKER_RE.match(text)):
            # A superscript marker; pypdf runs the rest of the line on in it.
            emit(f"{_MARK_OPEN}{m.group()}{_MARK_CLOSE}{text[m.end():]}", i)
            kept.append(i)
            body_end = len(out)
        elif on_body_line(prev, f) or on_body_line(after, f):
            emit(text, i)
            kept.append(i)
            body_end = len(out)
        elif same_family and f.size < size:
            keep(i, start)
        elif has_body and f.size >= size - 0.5 and "\n" not in text.strip() and len(text.strip()) <= 150:
            if (
                last_heading
                and _family(last_heading[1].font) == _family(f.font)
                and abs(last_heading[1].size - f.size) < 0.1
                and -0.5 < last_heading[1].y - f.y <= 2 * f.size
            ):  # more of the heading: an italic word, its next line
                k = last_heading[0]
                out[k] = f"{out[k].rstrip()} {text.strip()}\n"
            else:
                emit(f"\n{HEADING}{text.strip()}\n", i)
                k = len(out) - 1
            kept.append(i)
            heading = (k, f)
            prev_class, last_char = None, ""
        else:
            emit("\n", i)  # dropped; a figure's label may yet claim it
        prev = f
    text = _NOTE_MARKER_RE.sub(lambda m: f"{_MARK_OPEN}{m.group()}{_MARK_CLOSE}", "".join(out))
    for z in zones:
        z.caption = _caption(z)
    _settle_figures(zones)
    return PageText(text, zones, [frags[k] for k in kept])


def _settle_figures(zones: list[FigureZone]) -> None:
    """Hand each run of lettering or cells to the figure it is drawn in.

    Reading order isn't enough on a page with several figures: a table's
    cells may come before its label, right after the figure above it. Each
    run goes to the nearest caption on its drawing's side — above a
    figure's caption, below a table's — else stays where it was read.
    """
    if len(zones) < 2:
        return
    loose: list[tuple[Fragment, FigureZone]] = []
    for z in zones:
        loose += [(f, z) for k, f in enumerate(z.frags) if not z.label_at <= k < z.caption_end]
        z.frags = z.frags[z.label_at : z.caption_end]
        z.label_at, z.caption_end = 0, len(z.frags)

    def reach(z: FigureZone, f: Fragment) -> float | None:
        gap = (z.caption_bottom - f.y) if z.drawn_below else (f.y - z.caption_top)
        return gap if gap > 0 else None

    for f, home in loose:
        near = [(gap, k) for k, z in enumerate(zones) if (gap := reach(z, f)) is not None]
        (zones[min(near)[1]] if near else home).frags.append(f)


def _caption(zone: FigureZone) -> str:
    """The label with the caption lines printed after it at its left edge;
    records where that caption sits on the page."""
    label = zone.frags[zone.label_at]
    rows: list[list[Fragment]] = [[label]]
    for f in zone.frags[zone.label_at + 1 :]:
        if abs(f.y - rows[-1][0].y) > 0.5 * f.size:  # the next line
            rows.append([])
        rows[-1].append(f)
    taken = rows[:1]
    for row in rows[1:12]:
        # A caption line starts at the label's edge, set solid under the line
        # before; a table's header follows after a wider gap.
        if abs(row[0].x - label.x) > 2 or taken[-1][0].y - row[0].y > 1.75 * label.size:
            break
        taken.append(row)
    lines = ["".join(f.text for f in row) for row in taken]
    zone.caption_top = label.y + 0.85 * label.size
    zone.caption_bottom = taken[-1][0].y - 0.3 * label.size
    zone.caption_end = zone.label_at + sum(len(row) for row in taken)
    text = ""
    for line in _page_lines("\n".join(lines)):
        text = _join(text, line)
    return " ".join(text.split())[:1000]


def _matmul(a, b) -> list[float]:
    return [
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def _page_fragments(page) -> list[Fragment]:
    """A pypdf page → its text fragments, in extraction order; their texts
    concatenate to `page.extract_text()`."""
    out: list[Fragment] = []

    def visit(text, cm, tm, font_dict, font_size):
        if not text:
            return
        m = _matmul(tm, cm)
        name = str((font_dict or {}).get("/BaseFont", "")).lstrip("/")
        name = re.sub(r"^[A-Z]{6}\+", "", name)
        out.append(Fragment(name, abs(font_size) * math.hypot(m[2], m[3]), m[4], m[5], text))

    page.extract_text(visitor_text=visit)
    return out


# ----- PDF access ---------------------------------------------------------------


def _quiet_fonttools_hint(record: logging.LogRecord) -> bool:
    # pypdf logs this per font and page for CFF fonts; the text it extracts
    # without fontTools is complete, so the hint is only noise.
    return not record.getMessage().startswith("fontTools is required")


def _open_reader(pdf_path: Path):
    from pypdf import PdfReader  # lazy: `uv run --with pypdf …`

    cmap_log = logging.getLogger("pypdf._cmap")  # where pypdf 6 logs the hint
    if _quiet_fonttools_hint not in cmap_log.filters:
        cmap_log.addFilter(_quiet_fonttools_hint)
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError(f"{pdf_path} is password-protected")
    return reader


def outline_chapters(reader, n_pages: int) -> list[ChapterSpec]:
    """Top-level bookmarks → chapters; each runs until the next one starts."""
    starts: dict[int, str] = {}
    for item in reader.outline or []:
        if isinstance(item, list):  # the previous entry's children
            continue
        try:
            page = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001 — a broken bookmark is skipped, not fatal
            continue
        if page is not None and 0 <= page < n_pages:
            starts.setdefault(page + 1, (getattr(item, "title", "") or "").strip())
    firsts = sorted(starts)
    return [
        ChapterSpec(first, (firsts[i + 1] - 1) if i + 1 < len(firsts) else n_pages, starts[first])
        for i, first in enumerate(firsts)
    ]


def _save_cover(reader, page_no: int, out: Path) -> bool:
    """Largest raster image on `page_no` → `out` (JPEG, long side capped).

    False when the page holds no real image (a vector cover; masks and
    specks under 200 px don't count). Iterating `page.images` needs Pillow:
    its ImportError propagates so the caller can name the fix.
    """
    images = [im for im in reader.pages[page_no - 1].images if min(im.image.size) >= 200]
    if not images:
        return False
    best = max(images, key=lambda im: im.image.size[0] * im.image.size[1]).image.convert("RGB")
    best.thumbnail((COVER_MAX_PX, COVER_MAX_PX))
    out.parent.mkdir(parents=True, exist_ok=True)
    best.save(out, "JPEG", quality=90)
    return True


def _use_cover_file(src: Path, out: Path) -> None:
    """An image file → `out` as the cover. A JPEG already within the size
    cap is copied byte for byte (no re-encode); anything else is converted
    like a page's image. A no-op when `src` is `out` — re-extracting while
    keeping the folder's current cover."""
    if out.exists() and src.resolve() == out.resolve():
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        if src.suffix.lower() in (".jpg", ".jpeg"):
            shutil.copyfile(src, out)
            return
        raise ValueError(f"converting {src} to the cover needs Pillow: run with `--with \"pypdf[image]\"`")
    with Image.open(src) as im:
        fits = im.format == "JPEG" and max(im.size) <= COVER_MAX_PX
        if not fits:
            img = im.convert("RGB")
            img.thumbnail((COVER_MAX_PX, COVER_MAX_PX))
            img.save(out, "JPEG", quality=90)
    if fits:
        shutil.copyfile(src, out)


def _notes_section(reader, n_pages: int) -> tuple[int, int, str] | None:
    """The endnotes section — a top-level bookmark titled "Notes" or
    "Endnotes" — as (first page, last page, title)."""
    for c in outline_chapters(reader, n_pages):
        if _key(c.title) in ("notes", "endnotes"):
            return c.first_page, c.last_page, c.title
    return None


@dataclass
class _Mark:
    kind: str  # "note" | "figure"
    paragraph: int  # index into the chapter's paragraphs
    offset: int  # where in that paragraph's text
    payload: str  # a note marker's original text / a figure's token


def _split_marks(body: str) -> tuple[list[str], list[_Mark]]:
    """A chapter body with `read_page`'s marks → its clean paragraphs and
    where each mark stood. Marks left alone in a paragraph (a figure after
    the chapter's last line of text) belong at the end of the one before."""
    paragraphs: list[str] = []
    marks: list[_Mark] = []
    for para in body.split("\n\n"):
        clean, pos, here = "", 0, []
        for m in _TOKEN_RE.finditer(para):
            clean += para[pos : m.start()]
            pos = m.end()
            if clean.endswith(" ") and para[pos : pos + 1] == " ":
                pos += 1
            kind = "note" if m.group(1) is not None else "figure"
            here.append((kind, len(clean), m.group(1) if kind == "note" else m.group(2)))
        clean += para[pos:]
        text = clean.strip()
        lead = len(clean) - len(clean.lstrip())
        if text:
            paragraphs.append(text)
            idx = len(paragraphs) - 1
            marks += [_Mark(k, idx, min(max(0, off - lead), len(text)), p) for k, off, p in here]
        else:
            idx = len(paragraphs) - 1
            end = len(paragraphs[idx]) if idx >= 0 else 0
            marks += [_Mark(k, idx, end, p) for k, _off, p in here]
    return paragraphs, marks


# A marker the first pass skips, safe to take once its note's number is known
# and it sits between its neighbours' markers: after any sentence end
# ("the letter A.1 Others", "F#.7 In", "the US. 26 If", "table 3.1. 5 In"),
# or after a closing quote mid-sentence ("a “quiet ending” 9 can be").
_LATE_MARKER_RE = re.compile(
    r"(?<=[.?!\"'’”)])[ \t]?(\d{1,3})(?=[ \t]+[\"“(A-Z]|[ \t]*(?:\n|$))"
    r"|(?<=[\"'’”])[ \t]?(\d{1,3})(?=[ \t]+[a-z])"
)
# Last resort, after a comma or colon ("as studies show,13 memory", "these:14 1.").
_LOOSE_MARKER_RE = re.compile(r"(?<=[,;:])[ \t]?(\d{1,3})(?=[ \t]+[a-z\d]|[ \t]*\n|$)")


def _resolve_markers(body: str, notes: dict | None, used: set[int]) -> tuple[str, int]:
    """Keep the note markers a note claims, in order; put back the digits of
    the others (they were text after all). With no notes (none parsed) every
    marker just goes. Then look for each note still unclaimed between its
    neighbours' markers, in a form the first pass skips. Returns the body
    and how many marker-like numbers went back into the text."""
    restored = 0

    def claim(m: re.Match) -> str:
        nonlocal restored
        if m.group(1) is None:
            return m.group()  # a figure
        if notes is None:
            return ""
        number = int(m.group(1))
        if number in notes and number not in used:
            used.add(number)
            return m.group()
        restored += 1
        return m.group(1)

    body = _TOKEN_RE.sub(claim, body)
    if not notes:
        return body, restored
    for number in sorted(set(notes) - used):
        at = {int(m.group(1)): m for m in _TOKEN_RE.finditer(body) if m.group(1) is not None}
        lo = max((m.end() for n, m in at.items() if n < number), default=0)
        hi = min((m.start() for n, m in at.items() if n > number), default=len(body))
        hit = next(
            (
                m
                for pattern in (_LATE_MARKER_RE, _LOOSE_MARKER_RE)
                for m in pattern.finditer(body, lo, hi)
                if int(next(g for g in m.groups() if g)) == number
            ),
            None,
        )
        if hit is not None:
            body = f"{body[: hit.start()]}{_MARK_OPEN}{hit.group()}{_MARK_CLOSE}{body[hit.end() :]}"
            used.add(number)
    return body, restored


_LABEL_RE = re.compile(r"(figure|fig\.|table|example|plate|map|chart)s?\s+(\d+(?:\.\d+)*[a-z]?)", re.IGNORECASE)
# A mention in the text: "Figure 2.1", "figures 4.1 and 4.2", "Tables 3.2a–3.2b".
_REFERENCE_RE = re.compile(
    r"\b(figures?|figs?\.|tables?|examples?|plates?|maps?|charts?)\s+"
    r"(\d+(?:\.\d+)*[a-z]?(?:\s*(?:,|and|&|or|to|-|–)\s*\d+(?:\.\d+)*[a-z]?)*)",
    re.IGNORECASE,
)


def _figure_kind(word: str) -> str:
    kind = word.lower().rstrip("s.")
    return "figure" if kind == "fig" else kind


def _label_key(label: str) -> tuple[str, str] | None:
    """'Figure 2.1' → ('figure', '2.1')."""
    m = _LABEL_RE.match(label)
    return (_figure_kind(m.group(1)), m.group(2).lower()) if m else None


def _first_mentions(paragraphs: list[str]) -> dict[tuple[str, str], int]:
    """(kind, number) → the paragraph that first mentions it."""
    first: dict[tuple[str, str], int] = {}
    for i, p in enumerate(paragraphs):
        for m in _REFERENCE_RE.finditer(p):
            for number in re.findall(r"\d+(?:\.\d+)*[a-z]?", m.group(2).lower()):
                first.setdefault((_figure_kind(m.group(1)), number), i)
    return first


def figure_band(
    zone: FigureZone,
    kept: list[Fragment],
    page: list[Fragment],
    top: float,
    bottom: float,
    neighbours: list[FigureZone] = (),
) -> tuple[float, float]:
    """The heights a figure is drawn between.

    A figure's caption closes it: a figure's sits below its drawing, so the
    band runs from the nearest text above — kept text, a figure set above
    it, the running head, else the page's top — down to the caption's last
    line. A table's sits above, so the band runs from its label down to the
    nearest text below. Which way round is read off the page (lettering
    above the label, cells below the caption), else taken from the kind.
    The drawing itself has no text: it reaches out to the neighbours.
    """
    mine = {id(f) for f in zone.frags}
    solid = [f for f in page if f.text.strip() and f.size > 1]
    bounds = list(kept) + [f for z in neighbours if z is not zone for f in z.frags]
    if solid:
        # Running head and folio: the page's outermost lines, if not this figure's.
        hi, lo = max(f.y for f in solid), min(f.y for f in solid)
        bounds += [f for f in solid if id(f) not in mine and (abs(f.y - hi) < 1 or abs(f.y - lo) < 1)]
    bounds = [f for f in bounds if id(f) not in mine]
    fig_top = max(f.y + 0.85 * f.size for f in zone.frags)
    fig_bottom = min(f.y - 0.3 * f.size for f in zone.frags)
    lettering_above = fig_top > zone.caption_top + 1
    cells_below = fig_bottom < zone.caption_bottom - 1
    caption_above = cells_below and not lettering_above or (
        cells_below == lettering_above and zone.drawn_below
    )
    if caption_above:
        below = [f.y + 0.85 * f.size for f in bounds if f.y + 0.85 * f.size <= fig_bottom + 0.5]
        return zone.caption_top + 1, (max(below) if below else bottom)
    above = [f.y - 0.3 * f.size for f in bounds if f.y - 0.3 * f.size >= fig_top - 0.5]
    return (min(above) if above else top), zone.caption_bottom - 1


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def extract_pdf(
    pdf_path: Path,
    output_dir: Path,
    *,
    chapters: list[ChapterSpec] | None = None,
    pages: tuple[int, int] | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    language: str = "en",
    cover_page: int | None = 1,
    cover_file: Path | None = None,
    epub_path: Path | None = None,
    font_filter: bool = False,
    notes_pages: tuple[int, int] | None = None,
    notes: bool = True,
    figures: bool = True,
    drop_patterns: Iterable[str] = (),
    rewrites: Iterable[tuple[str, str]] = (),
    progress: Callable[[str], None] = print,
) -> PdfMeta:
    """Chapters (+ cover, + sync EPUB) from a PDF. `title` / `authors`
    override the PDF's metadata; `cover_file` (an image) takes precedence
    over `cover_page`, and both None skip the cover; `epub_path` None skips
    the EPUB. An existing EPUB is only overwritten when an earlier run
    generated it.

    `font_filter` keeps only the text set in the body font (`read_page`) —
    for a born-digital PDF, not a scan — and then also takes out its figures
    and tables (rendered, placed before their first mention; `figures`) and
    its endnotes (from `notes_pages`, else the "Notes" bookmark; rendered and
    tied to their markers; `notes`)."""
    if cover_file is not None and not cover_file.is_file():
        raise ValueError(f"cover image not found: {cover_file}")
    reader = _open_reader(pdf_path)
    n_pages = len(reader.pages)
    info = reader.metadata or {}
    meta = PdfMeta(
        title=(title or getattr(info, "title", None) or "").strip(),
        authors=authors
        or [a.strip() for a in re.split(r"[;,&]", getattr(info, "author", None) or "") if a.strip()],
    )
    if cover_page is not None and not 1 <= cover_page <= n_pages:
        raise ValueError(f"cover page {cover_page}: the PDF has only {n_pages} page(s)")
    if epub_path is not None and epub_path.exists() and not is_generated(epub_path):
        raise ValueError(
            f"{epub_path} exists and wasn't generated by ln-vox — not overwriting it. "
            "Pass another --epub path, or --no-epub."
        )

    if chapters:
        specs = chapters
        source = "--chapter"
    else:
        lo, hi = pages or (1, n_pages)
        specs = [
            ChapterSpec(max(c.first_page, lo), min(c.last_page, hi), c.title)
            for c in outline_chapters(reader, n_pages)
            if c.last_page >= lo and c.first_page <= hi
        ]
        source = "bookmarks"
        if not specs:
            specs = [ChapterSpec(lo, hi, meta.title or pdf_path.stem)]
            source = "whole document (no bookmarks; split with --chapter)"

    # The endnotes section is read for its notes, never narrated.
    section: tuple[int, int, str] | None = None
    if font_filter and notes:
        section = (*notes_pages, "Notes") if notes_pages else _notes_section(reader, n_pages)
        if section:
            specs = [c for c in specs if not (section[0] <= c.first_page and c.last_page <= section[1])]
    for first, last in [(c.first_page, c.last_page) for c in specs] + ([section[:2]] if section else []):
        if last > n_pages:
            raise ValueError(f"pages {first}-{last}: the PDF has only {n_pages} page(s)")
    progress(f"{n_pages} page(s); {len(specs)} chapter(s) from {source}")

    fragments: dict[int, list[Fragment]] = {}
    reads: dict[int, PageText] = {}
    style = None  # the body font, with --font-filter
    if font_filter:
        for c in specs:
            for no in range(c.first_page, c.last_page + 1):
                if no not in fragments:
                    fragments[no] = _page_fragments(reader.pages[no - 1])
        style = body_style(fragments.values())
        if style:
            family, size, share = style
            progress(f"  font filter: body text is {family} {size:g} pt ({share:.0%} of the characters)")
            if share < 0.5:
                progress("  font filter: no dominant body font (a scan's OCR layer?) — text may be lost")

    texts: dict[int, str] = {}

    def page_text(no: int) -> str:
        if no not in texts:
            if style:
                raw = "".join(f.text for f in fragments[no])
                reads[no] = read_page(fragments[no], style[0], style[1], no)
                texts[no] = reads[no].text
            else:
                raw = texts[no] = reader.pages[no - 1].extract_text() or ""
            if not raw.strip():
                meta.pages_without_text.append(no)
        return texts[no]

    bodies = []
    for i, c in enumerate(specs, start=1):
        ch_title = c.title or f"Chapter {i}"
        body = clean_chapter([page_text(p) for p in range(c.first_page, c.last_page + 1)], c.title)
        bodies.append((c, ch_title, body))
    if not any(_TOKEN_RE.sub("", body).strip() for _, _, body in bodies):
        raise ValueError(
            f"no text in {pdf_path}: it has no text layer (a scan without OCR?). "
            "Run it through OCR first, e.g. `ocrmypdf in.pdf out.pdf`."
        )

    # Endnotes, grouped by the chapter they number ("Chapter 2" → "#2").
    note_groups: dict[str, dict] = {}
    if section and style:
        from lnvox.ingest.pdf_notes import group_key, parse_notes

        parsed = parse_notes(
            [(no, _page_fragments(reader.pages[no - 1])) for no in range(section[0], section[1] + 1)],
            title=section[2],
        )
        for n in parsed:
            note_groups.setdefault(group_key(n.group), {}).setdefault(n.number, n)
        progress(
            f"  notes: {len(parsed)} in {len(note_groups)} group(s), pages {section[0]}-{section[1]}"
            if parsed
            else f"  notes: none found on pages {section[0]}-{section[1]}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    removed = _remove_previous_chapter_files(output_dir, SIDECAR)
    if removed:
        progress(f"  removed {removed} chapter file(s) from the previous extraction")
    _remove_previous_renders(output_dir)

    renderer = None
    if style and (figures or note_groups):
        try:
            from lnvox.ingest.pdf_render import PageRenderer

            renderer = PageRenderer(pdf_path)
        except ImportError:
            progress('  figures and notes not rendered: needs pypdfium2 — run with `--with pypdfium2`')

    cover: Path | None = None
    if cover_file is not None:
        cover = output_dir / "images" / "cover.jpg"
        _use_cover_file(cover_file, cover)
        meta.cover_image = cover.relative_to(output_dir).as_posix()
        progress(f"  cover: {cover_file.as_posix()} → {meta.cover_image}")
    elif cover_page is not None:
        cover = output_dir / "images" / "cover.jpg"
        found = _make_cover(pdf_path, reader, cover_page, cover, progress)
        if found:
            meta.cover_image = cover.relative_to(output_dir).as_posix()
        else:
            cover = None

    written = [(c, t, b) for c, t, b in bodies if _TOKEN_RE.sub("", b).strip()]
    id_width = max(2, len(str(len(written))))
    for c, ch_title, body in bodies:
        if not _TOKEN_RE.sub("", body).strip():
            progress(f"  skipped '{ch_title}' (pages {c.first_page}-{c.last_page}): no text")

    from lnvox.ingest.pdf_notes import chapter_key

    zones = {z.token: (no, z) for no, r in reads.items() for z in r.figures}
    images_dir = output_dir / "images"
    epub_chapters: list[EpubChapter] = []
    figure_images: list[str] = []  # in reading order — the m4b embeds them after the cover
    note_images: list[Path] = []
    visual_elements: list[dict] = []
    used_by_group: dict[str, set[int]] = {}
    n_figures = n_mentioned = n_notes = n_restored = 0
    for i, (c, ch_title, body) in enumerate(written, start=1):
        chapter_id = f"{i:0{id_width}d}"
        stem = f"chapter{chapter_id}"  # the EPUB spine item Stage 6 aligns it onto
        key = chapter_key(ch_title)
        if not note_groups:
            chapter_notes = None
        elif key in note_groups or set(note_groups) != {""}:
            chapter_notes = note_groups.get(key, {})
        else:  # one series of notes for the whole book
            key, chapter_notes = "", note_groups[""]
        used = used_by_group.setdefault(key, set())
        body, restored = _resolve_markers(body, chapter_notes, used)
        n_restored += restored
        # After the markers: one the notes don't claim goes back in as text,
        # and the book's own patterns must see it there.
        body = "\n\n".join(_edit_paragraphs(body.split("\n\n"), drop_patterns, rewrites))
        paragraphs, marks = _split_marks(body)

        filename = f"{chapter_id}-{_slugify(ch_title)}.txt"
        text = "\n\n".join(paragraphs)
        (output_dir / filename).write_text(f"{ch_title}\n\n{text}\n", encoding="utf-8")

        epub_notes: list[EpubNote] = []
        for mark in (m for m in marks if m.kind == "note"):
            number = int(mark.payload)
            note = chapter_notes[number]
            src = ""
            if renderer is not None:
                crops = [renderer.band(page, top, bottom) for page, top, bottom in note.segments]
                crops = [im for im in crops if im is not None]
                if crops:
                    path = images_dir / f"note-{chapter_id}-{number}.png"
                    _save_png(_vstack(crops), path)
                    note_images.append(path)
                    src = path.relative_to(output_dir).as_posix()
            epub_notes.append(EpubNote(mark.paragraph, mark.offset, str(number), note.paragraphs))
            para = paragraphs[mark.paragraph] if mark.paragraph >= 0 else ""
            visual_elements.append({
                "kind": "footnote",
                "src": src,
                "html": '<aside epub:type="footnote">'
                + "".join(
                    f"<p>{f'<sup>{number}</sup> ' if j == 0 else ''}{_html(p)}</p>"
                    for j, p in enumerate(note.paragraphs or [""])
                )
                + "</aside>",
                "text": note.text,
                "label": str(number),
                "language": "",
                "chapter_id": chapter_id,
                "after_paragraph": mark.paragraph,
                "anchor": {"paragraph": mark.paragraph, "text": para[max(0, mark.offset - 60) : mark.offset]},
                "spine_page": stem,
            })
            n_notes += 1

        # Figures: a table continued over a page break is one figure, shown
        # before the paragraph that first mentions it (else after the one it
        # interrupted).
        by_label: dict[str, tuple[_Mark, list]] = {}
        for mark in (m for m in marks if m.kind == "figure" and m.payload in zones):
            no, zone = zones[mark.payload]
            by_label.setdefault(zone.label, (mark, []))[1].append((no, zone))
        mentions = _first_mentions(paragraphs)
        epub_figures: list[EpubFigure] = []
        if figures and renderer is not None:
            for label, (mark, pieces) in by_label.items():
                crops = []
                for no, zone in pieces:
                    box = reader.pages[no - 1].cropbox
                    band = figure_band(
                        zone, reads[no].kept, fragments[no], float(box.top), float(box.bottom), reads[no].figures
                    )
                    crops.append(renderer.band(no, *band))
                crops = [im for im in crops if im is not None]
                if not crops:
                    progress(f"  [skip] {label} (page {pieces[0][0]}): nothing drawn to render")
                    continue
                path = images_dir / f"{_slug(label)}.png"
                k = 2
                while path.relative_to(output_dir).as_posix() in figure_images:
                    path, k = images_dir / f"{_slug(label)}-{k}.png", k + 1
                _save_png(_vstack(crops), path)
                ref = mentions.get(_label_key(label) or ("", ""))
                n_mentioned += ref is not None
                epub_figures.append(
                    EpubFigure(path, ref if ref is not None else mark.paragraph + 1, pieces[0][1].caption or label)
                )
        epub_figures.sort(key=lambda f: f.before)
        figure_images += [f.image.relative_to(output_dir).as_posix() for f in epub_figures]
        n_figures += len(epub_figures)

        epub_chapters.append(EpubChapter(stem, ch_title, paragraphs, epub_figures, epub_notes))
        meta.chapters.append({
            "file": filename,
            "title": ch_title,
            "source_parts": [stem],
            "pages": [c.first_page, c.last_page],
        })
        extras = ", ".join(
            s for s in (
                f"{len(epub_figures)} figure(s)" if epub_figures else "",
                f"{len(epub_notes)} note(s)" if epub_notes else "",
            ) if s
        )
        progress(
            f"  {filename}  ({len(text):,} chars from pages {c.first_page}-{c.last_page}"
            f"{', ' + extras if extras else ''})"
        )
    if renderer is not None:
        renderer.close()

    if style and figures and renderer is not None:
        progress(
            f"  figures: {n_figures} rendered — {n_mentioned} placed before their first mention, "
            f"{n_figures - n_mentioned} where printed"
        )
    if note_groups:
        total = sum(len(g) for g in note_groups.values())
        progress(
            f"  notes: {n_notes} of {total} tied to their markers"
            + (f"; {n_restored} marker-like number(s) left in the text" if n_restored else "")
        )
    meta.figures, meta.notes = n_figures, n_notes

    if meta.pages_without_text:
        progress(
            f"  {len(meta.pages_without_text)} page(s) had no text layer "
            f"(blank, or images only): {sorted(meta.pages_without_text)}"
        )
    book_title = meta.title or pdf_path.stem
    if epub_path is not None:
        write_epub(
            epub_path,
            title=book_title,
            authors=meta.authors,
            chapters=epub_chapters,
            cover=cover,
            language=language,
            extra_images=note_images,
        )
        meta.epub = epub_path
        progress(f"  EPUB (sync target) → {epub_path}")

    # The same keys `extract_epub` writes, so `lnvox ingest` (cover → Stage 5)
    # and Stage 6 (chapter → XHTML, notes) treat this folder like an EPUB
    # import. `images` is what Stage 5 embeds in the m4b: the cover, then the
    # figures, which Stage 6 finds in the EPUB and times.
    (output_dir / SIDECAR).write_text(
        json.dumps(
            {
                "title": book_title,
                "authors": meta.authors,
                "publisher": None,
                "language": language,
                "cover_image": meta.cover_image,
                "images": ([meta.cover_image] if meta.cover_image else []) + figure_images,
                "chapters": meta.chapters,
                "visual_elements": visual_elements,
                "source_pdf": str(pdf_path),
                "epub": epub_path.as_posix() if epub_path else None,
                # Removed by the next extraction into this folder.
                "rendered": figure_images + [p.relative_to(output_dir).as_posix() for p in note_images],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return meta


def _html(text: str) -> str:
    from xml.sax.saxutils import escape

    return escape(text)


def _save_png(img, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)


def _vstack(images: list):
    from lnvox.ingest.pdf_render import vstack

    return vstack(images)


def _remove_previous_renders(output_dir: Path) -> None:
    """Delete the figure and note images the previous extraction listed."""
    try:
        previous = json.loads((output_dir / SIDECAR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for rel in previous.get("rendered") or []:
        path = output_dir / rel
        if isinstance(rel, str) and rel.startswith("images/") and path.is_file():
            path.unlink()


def _make_cover(pdf_path: Path, reader, page_no: int, out: Path, progress) -> bool:
    """The cover page → `out`: rendered whole (a cover is often layered
    images, vector shapes and text), else its largest image."""
    try:
        from lnvox.ingest.pdf_render import render_page

        render_page(pdf_path, page_no, out, COVER_MAX_PX)
        progress(f"  cover: page {page_no} rendered → {out.parent.name}/{out.name}")
        return True
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 — fall back to the page's picture
        progress(f"  cover: page {page_no} couldn't be rendered ({e}); using its largest image")
    try:
        found = _save_cover(reader, page_no, out)
    except ImportError:
        progress('  cover skipped: needs Pillow — run with `uv run --with "pypdf[image]" …`')
        return False
    except Exception as e:  # noqa: BLE001 — an undecodable image costs the cover, not the book
        progress(f"  cover skipped: page {page_no}'s image couldn't be decoded ({e})")
        return False
    progress(
        f"  cover: page {page_no}'s largest image → {out.parent.name}/{out.name} "
        "(add `--with pypdfium2` to render the whole page)"
        if found
        else f"  cover skipped: page {page_no} has no raster image (add `--with pypdfium2` to render it)"
    )
    return found

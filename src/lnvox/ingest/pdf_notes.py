"""Endnotes of a born-digital PDF (`lnvox ingest-pdf --font-filter`).

A book's notes section is set as numbered notes under group headers, one
group per chapter:

    Notes                          ← the section's title (skipped)
    Chapter 1                      ← a group header, in another font
    1. I believe that Leonard …    ← a note: "N." at the left margin
       Experimental investigations …   ← an indented paragraph of the same note
    2. The pertinent research …
    382 Notes                      ← a running head (skipped)

`parse_notes` reads that into `Note`s, with the page band each note covers
so it can be rendered; `group_key` / `chapter_key` pair a group with the
chapter whose markers it numbers ("Chapter 1" ↔ "1 Introduction", "Preface"
↔ "Preface"). A book without group headers numbers its notes in one series:
every note then lands in group "".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from lnvox.ingest.pdf import Fragment, _family, _join, _key, _page_lines, body_style

_NOTE_START_RE = re.compile(r"\s*(\d{1,3})\.\s+")
_CHAPTER_NUMBER_RE = re.compile(r"\s*(?:chapter\s+)?(\d{1,3})\b", re.IGNORECASE)
BULLET = chr(0x2022)


@dataclass
class Note:
    group: str  # the header it sits under ("Chapter 2", "Preface"); "" without headers
    number: int
    paragraphs: list[str] = field(default_factory=list)
    # (page, top, bottom) in PDF user space: the band to render, per page
    segments: list[tuple[int, float, float]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def group_key(text: str) -> str:
    """A notes group header or a chapter title → the key that pairs them:
    '#N' for a numbered chapter, else its letters."""
    m = _CHAPTER_NUMBER_RE.match(text)
    return f"#{int(m.group(1))}" if m else _key(text)


chapter_key = group_key  # "1 Introduction" and "Chapter 1" both → "#1"


@dataclass
class _Line:
    page: int
    x: float
    y: float
    text: str
    families: set[str]


def _lines(page: int, frags: list[Fragment]) -> list[_Line]:
    """Fragments → text lines: pypdf ends a line's last run with a newline."""
    lines: list[_Line] = []
    cur: list[Fragment] = []
    for f in [*frags, None]:
        if f is not None:
            cur.append(f)
        if f is None or f.text.endswith("\n"):
            solid = [g for g in cur if g.text.strip()]
            if solid:
                lines.append(
                    _Line(
                        page,
                        solid[0].x,
                        solid[0].y,
                        "".join(g.text for g in cur).replace(chr(0x7F), BULLET),
                        {_family(g.font) for g in solid},
                    )
                )
            cur = []
    return lines


def parse_notes(pages: list[tuple[int, list[Fragment]]], title: str = "Notes") -> list[Note]:
    """The notes on `pages` — (page number, its fragments) — in order."""
    style = body_style(frags for _, frags in pages)
    if style is None:
        return []
    family, size, _share = style
    lines = [ln for page, frags in pages for ln in _lines(page, frags)]
    starts = [ln.x for ln in lines if family in ln.families]
    if not starts:
        return []
    margin = Counter(round(x) for x in starts).most_common(1)[0][0]
    line_gap = _line_gap(lines, family)

    notes: list[Note] = []
    group = ""
    header: str | None = None  # a heading not yet known to open a group
    note: Note | None = None
    above: _Line | None = None  # the previous line of note text
    for ln in lines:
        if family not in ln.families:
            # The section's title, a running head ("382 Notes") or the header
            # of the next chapter's notes — which only a note "1." confirms.
            if _key(ln.text) != _key(title):
                header = " ".join(ln.text.split())
            continue
        m = _NOTE_START_RE.match(ln.text)
        number = int(m.group(1)) if m else 0
        gap = above.y - ln.y if above is not None and above.page == ln.page else 0.0
        above = ln
        # A note starts at the margin, numbered on from the last one (or 1 in
        # a new group) or set off by the wider space between notes — a note
        # misnumbered in print is still its own; anything else is a line of
        # the current note.
        if m and abs(ln.x - margin) < 1.5 and (
            note is None or number in (note.number + 1, 1) or gap > 1.3 * line_gap
        ):
            if header is not None and number == 1:
                group = header
            header = None
            note = Note(group, number, [""])
            notes.append(note)
            text = ln.text[m.end() :]
        elif note is None:
            continue
        else:
            text = ln.text
            if ln.x - margin >= 0.4 * size:  # an indented line opens a paragraph
                note.paragraphs.append("")
        for part in _page_lines(text):
            note.paragraphs[-1] = _join(note.paragraphs[-1], part)
        _extend(note, ln, size, line_gap)
    for n in notes:
        n.paragraphs = [" ".join(p.split()) for p in n.paragraphs if p.strip()]
    return notes


def _line_gap(lines: list[_Line], family: str) -> float:
    """The distance between two lines of one note: the lower quartile of
    all gaps, which a section of many one-line notes can't push up to the
    wider space between notes."""
    gaps = sorted(
        a.y - b.y
        for a, b in zip(lines, lines[1:])
        if a.page == b.page and family in a.families and 0 < a.y - b.y < 40
    )
    return gaps[len(gaps) // 4] if gaps else 12.0


def _extend(note: Note, ln: _Line, size: float, line_gap: float) -> None:
    """Grow the note's band on `ln`'s page to take in the line."""
    top, bottom = ln.y + 0.85 * size, ln.y - min(0.35 * size, 0.45 * line_gap)
    if note.segments and note.segments[-1][0] == ln.page:
        page, t, b = note.segments[-1]
        note.segments[-1] = (page, max(t, top), min(b, bottom))
    else:
        note.segments.append((ln.page, top, bottom))

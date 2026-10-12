"""PDF → ln-vox novels-folder converter (`lnvox ingest-pdf`).

Writes the layout `lnvox ingest` reads — the same contract as `ingest-epub`:

    <output_dir>/
        01-atomic-explosives.txt     (first line = chapter title)
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

The cover is the largest image on the cover page — a scan's page image, or a
publisher PDF's cover art; a purely vector cover would need a rasterizer and
is skipped. pypdf (and Pillow, for the cover) are imported lazily and aren't
locked dependencies: run this as `uv run --with "pypdf[image]" lnvox
ingest-pdf …`. A re-extraction first removes the chapter files the previous
run listed in `.epub_meta.json`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from lnvox.ingest.epub import _remove_previous_chapter_files, _slugify
from lnvox.ingest.epub_writer import EpubChapter, is_generated, write_epub

SIDECAR = ".epub_meta.json"
COVER_MAX_PX = 1600  # long side of the embedded cover — plenty for a player or reader

_SOFT_HYPHEN = "\u00ad"
_LIGATURES = str.maketrans(
    {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}
)
_PAGE_NUMBER_RE = re.compile(r"\d{1,4}")
# Strict numeral grammar, so edge words like "did" or "mild" don't qualify.
_ROMAN_RE = re.compile(r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", re.IGNORECASE)
# Rule-line debris glued to the start of a line ("------ - -----there are").
_RULE_PREFIX_RE = re.compile(r"^[-_=~][-_=~\s]{3,}")
_SENTENCE_END = (".", "?", "!", ":", '"', "'", "\u201d", "\u2019")
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
    return bool(
        _PAGE_NUMBER_RE.fullmatch(s)
        or (s and _ROMAN_RE.fullmatch(s))
        or not re.search(r"[A-Za-z]{2}", s)
    )


def _page_lines(text: str) -> list[str]:
    text = text.translate(_LIGATURES)
    return [_RULE_PREFIX_RE.sub("", ln.strip()) for ln in text.splitlines() if ln.strip()]


def _running_keys(pages: list[list[str]]) -> set[str]:
    """Keys of lines that recur at the page edges: running heads and feet.

    Counted per chapter, since a book's recto head is usually the chapter
    title. Digits are ignored by the key, so "12 THE OPEN MIND" and
    "THE OPEN MIND 13" count as one head.
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
# "I. Atomic Explosives (May 1946)" over a page headed "Atomic Explosives".
_TITLE_NUMBERING_RE = re.compile(r"^\s*(?:chapter\s+)?(?:[ivxlcdm]+|\d+)[.:)\-]?\s+", re.IGNORECASE)
_TITLE_TRAILER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _drop_title_lines(lines: list[str], title: str) -> list[str]:
    """Drop the chapter title printed at the top of its first page.

    Fuzzy, so an OCR'd "Atomie Explosives" still matches the title, and the
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
            return lines[i + 1 :]
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


def clean_chapter(pages: list[str], title: str = "") -> str:
    """One chapter's raw page texts → narration-ready paragraphs."""
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
    prev_no: int | None = None
    for lines, page_no in zip(trimmed, numbers):
        # A jump in printed page numbers = missing pages: don't glue the
        # sentence fragments on either side into one paragraph.
        if buf and prev_no is not None and page_no is not None and page_no != prev_no + 1:
            paragraphs.append(buf)
            buf = ""
        if page_no is not None:
            prev_no = page_no
        for line in lines:
            buf = _join(buf, line)
            if line.endswith(_SENTENCE_END) and len(line) < 0.85 * width:
                paragraphs.append(buf)
                buf = ""
    if buf:
        paragraphs.append(buf)
    cleaned = (re.sub(r"\s+", " ", p.replace(_SOFT_HYPHEN, "")).strip() for p in paragraphs)
    return "\n\n".join(p for p in cleaned if p)


# ----- PDF access ---------------------------------------------------------------


def _open_reader(pdf_path: Path):
    from pypdf import PdfReader  # lazy: `uv run --with pypdf …`

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
    progress: Callable[[str], None] = print,
) -> PdfMeta:
    """Chapters (+ cover, + sync EPUB) from a PDF. `title` / `authors`
    override the PDF's metadata; `cover_file` (an image) takes precedence
    over `cover_page`, and both None skip the cover; `epub_path` None skips
    the EPUB. An existing EPUB is only overwritten when an earlier run
    generated it."""
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
    for c in specs:
        if c.last_page > n_pages:
            raise ValueError(f"pages {c.first_page}-{c.last_page}: the PDF has only {n_pages} page(s)")
    progress(f"{n_pages} page(s); {len(specs)} chapter(s) from {source}")

    texts: dict[int, str] = {}

    def page_text(no: int) -> str:
        if no not in texts:
            texts[no] = reader.pages[no - 1].extract_text() or ""
            if not texts[no].strip():
                meta.pages_without_text.append(no)
        return texts[no]

    bodies = []
    for i, c in enumerate(specs, start=1):
        ch_title = c.title or f"Chapter {i}"
        body = clean_chapter([page_text(p) for p in range(c.first_page, c.last_page + 1)], c.title)
        bodies.append((c, ch_title, body))
    if not any(body for _, _, body in bodies):
        raise ValueError(
            f"no text in {pdf_path}: it has no text layer (a scan without OCR?). "
            "Run it through OCR first, e.g. `ocrmypdf in.pdf out.pdf`."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    removed = _remove_previous_chapter_files(output_dir, SIDECAR)
    if removed:
        progress(f"  removed {removed} chapter file(s) from the previous extraction")

    cover: Path | None = None
    if cover_file is not None:
        cover = output_dir / "images" / "cover.jpg"
        _use_cover_file(cover_file, cover)
        meta.cover_image = cover.relative_to(output_dir).as_posix()
        progress(f"  cover: {cover_file.as_posix()} → {meta.cover_image}")
    elif cover_page is not None:
        cover = output_dir / "images" / "cover.jpg"
        try:
            found = _save_cover(reader, cover_page, cover)
        except ImportError:
            found = False
            progress('  cover skipped: needs Pillow — run with `uv run --with "pypdf[image]" …`')
        except Exception as e:  # noqa: BLE001 — an undecodable image costs the cover, not the book
            found = False
            progress(f"  cover skipped: page {cover_page}'s image couldn't be decoded ({e})")
        else:
            progress(
                f"  cover: page {cover_page} → {cover.relative_to(output_dir).as_posix()}"
                if found
                else f"  cover skipped: page {cover_page} has no raster image"
            )
        if found:
            meta.cover_image = cover.relative_to(output_dir).as_posix()
        else:
            cover = None

    written = [(c, t, b) for c, t, b in bodies if b]
    id_width = max(2, len(str(len(written))))
    for c, ch_title, body in bodies:
        if not body:
            progress(f"  skipped '{ch_title}' (pages {c.first_page}-{c.last_page}): no text")
    epub_chapters = []
    for i, (c, ch_title, body) in enumerate(written, start=1):
        chapter_id = f"{i:0{id_width}d}"
        filename = f"{chapter_id}-{_slugify(ch_title)}.txt"
        (output_dir / filename).write_text(f"{ch_title}\n\n{body}\n", encoding="utf-8")
        stem = f"chapter{chapter_id}"  # the EPUB spine item Stage 6 aligns it onto
        epub_chapters.append(EpubChapter(stem, ch_title, body.split("\n\n")))
        meta.chapters.append({
            "file": filename,
            "title": ch_title,
            "source_parts": [stem],
            "pages": [c.first_page, c.last_page],
        })
        progress(f"  {filename}  ({len(body):,} chars from pages {c.first_page}-{c.last_page})")

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
        )
        meta.epub = epub_path
        progress(f"  EPUB (sync target) → {epub_path}")

    # The same keys `extract_epub` writes, so `lnvox ingest` (cover → Stage 5)
    # and Stage 6 (chapter → XHTML) treat this folder like an EPUB import.
    (output_dir / SIDECAR).write_text(
        json.dumps(
            {
                "title": book_title,
                "authors": meta.authors,
                "publisher": None,
                "language": language,
                "cover_image": meta.cover_image,
                "images": [meta.cover_image] if meta.cover_image else [],
                "chapters": meta.chapters,
                "visual_elements": [],
                "source_pdf": str(pdf_path),
                "epub": epub_path.as_posix() if epub_path else None,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return meta

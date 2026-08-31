"""Spine-page chapter splitting (DESIGN.md §18).

All book content below is invented — no real book text.
"""

import zipfile
from pathlib import Path

from lnvox.ingest.epub import _split_page_sections, extract_epub


def _page(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"{body}</body></html>"
    ).encode()


def test_page_without_markers_is_one_section():
    page = _page("<p>A lantern swung in the wind.</p><p>Nobody answered.</p>")
    sections = _split_page_sections(page, "chapter1")
    assert len(sections) == 1
    assert sections[0][0] == "chapter1"


def test_single_heading_does_not_split():
    """One marker is the ordinary one-page-one-chapter case."""
    page = _page("<h2>The Gate</h2><p>A lantern swung in the wind.</p>")
    sections = _split_page_sections(page, "chapter1")
    assert len(sections) == 1
    assert sections[0][1] == "The Gate"


def test_splits_at_headings():
    page = _page(
        "<h2>The Gate</h2><p>A lantern swung.</p>"
        "<h2>The Road</h2><p>Dust rose behind them.</p><p>Then rain.</p>"
    )
    sections = _split_page_sections(page, "book-h-0")
    assert [s[1] for s in sections] == ["The Gate", "The Road"]
    assert sections[0][2] == "A lantern swung."
    assert sections[1][2] == "Dust rose behind them.\n\nThen rain."


def test_splits_at_gutenberg_chapter_anchors_titled_by_image_alt():
    """Illustrated editions render chapter titles as images, not headings."""
    page = _page(
        '<div><a id="Chapter_I"/><img alt="Chapter I" src="i1.jpg"/></div>'
        "<p>A lantern swung.</p>"
        '<div><a id="Chapter_II"/><img alt="Chapter II" src="i2.jpg"/></div>'
        "<p>Dust rose behind them.</p>"
    )
    sections = _split_page_sections(page, "book-h-2")
    assert [s[1] for s in sections] == ["Chapter I", "Chapter II"]
    assert [s[0] for s in sections] == ["book-h-2#Chapter_I", "book-h-2#Chapter_II"]
    # The <img> must not leak into the narrated text.
    assert "Chapter I" not in sections[0][2]


def test_page_number_anchors_are_not_chapter_markers():
    page = _page(
        '<h2>The Gate</h2><p><a id="Page_117"/>A lantern swung.</p>'
        '<p><a id="pgepubid00103"/>Nobody answered.</p>'
    )
    sections = _split_page_sections(page, "chapter1")
    assert len(sections) == 1


def test_text_before_first_marker_opens_the_first_chapter():
    page = _page(
        "<p>An epigraph.</p>"
        "<h2>The Gate</h2><p>A lantern swung.</p>"
        "<h2>The Road</h2><p>Dust rose.</p>"
    )
    sections = _split_page_sections(page, "book-h-0")
    assert sections[0][2] == "An epigraph.\n\nA lantern swung."


def _build_epub(tmp_path: Path, page_body: str) -> Path:
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Invented Book</dc:title></metadata><manifest>"
            '<item id="p1" href="p1.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine><itemref idref=\"p1\"/></spine></package>",
        )
        zf.writestr("p1.xhtml", _page(page_body).decode())
    return epub


_TWO_CHAPTER_PAGE = (
    "<h2>The Gate</h2><p>A lantern swung.</p>"
    "<h2>The Road</h2><p>Dust rose behind them.</p>"
)


def test_extract_epub_split_is_opt_in(tmp_path):
    epub = _build_epub(tmp_path, _TWO_CHAPTER_PAGE)

    off = extract_epub(epub, tmp_path / "off", progress=lambda _m: None)
    assert len(off.chapters) == 1

    on = extract_epub(
        epub, tmp_path / "on", split_chapters=True, progress=lambda _m: None
    )
    assert len(on.chapters) == 2
    assert [c["title"] for c in on.chapters] == ["The Gate", "The Road"]
    # §18.5: source_parts keeps the anchor so Stage 6 can re-align.
    assert on.chapters[0]["source_parts"] == ["p1#sec000"]


def test_split_conserves_every_paragraph(tmp_path):
    epub = _build_epub(tmp_path, _TWO_CHAPTER_PAGE)

    def bodies(meta, root: Path) -> list[str]:
        out: list[str] = []
        for ch in meta.chapters:
            text = (root / ch["file"]).read_text(encoding="utf-8")
            out += [
                p.strip()
                for p in text.split("\n\n", 1)[1].split("\n\n")
                if p.strip()
            ]
        return out

    off_root, on_root = tmp_path / "off", tmp_path / "on"
    off = extract_epub(epub, off_root, progress=lambda _m: None)
    on = extract_epub(
        epub, on_root, split_chapters=True, progress=lambda _m: None
    )
    assert bodies(on, on_root) == bodies(off, off_root)

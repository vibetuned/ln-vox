"""Spine-page chapter splitting (DESIGN.md §18).

All book content below is invented — no real book text.
"""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.ingest.epub import _resolve_href, _split_page_sections, extract_epub
from lnvox.ingest.text import ingest_folder


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


# ---- nested chapter markers ----------------------------------------------------


def test_anchor_inside_heading_is_one_marker():
    """Gutenberg: `<h2><a id="chap01"/>CHAPTER I.</h2>` is ONE chapter start.

    Counted twice, the heading's section came out empty and the text landed
    in a chapter titled "chap01" (or a later picture's alt text).
    """
    page = _page(
        '<h2><a id="chap01"></a>Chapter I. The Gate</h2><p>A lantern swung.</p>'
        '<img alt="A lantern" src="l.jpg"/><p>Nobody answered.</p>'
        '<h2><a id="chap02"></a>Chapter II. The Road</h2><p>Dust rose.</p>'
    )
    sections = _split_page_sections(page, "book-h-1")
    assert [s[1] for s in sections] == ["Chapter I. The Gate", "Chapter II. The Road"]
    assert sections[0][2] == "A lantern swung.\n\nNobody answered."


def test_chapter_section_wrapping_heading_is_one_marker(tmp_path):
    """`<section id="chapter12"><h1>…</h1>` (id matches the anchor rule) is one
    chapter, so a heading-less continuation page still merges into it."""
    body1 = '<section id="chapter01"><h1>The Gate</h1><p>A lantern swung.</p></section>'
    body2 = "<section><p>Nobody answered.</p></section>"
    epub = _build_multi_epub(tmp_path, [("chapter01", body1), ("chapter01_1", body2)])
    meta = extract_epub(epub, tmp_path / "out", split_chapters=True, progress=lambda _m: None)
    assert [c["title"] for c in meta.chapters] == ["The Gate"]
    assert meta.chapters[0]["source_parts"] == ["chapter01", "chapter01_1"]


def test_anchor_wrapping_heading_takes_the_heading_title():
    page = _page(
        '<a id="Chapter_I"><h2>The Gate</h2></a><p>A lantern swung.</p>'
        '<a id="Chapter_II"><h2>The Road</h2></a><p>Dust rose.</p>'
    )
    sections = _split_page_sections(page, "book-h-2")
    assert [s[1] for s in sections] == ["The Gate", "The Road"]


# ---- multi-page fixtures --------------------------------------------------------


def _build_multi_epub(
    tmp_path: Path, pages: list[tuple[str, str]], *, images: dict[str, bytes] = None,
    text_dir: str = "",
) -> Path:
    """EPUB with one spine page per `(stem, body)`; pages live under `text_dir`."""
    images = images or {}
    epub = tmp_path / "multi.epub"
    prefix = f"{text_dir}/" if text_dir else ""
    manifest = "".join(
        f'<item id="p{i}" href="{prefix}{stem}.xhtml" media-type="application/xhtml+xml"/>'
        for i, (stem, _b) in enumerate(pages)
    ) + "".join(
        f'<item id="i{i}" href="{href}" media-type="image/jpeg"/>'
        for i, href in enumerate(images)
    )
    spine = "".join(f'<itemref idref="p{i}"/>' for i in range(len(pages)))
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>Invented Book</dc:title></metadata><manifest>{manifest}"
            f"</manifest><spine>{spine}</spine></package>",
        )
        for stem, body in pages:
            zf.writestr(f"OEBPS/{prefix}{stem}.xhtml", _page(body).decode())
        for href, data in images.items():
            zf.writestr(f"OEBPS/{href}", data)
    return epub


# ---- 100+ chapters ---------------------------------------------------------------


def test_chapter_files_pad_to_the_count_and_sort_numerically(tmp_path):
    pages = [(f"c{i}", f"<h2>Part {i}</h2><p>Body {i}.</p>") for i in range(1, 102)]
    epub = _build_multi_epub(tmp_path, pages)
    out = tmp_path / "big"
    meta = extract_epub(epub, out, progress=lambda _m: None)
    files = [c["file"] for c in meta.chapters]
    assert files[0].startswith("001-") and files[-1].startswith("101-")
    # Every downstream stage sorts ids as strings: equal width keeps order.
    chapters = ingest_folder(out)
    assert [c.chapter_id for c in chapters] == [f"{i:03d}" for i in range(1, 102)]
    assert [c.title for c in chapters][:3] == ["Part 1", "Part 2", "Part 3"]


def test_small_books_keep_two_digit_ids(tmp_path):
    epub = _build_epub(tmp_path, _TWO_CHAPTER_PAGE)
    meta = extract_epub(epub, tmp_path / "out", split_chapters=True, progress=lambda _m: None)
    assert [c["file"][:3] for c in meta.chapters] == ["01-", "02-"]


def test_ingest_folder_sorts_by_numeric_prefix(tmp_path):
    for name in ("100-d.txt", "11-e.txt", "2-b.txt", "10-c.txt", "extra.txt"):
        (tmp_path / name).write_text(f"{name}\n\nbody", encoding="utf-8")
    ids = [c.chapter_id for c in ingest_folder(tmp_path)]
    assert ids == ["2", "10", "11", "100", "extra"]


# ---- re-extraction ---------------------------------------------------------------


def test_reextract_removes_only_the_previous_runs_chapter_files(tmp_path):
    epub = _build_epub(tmp_path, _TWO_CHAPTER_PAGE)
    out = tmp_path / "out"
    first = extract_epub(epub, out, split_chapters=True, progress=lambda _m: None)
    assert len(first.chapters) == 2
    keep = out / "99-hand-written.txt"   # not written by ingest-epub
    keep.write_text("Mine\n\nkeep me", encoding="utf-8")

    second = extract_epub(epub, out, progress=lambda _m: None)
    written = {c["file"] for c in second.chapters}
    on_disk = {p.name for p in out.glob("*.txt")}
    assert on_disk == written | {keep.name}, on_disk
    assert keep.read_text(encoding="utf-8") == "Mine\n\nkeep me"


def test_reextract_ignores_sidecar_paths_outside_the_folder(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    (out / ".epub_meta.json").write_text(
        json.dumps({"chapters": [{"file": "../victim.txt"}]}), encoding="utf-8"
    )
    extract_epub(_build_epub(tmp_path, _TWO_CHAPTER_PAGE), out, progress=lambda _m: None)
    assert victim.exists()


# ---- image references ---------------------------------------------------------------


def test_resolve_href_normalises_relative_paths():
    assert _resolve_href("Text/ch1.xhtml", "../Images/a%20b.jpg") == "Images/a b.jpg"
    assert _resolve_href("Text/ch1.xhtml", "./pic.png#frag") == "Text/pic.png"
    assert _resolve_href("ch1.xhtml", "img/x.png") == "img/x.png"


def test_images_are_listed_in_reading_order_via_relative_refs(tmp_path):
    epub = _build_multi_epub(
        tmp_path,
        [
            ("p1", '<p>One.</p><img src="../Images/second.jpg"/>'),
            ("p2", '<p>Two.</p><img src="../Images/first.jpg"/>'),
        ],
        images={"Images/first.jpg": b"\xff\xd8a", "Images/second.jpg": b"\xff\xd8b"},
        text_dir="Text",
    )
    meta = extract_epub(epub, tmp_path / "out", progress=lambda _m: None)
    assert meta.images == ["images/second.jpg", "images/first.jpg"]


def _run_all():
    import inspect

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
        else:
            fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

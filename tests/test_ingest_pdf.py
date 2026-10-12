"""`lnvox ingest-pdf` — chapter specs, text cleanup, bookmarks, file layout.

pypdf is never imported: the extractor gets a fake reader, so these run
without the (unlocked) dependency. Runnable directly
(`python tests/test_ingest_pdf.py`) or under pytest. All text is invented.
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.ingest import pdf
from lnvox.ingest.pdf import ChapterSpec, clean_chapter, outline_chapters, parse_chapter_spec

SOFT = "\u00ad"


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def test_parse_chapter_spec():
    assert parse_chapter_spec("5-12:The Gate") == ChapterSpec(5, 12, "The Gate")
    assert parse_chapter_spec(" 7 ") == ChapterSpec(7, 7, "")
    # Only the first ':' separates: titles keep theirs.
    assert parse_chapter_spec("3-4:Part 1: Origins") == ChapterSpec(3, 4, "Part 1: Origins")
    for bad in ("12-5", "0-3", "a-b", "", "5-:x"):
        assert _raises(parse_chapter_spec, bad), bad


# Four printed pages, 61-63 then 66: chapter number + title on the first,
# alternating running heads (book title / chapter title), folios at the foot.
PAGES = [
    (
        "IV\nThe Lantern Keepers\n"
        "The gate stood open all night, and the keep" + SOFT + "\n"
        "ers of the lantern watched it swing in the wind\n"
        "until the oil ran low and the small flame died.\n"
        "Nobody came.\n"
        "The guard waited at the post through the cold-\n"
        "61"
    ),
    (
        "The Open Road\n"
        "est hour of the night, counting the stars above\n"
        "the walls of the city.\n"
        "62"
    ),
    (
        "The Lantern Keepers\n"
        "At dawn the traveller arrived with a letter from\n"
        "the capital, sealed in wax, and the guard read it\n"
        "63"
    ),
    (
        "The Open Road\n"
        "so the keepers lit the lantern again and sang it\n"
        "home.\n"
        "66"
    ),
]


def test_clean_chapter_removes_page_furniture_and_rebuilds_paragraphs():
    assert clean_chapter(PAGES, "The Lantern Keepers") == (
        "The gate stood open all night, and the keepers of the lantern watched "
        "it swing in the wind until the oil ran low and the small flame died. "
        "Nobody came.\n\n"
        # Re-joined across the page turn (61 → 62) and the line hyphen.
        "The guard waited at the post through the coldest hour of the night, "
        "counting the stars above the walls of the city.\n\n"
        # 63 → 66: missing pages, so the fragments stay apart.
        "At dawn the traveller arrived with a letter from the capital, sealed "
        "in wax, and the guard read it\n\n"
        "so the keepers lit the lantern again and sang it home."
    )


def test_ocr_title_scan_specks_and_rule_debris():
    pages = [
        (
            "Lanteru Keepers\n"  # OCR'd title: fuzzy-matched, dropped
            "The gate stood open all night long, and nobody\n"
            "------ - -----came to close it before the dawn.\n"
            "\\ .r'--"  # scan specks at the foot
        ),
    ]
    assert clean_chapter(pages, "Lantern Keepers") == (
        "The gate stood open all night long, and nobody came to close it before the dawn."
    )
    # Without a title nothing at the top is taken for one.
    assert clean_chapter(pages).startswith("Lanteru Keepers The gate")


def test_bookmark_title_numbering_and_date_dont_block_the_match():
    # Bookmark "IV. The Gate (May 1946)"; the page prints only an OCR'd heading.
    pages = ["IV\nThe Gnte\nThe gate stood open all night long, and nobody came to close it."]
    assert clean_chapter(pages, "IV. The Gate (May 1946)") == (
        "The gate stood open all night long, and nobody came to close it."
    )


def test_hyphen_before_a_capital_is_part_of_the_word():
    pages = ["They spoke an old Anglo-\nSaxon dialect at the gate, with a well-\nknown accent."]
    assert clean_chapter(pages) == "They spoke an old Anglo-Saxon dialect at the gate, with a wellknown accent."


class _Item(SimpleNamespace):
    pass


class _FakeReader:
    def __init__(self, texts, outline=(), title=None, images=None):
        """`images`: page index → list of PIL images on that page."""
        images = images or {}
        self.pages = [
            SimpleNamespace(
                extract_text=lambda t=t: t,
                images=[SimpleNamespace(image=im) for im in images.get(i, [])],
            )
            for i, t in enumerate(texts)
        ]
        self.outline = list(outline)
        self.metadata = SimpleNamespace(title=title, author=None) if title else None
        self.is_encrypted = False

    def get_destination_page_number(self, item):
        return item.page


def test_outline_chapters_use_top_level_bookmarks():
    outline = [
        _Item(title="Preface", page=0),
        [_Item(title="A subsection", page=1)],  # children are ignored
        _Item(title="One", page=2),
        _Item(title="Two", page=5),
        _Item(title="Same page", page=5),  # first title on a page wins
    ]
    assert outline_chapters(_FakeReader([""] * 8, outline), 8) == [
        ChapterSpec(1, 2, "Preface"),
        ChapterSpec(3, 5, "One"),
        ChapterSpec(6, 8, "Two"),
    ]


def _extract(reader, out: Path, **kw):
    orig = pdf._open_reader
    pdf._open_reader = lambda _path: reader
    try:
        return pdf.extract_pdf(Path("book.pdf"), out, progress=lambda _m: None, **kw)
    finally:
        pdf._open_reader = orig


def test_extract_writes_chapters_and_replaces_the_previous_run():
    texts = ["", "The first chapter speaks.", "The second chapter answers."]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "notes.txt").write_text("mine", encoding="utf-8")
        meta = _extract(
            _FakeReader(texts), out,
            chapters=[parse_chapter_spec("1-2:Opening"), parse_chapter_spec("3:Reply")],
        )
        assert meta.pages_without_text == [1]
        assert (out / "01-opening.txt").read_text(encoding="utf-8") == (
            "Opening\n\nThe first chapter speaks.\n"
        )
        # The sidecar is ingest-epub's: Stage 6 reads source_parts from it.
        sidecar = json.loads((out / ".epub_meta.json").read_text(encoding="utf-8"))
        assert [c["pages"] for c in sidecar["chapters"]] == [[1, 2], [3, 3]]
        assert [c["source_parts"] for c in sidecar["chapters"]] == [["chapter01"], ["chapter02"]]
        assert sidecar["cover_image"] is None  # page 1 holds no image

        # Bookmarks drive the split when no --chapter is given; the previous
        # run's chapter files go, a file we didn't write stays.
        reader = _FakeReader(texts, [_Item(title="All of it", page=1)], title="The Book")
        meta = _extract(reader, out)
        assert sorted(p.name for p in out.glob("*.txt")) == ["01-all-of-it.txt", "notes.txt"]
        assert meta.title == "The Book"

        # Neither: one chapter named after the PDF's title.
        _extract(_FakeReader(texts, title="The Book"), out, pages=(2, 3))
        assert (out / "01-the-book.txt").read_text(encoding="utf-8").startswith(
            "The Book\n\nThe first chapter speaks. The second"
        )


def test_extract_writes_an_epub_that_reads_back_as_the_same_book():
    from lnvox.ingest.epub import extract_epub
    from lnvox.ingest.epub_writer import is_generated

    texts = ["The gate stood open. Nobody came.", "At dawn the traveller & his <dog> arrived."]
    with tempfile.TemporaryDirectory() as d:
        out, epub = Path(d) / "novel", Path(d) / "epubs" / "book.epub"
        meta = _extract(
            _FakeReader(texts), out,
            chapters=[parse_chapter_spec("1:The Gate"), parse_chapter_spec("2:Dawn")],
            title="The Book", authors=["A. Writer"], epub_path=epub,
        )
        assert meta.epub == epub and is_generated(epub)

        # Stage 6 aligns onto what ingest-epub reads out of the EPUB, so it
        # must be exactly the chapters (escaping included) and the same map.
        back = Path(d) / "back"
        bmeta = extract_epub(epub, back, progress=lambda _m: None)
        assert (bmeta.title, bmeta.authors) == ("The Book", ["A. Writer"])
        for ch in json.loads((out / ".epub_meta.json").read_text(encoding="utf-8"))["chapters"]:
            assert (back / ch["file"]).read_text(encoding="utf-8") == (out / ch["file"]).read_text(encoding="utf-8")
        assert [c["source_parts"] for c in bmeta.chapters] == [["chapter01"], ["chapter02"]]

        # Re-running overwrites our own EPUB; anyone else's is left alone.
        _extract(_FakeReader(texts), out, epub_path=epub)
        foreign = Path(d) / "real.epub"
        foreign.write_bytes(b"PK not ours")
        try:
            _extract(_FakeReader(texts), out, epub_path=foreign)
        except ValueError as e:
            assert "not overwriting" in str(e)
        else:
            raise AssertionError("expected ValueError")
        assert foreign.read_bytes() == b"PK not ours"


def test_cover_is_the_largest_image_capped_and_shared_with_the_epub():
    import pytest

    Image = pytest.importorskip("PIL.Image")
    from lnvox.ingest.epub import extract_epub

    cover = Image.new("RGB", (2000, 3200), (20, 30, 40))
    images = {0: [Image.new("1", (1, 1)), Image.new("RGB", (300, 300)), cover]}
    with tempfile.TemporaryDirectory() as d:
        out, epub = Path(d) / "novel", Path(d) / "book.epub"
        meta = _extract(_FakeReader(["Cover text.", "Body text."], images=images), out, epub_path=epub)
        assert meta.cover_image == "images/cover.jpg"
        with Image.open(out / meta.cover_image) as im:
            assert im.size == (1000, 1600)  # long side capped at COVER_MAX_PX
        sidecar = json.loads((out / ".epub_meta.json").read_text(encoding="utf-8"))
        assert sidecar["cover_image"] == sidecar["images"][0] == "images/cover.jpg"
        assert extract_epub(epub, Path(d) / "back", progress=lambda _m: None).cover_image

        # --cover-page 0 → no cover anywhere.
        meta = _extract(_FakeReader(["Cover text.", "Body text."], images=images), out, cover_page=None)
        assert meta.cover_image is None


def test_cover_file_keeps_the_current_cover_or_converts_a_new_one():
    import pytest

    Image = pytest.importorskip("PIL.Image")
    texts = ["Cover text.", "Body text."]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "novel"
        current = out / "images" / "cover.jpg"
        current.parent.mkdir(parents=True)
        Image.new("RGB", (1050, 1600), (10, 20, 30)).save(current, "JPEG", quality=90)
        before = current.read_bytes()
        # The folder's own cover: kept byte for byte, despite page 1's image.
        page_cover = {0: [Image.new("RGB", (400, 600), (200, 0, 0))]}
        meta = _extract(_FakeReader(texts, images=page_cover), out, cover_file=current)
        assert meta.cover_image == "images/cover.jpg" and current.read_bytes() == before
        # A large PNG elsewhere: converted to JPEG with the long side capped.
        png = Path(d) / "art.png"
        Image.new("RGB", (2400, 3600), (0, 90, 0)).save(png)
        _extract(_FakeReader(texts), out, cover_file=png)
        with Image.open(current) as im:
            assert (im.format, im.size) == ("JPEG", (1067, 1600))
        try:
            _extract(_FakeReader(texts), out, cover_file=Path(d) / "missing.jpg")
        except ValueError as e:
            assert "not found" in str(e)
        else:
            raise AssertionError("expected ValueError")


def test_extract_refuses_a_pdf_without_a_text_layer():
    with tempfile.TemporaryDirectory() as d:
        try:
            _extract(_FakeReader(["", " \n"]), Path(d))
        except ValueError as e:
            assert "OCR" in str(e)
        else:
            raise AssertionError("expected ValueError")
        assert not any(Path(d).iterdir())  # nothing written


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

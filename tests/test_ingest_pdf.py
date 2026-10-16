"""`lnvox ingest-pdf` — chapter specs, text cleanup, bookmarks, file layout.

pypdf is never imported: the extractor gets a fake reader, so these run
without the (unlocked) dependency. Runnable directly
(`python tests/test_ingest_pdf.py`) or under pytest. All text is invented.
"""

import json
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.ingest import pdf
from lnvox.ingest.pdf import (
    ChapterSpec,
    Fragment,
    body_style,
    clean_chapter,
    filter_page,
    outline_chapters,
    parse_chapter_spec,
)

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


def test_ligature_gaps_are_closed_but_ff_word_ends_are_kept():
    fi, fl, ff = "ﬁ", "ﬂ", "ﬀ"
    pages = [
        f"It is dif{fi}  cult to be speci{fi} c about the in{fl} uence of the\n"
        f"first ef{fi}  -\ncient {fi} gure, said Ciof{fi}  , and then turned o{ff} the light."
    ]
    assert clean_chapter(pages) == (
        "It is difficult to be specific about the influence of the first efficient "
        "figure, said Cioffi, and then turned off the light."
    )


def test_hyphen_before_a_capital_is_part_of_the_word():
    pages = ["They spoke an old Anglo-\nSaxon dialect at the gate, with a well-\nknown accent."]
    assert clean_chapter(pages) == "They spoke an old Anglo-Saxon dialect at the gate, with a wellknown accent."


def F(font, size, x, y, text):  # noqa: N802 — reads like the Fragment it builds
    return Fragment(font, size, x, y, text)


# One typeset page as pypdf reports it: 9 pt Serif running text, a running
# head and folio, a two-style heading, a figure whose axis label precedes its
# "Figure" label, a block quote, list items, an indented paragraph, and note
# markers both inline and as a superscript run pypdf ran the line on in.
TYPESET_PAGE = [
    F("Sans-Semibold", 8.0, 72, 600, "12 The Lantern Book\n"),
    F("Serif", 9.0, 72, 580, "the keepers had watched the gate since the war began.\n"),
    F("Sans-Semibold", 9.0, 72, 554, "The Night "),
    F("Sans-SemiboldItalic", 9.0, 118, 554, "Watch"),
    F("Serif", 9.0, 118, 554, "\n"),
    F("Serif", 9.0, 72, 528, "Nobody came to the gate that night, and the guard\n"),
    F("Serif", 9.0, 72, 515, "counted the stars until dawn.4 He wrote his letter\n"),
    F("Serif", 9.0, 72, 502, "with a pen that the captain had left on the table\n"),
    F("Sans", 9.0, 100, 450, "Light Level\n"),
    F("Sans-Semibold", 8.0, 72, 400, "Figure 1.1"),
    F("Serif", 8.0, 72, 400, "\n"),
    F("Serif", 8.0, 72, 388, "The lantern's light over one night.\n"),
    F("Serif", 9.0, 72, 360, "before the war, and the letter read:\n"),
    F("Serif", 8.0, 81, 342, "Keep the lantern lit until the traveller comes home\n"),
    F("Serif", 8.0, 81, 330, "with the news from the capital.\n"),
    F("Serif", 9.0, 81, 312, "So the guard lit the lantern again and again.\n"),
    F("Serif", 9.0, 81, 299, "The next morning he wrote down two rules for the gate:\n"),
    F("Serif", 9.0, 72, 286, "1. Never let the oil run low before the night is over.\n"),
    F("Serif", 9.0, 72, 273, "2. Never leave the gate open when the wind is high.\n"),
    F("Serif", 9.0, 81, 260, "The traveller left at noon and was known as the "),
    F("Serif-Italic", 9.0, 290, 260, "lantern man."),
    F("Serif", 5.4, 340, 263.6, "7 Nobody saw him again.\n"),
    F("Sans-Semibold", 8.0, 300, 230, "13\n"),
]


def test_font_filter_keeps_the_running_text_and_its_paragraphs():
    assert body_style([TYPESET_PAGE])[:2] == ("Serif", 9.0)
    text = clean_chapter([filter_page(TYPESET_PAGE, "Serif", 9.0)])
    assert text.split("\n\n") == [
        "the keepers had watched the gate since the war began.",
        "The Night Watch",  # its own paragraph, both styles rejoined
        # The figure is gone — axis label, "Figure" label and caption — and
        # the sentence it interrupted runs on; the marker "4" is gone.
        "Nobody came to the gate that night, and the guard counted the stars until "
        "dawn. He wrote his letter with a pen that the captain had left on the table "
        "before the war, and the letter read:",
        "Keep the lantern lit until the traveller comes home with the news from the capital.",
        "So the guard lit the lantern again and again.",
        "The next morning he wrote down two rules for the gate:",
        "1. Never let the oil run low before the night is over.",
        "2. Never leave the gate open when the wind is high.",
        # Indented: a new paragraph, though the line above is full width.
        # The superscript "7" is dropped, the text pypdf ran on in it kept.
        "The traveller left at noon and was known as the lantern man. Nobody saw him again.",
    ]


def test_font_filter_details():
    # Font names: the family, whatever the style suffix.
    assert {pdf._family(n) for n in ("SourceSerif-Italic", "TimesNewRomanPSMT", "TimesNewRomanPS-BoldMT")} == {
        "SourceSerif",
        "TimesNewRoman",
    }
    assert pdf._family("Arial,Bold") == "Arial"
    # A page without running text (a half-title) yields no heading.
    assert filter_page([F("Sans-Semibold", 11.0, 72, 500, "The Lantern Book")], "Serif", 9.0).strip() == ""
    # Body-size note markers go; abbreviations, years and pitch names stay.
    page = [F("Serif", 9.0, 72, 500, t) for t in (
        "It was settled. 4 Most agreed (in the 1960s). 12 Then came Op. 27 No. 2,\n",
        "played at p. 12 in 1946. 12 people heard the C4 and the B3. Or so.” 9\n",
    )]
    assert filter_page(page, "Serif", 9.0) == (
        "It was settled. Most agreed (in the 1960s). Then came Op. 27 No. 2,\n"
        "played at p. 12 in 1946. 12 people heard the C4 and the B3. Or so.”\n"
    )


MO, MC, FO, FC = pdf._MARK_OPEN, pdf._MARK_CLOSE, pdf._FIG_OPEN, pdf._FIG_CLOSE

# A page with a note marker and a figure: lettering, then label and caption
# under the drawing, between two stretches of running text.
FIGURE_PAGE = [
    F("Serif", 9.0, 72, 560, "The keepers watched the gate.1 Figure 1.1 shows the\n"),
    F("Serif", 9.0, 72, 547, "lantern they kept burning all through the long night.\n"),
    F("Times-Roman", 8.0, 120, 450, "lantern\n"),
    F("Sans-Semibold", 8.0, 72, 400, "Figure 1.1"),
    F("Serif", 8.0, 72, 400, "\n"),
    F("Serif", 8.0, 72, 388, "The lantern over one night, as the keepers saw it.\n"),
    F("Serif", 9.0, 81, 360, "So the guard lit it again and went home at dawn.\n"),
]


def test_read_page_marks_note_markers_and_figures():
    page = pdf.read_page(FIGURE_PAGE, "Serif", 9.0, page_no=7)
    assert f"gate.{MO}1{MC} Figure 1.1 shows" in page.text
    assert f"{FO}p7f0{FC}" in page.text and "lantern\n" not in page.text
    (zone,) = page.figures
    assert (zone.label, zone.token) == ("Figure 1.1", "p7f0")
    assert zone.caption == "Figure 1.1 The lantern over one night, as the keepers saw it."
    assert [f.text for f in zone.frags][0] == "lantern\n"  # the lettering read before the label
    assert all(f.font == "Serif" and f.size == 9.0 for f in page.kept)
    # Caption under the drawing: from the text above down to the caption's foot.
    top, bottom = pdf.figure_band(zone, page.kept, FIGURE_PAGE, 648, 0, page.figures)
    assert (round(top, 1), round(bottom, 1)) == (round(547 - 0.3 * 9, 1), round(388 - 0.3 * 8 - 1, 1))


def test_table_band_runs_from_its_label_down():
    page = [
        F("Serif", 9.0, 72, 560, "The guards kept a tally of the nights they watched the gate.\n"),
        F("Sans-Semibold", 8.0, 72, 500, "Table 2.1"),
        F("Serif", 8.0, 72, 500, "\n"),
        F("Serif", 8.0, 72, 488, "Nights watched, by guard\n"),
        F("Serif", 8.0, 66, 470, " Guard Nights\n"),
        F("Serif", 8.0, 66, 458, " Anna 12\n"),
        F("Serif", 9.0, 72, 400, "Anna watched more nights than all the others together.\n"),
        F("Sans-Semibold", 8.0, 300, 60, "13\n"),
    ]
    read = pdf.read_page(page, "Serif", 9.0)
    (zone,) = read.figures
    assert zone.caption == "Table 2.1 Nights watched, by guard"
    top, bottom = pdf.figure_band(zone, read.kept, page, 648, 0, read.figures)
    assert (round(top, 1), round(bottom, 1)) == (round(500 + 0.85 * 8 + 1, 1), round(400 + 0.85 * 9, 1))


def test_markers_are_claimed_by_notes_or_put_back():
    notes = {4: "n4", 5: "n5", 6: "n6"}
    body = (
        f"It was settled.{MO}4{MC} Most agreed.{MO} 12{MC} Nobody objected.\n\n"
        f"The mark was letter A.5 Others say so. {FO}p7f0{FC}"
    )
    used: set[int] = set()
    body, restored = pdf._resolve_markers(body, notes, used)
    # 4 claimed; 12 has no note, so it was text; 5 found late between its
    # neighbours ("A." is no lowercase word); 6 appears nowhere.
    assert (used, restored) == ({4, 5}, 1)
    paragraphs, marks = pdf._split_marks(body)
    assert paragraphs == [
        "It was settled. Most agreed. 12 Nobody objected.",
        "The mark was letter A. Others say so.",
    ]
    assert [(m.kind, m.paragraph, m.offset, m.payload.strip()) for m in marks] == [
        ("note", 0, len("It was settled."), "4"),
        ("note", 1, len("The mark was letter A."), "5"),
        ("figure", 1, len(paragraphs[1]), "p7f0"),
    ]
    # No notes parsed: markers just go.
    assert pdf._resolve_markers(f"Done.{MO}3{MC} Next.", None, set()) == ("Done. Next.", 0)


def test_first_mentions_and_labels():
    mentions = pdf._first_mentions(
        ["Nothing here.", "See figure 2.1 and tables 3.1–3.2.", "Table 4.2a shows it; figure 2.1 again."]
    )
    assert mentions == {("figure", "2.1"): 1, ("table", "3.1"): 1, ("table", "3.2"): 1, ("table", "4.2a"): 2}
    assert pdf._label_key("Table 4.2a") == ("table", "4.2a")
    assert pdf._label_key("Fig. 2") == ("figure", "2")


NOTES_PAGES = [
    (10, [
        F("Sans-Semibold", 11.0, 72, 560, "Notes\n"),
        F("Sans-Semibold", 8.0, 72, 530, "Preface\n"),
        F("Serif", 8.0, 72, 512, "1. See Smith 1990.\n"),
        F("Sans-Semibold", 8.0, 72, 488, "Chapter 1\n"),
        F("Serif", 8.0, 72, 470, "1. The first note runs over two\n"),
        F("Serif", 8.0, 72, 458, "lines and goes on.\n"),
        F("Serif", 8.0, 80, 446, "A second paragraph of it.\n"),
        F("Serif", 8.0, 72, 428, "2. Short.\n"),
        F("Serif", 8.0, 72, 410, "3. A note that continues over the page\n"),
    ]),
    (11, [
        F("Sans-Semibold", 8.0, 72, 606, "212 Notes\n"),
        F("Serif", 8.0, 72, 570, "break, citing a list:\n"),
        F("Serif", 8.0, 72, 558, "12. and more of note 3.\n"),
        F("Serif", 8.0, 72, 540, "11. A stray note, numbered wrong in print.\n"),
    ]),
]


def test_parse_notes_groups_paragraphs_and_page_breaks():
    from lnvox.ingest.pdf_notes import chapter_key, group_key, parse_notes

    notes = parse_notes(NOTES_PAGES)
    assert [(n.group, n.number) for n in notes] == [
        ("Preface", 1), ("Chapter 1", 1), ("Chapter 1", 2), ("Chapter 1", 3), ("Chapter 1", 11)
    ]
    assert notes[1].paragraphs == ["The first note runs over two lines and goes on.", "A second paragraph of it."]
    # Over the page break and its running head; a line opening "12." set
    # solid under the last is still this note.
    assert notes[3].text == "A note that continues over the page break, citing a list: 12. and more of note 3."
    assert [s[0] for s in notes[3].segments] == [10, 11]
    assert group_key("Chapter 1") == chapter_key("1 The Lantern") == "#1"
    assert group_key("Preface") == chapter_key("Preface") == "preface"


class _Item(SimpleNamespace):
    pass


def _visit(fragments):
    """A fake `page.extract_text`: reports `fragments` to `visitor_text`
    the way pypdf does, subset-prefixed font names included."""

    def extract_text(visitor_text=None):
        for f in fragments:
            if visitor_text:
                visitor_text(f.text, [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, f.x, f.y], {"/BaseFont": f"/ABCDEF+{f.font}"}, f.size)
        return "".join(f.text for f in fragments)

    return extract_text


class _FakeReader:
    def __init__(self, texts, outline=(), title=None, images=None, fragments=None):
        """`images`: page index → list of PIL images on that page;
        `fragments`: page index → its `Fragment`s (overrides the text)."""
        images = images or {}
        fragments = fragments or {}
        self.pages = [
            SimpleNamespace(
                extract_text=_visit(fragments[i]) if i in fragments else (lambda visitor_text=None, t=t: t),
                images=[SimpleNamespace(image=im) for im in images.get(i, [])],
                cropbox=SimpleNamespace(top=648.0, bottom=0.0),
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


def test_extract_with_font_filter_reads_fonts_from_the_page():
    fragments = {0: TYPESET_PAGE, 1: [F("Serif", 9.0, 72, 580, "Then the lantern went out for good.\n")]}
    spec = [parse_chapter_spec("1-2:The Lantern")]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _extract(_FakeReader(["", ""], fragments=fragments), out, chapters=spec, font_filter=True)
        text = (out / "01-the-lantern.txt").read_text(encoding="utf-8")
        assert "\n\nThe Night Watch\n\n" in text
        assert not any(junk in text for junk in ("Lantern Book", "Figure", "Light Level", "lantern's light", "13"))
        assert text.endswith("Nobody saw him again. Then the lantern went out for good.\n")
        # Without the filter the same page reads as pypdf's plain text.
        _extract(_FakeReader(["", ""], fragments=fragments), out, chapters=spec)
        assert "Figure 1.1" in (out / "01-the-lantern.txt").read_text(encoding="utf-8")


class _FakeRenderer:
    """Stands in for pypdfium2: a black block as tall as the band asked for."""

    def __init__(self, _pdf_path):
        pass

    def band(self, page, top, bottom):
        from PIL import Image

        return Image.new("RGB", (200, max(2, int(top - bottom))), "black")

    def close(self):
        pass


def test_extract_renders_figures_and_ties_notes_for_the_reader():
    import pytest

    Image = pytest.importorskip("PIL.Image")
    from lnvox.ingest import pdf_render
    from lnvox.ingest.epub import extract_epub

    def fake_cover(_pdf, _page, out, _max_px):
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (60, 90), "teal").save(out, "JPEG")

    notes_page = [f for _, frags in NOTES_PAGES[:1] for f in frags]
    reader = _FakeReader(
        ["", ""],
        outline=[_Item(title="1 The Lantern", page=0), _Item(title="Notes", page=1)],
        fragments={0: FIGURE_PAGE, 1: notes_page},
    )
    saved = pdf_render.PageRenderer, pdf_render.render_page
    pdf_render.PageRenderer, pdf_render.render_page = _FakeRenderer, fake_cover
    try:
        with tempfile.TemporaryDirectory() as d:
            out, epub = Path(d) / "novel", Path(d) / "book.epub"
            meta = _extract(reader, out, font_filter=True, epub_path=epub)
            assert (meta.figures, meta.notes) == (1, 1)
            text = (out / "01-1-the-lantern.txt").read_text(encoding="utf-8")
            assert "The keepers watched the gate. Figure 1.1 shows the lantern" in text
            assert "lantern\n" not in text and "over one night" not in text

            side = json.loads((out / ".epub_meta.json").read_text(encoding="utf-8"))
            # Stage 5 embeds the cover, then the figure; Stage 6 finds it in the EPUB.
            assert side["images"] == ["images/cover.jpg", "images/figure-1-1.png"]
            (note,) = side["visual_elements"]
            assert (note["kind"], note["label"], note["src"]) == ("footnote", "1", "images/note-01-1.png")
            assert note["text"] == "The first note runs over two lines and goes on.\n\nA second paragraph of it."
            assert note["anchor"] == {"paragraph": 0, "text": "The keepers watched the gate."}
            assert sorted(side["rendered"]) == ["images/figure-1-1.png", "images/note-01-1.png"]

            with zipfile.ZipFile(epub) as z:
                page = z.read("OEBPS/text/chapter01.xhtml").decode("utf-8")
                assert "OEBPS/images/note-01-1.png" in z.namelist()
            # The figure goes before the paragraph that first mentions it; the
            # noteref sits where the marker stood, the note at the chapter's end.
            assert page.index("<figure") < page.index("<p>")
            assert 'alt="Figure 1.1 The lantern over one night, as the keepers saw it."' in page
            assert 'gate.<a epub:type="noteref"' in page and 'data-note="1"></a> Figure 1.1' in page
            assert page.index('epub:type="endnote"') > page.rindex("</p>")
            # Read back, the EPUB is still exactly the chapter text.
            back = extract_epub(epub, Path(d) / "back", progress=lambda _m: None)
            assert (Path(d) / "back" / back.chapters[0]["file"]).read_text(encoding="utf-8") == (
                out / "01-1-the-lantern.txt"
            ).read_text(encoding="utf-8")

            # A re-extraction without notes drops the previous run's renders.
            _extract(reader, out, font_filter=True, notes=False, figures=False, epub_path=epub)
            assert not (out / "images" / "note-01-1.png").exists()
            assert not (out / "images" / "figure-1-1.png").exists()
    finally:
        pdf_render.PageRenderer, pdf_render.render_page = saved


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

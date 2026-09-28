"""Stage 6 OPF parsing: which manifest items are pages, and stem collisions.

Index v15-v17 ship `Text/toc.xhtml` AND `toc.ncx`. Items were keyed by bare
stem and anything with "xml" in its media-type counted as a page, so the NCX
overwrote the TOC page and Stage 6 parsed navigation XML as HTML — the
XMLParsedAsHTMLWarning users saw in step 6.
"""

import warnings
import zipfile

from bs4 import XMLParsedAsHTMLWarning

from lnvox.stages.s6_sync import _parse_opf

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _epub(tmp_path, manifest_items: str, spine: str, files: dict[str, str]):
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<manifest>{manifest_items}</manifest><spine toc=\"ncx\">{spine}</spine></package>"
    )
    p = tmp_path / "book.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for name, data in files.items():
            z.writestr(f"OEBPS/{name}", data)
    return p


XHTML = '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>'
NCX = '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap/></ncx>'


def test_ncx_never_shadows_the_page_with_the_same_stem(tmp_path):
    epub = _epub(
        tmp_path,
        '<item id="toc" href="Text/toc.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<itemref idref="toc"/>',
        {"Text/toc.xhtml": XHTML, "toc.ncx": NCX},
    )
    _, stem_to_href, spine = _parse_opf(epub)
    assert stem_to_href["toc"] == "Text/toc.xhtml"
    assert spine == ["toc"]
    assert "toc.ncx" not in stem_to_href.values()


def test_spine_page_wins_a_stem_collision_whatever_the_manifest_order(tmp_path):
    """`cover.svg` art listed AFTER `cover.xhtml` must not replace the page."""
    epub = _epub(
        tmp_path,
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="coverart" href="images/cover.svg" media-type="image/svg+xml"/>',
        '<itemref idref="cover"/>',
        {"cover.xhtml": XHTML, "images/cover.svg": "<svg/>"},
    )
    _, stem_to_href, spine = _parse_opf(epub)
    assert stem_to_href["cover"] == "cover.xhtml"
    assert spine == ["cover"]


def test_svg_content_document_in_the_spine_is_still_a_page(tmp_path):
    """EPUB 3 allows full-page SVG as a spine item; don't drop it."""
    epub = _epub(
        tmp_path,
        '<item id="art" href="art.svg" media-type="image/svg+xml"/>',
        '<itemref idref="art"/>',
        {"art.svg": "<svg/>"},
    )
    _, stem_to_href, spine = _parse_opf(epub)
    assert spine == ["art"] and stem_to_href["art"] == "art.svg"


def test_non_content_items_are_not_pages(tmp_path):
    epub = _epub(
        tmp_path,
        '<item id="p" href="p.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="css" href="p.css" media-type="text/css"/>'
        '<item id="img" href="p.jpg" media-type="image/jpeg"/>'
        '<item id="ncx" href="nav.ncx" media-type="application/x-dtbncx+xml"/>',
        '<itemref idref="p"/>',
        {"p.xhtml": XHTML},
    )
    _, stem_to_href, _ = _parse_opf(epub)
    assert set(stem_to_href) == {"p"}


def test_s6_run_on_such_a_book_raises_no_xml_as_html_warning(tmp_path):
    """The symptom itself: step 6 on a toc.xhtml + toc.ncx book."""
    import json
    epub = _epub(
        tmp_path,
        '<item id="ch" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="toc" href="Text/toc.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<itemref idref="toc"/><itemref idref="ch"/>',
        {"Text/ch1.xhtml": XHTML.replace("<p>x</p>", "<p>A lantern swung.</p>"),
         "Text/toc.xhtml": XHTML, "toc.ncx": NCX},
    )
    novel = tmp_path / "novel"; novel.mkdir()
    (novel / ".epub_meta.json").write_text(json.dumps({
        "title": "t", "images": [], "visual_elements": [],
        "chapters": [{"file": "01-one.txt", "title": "One", "source_parts": ["ch1"]}]}))
    book = tmp_path / "art"
    (book / "03_directed").mkdir(parents=True)
    (book / "03_directed/01.json").write_text(json.dumps({"chapter_id": "01", "scenes": [
        {"scene_id": "01_s1", "beats": [{"type": "narration", "text": "A lantern swung.",
         "source_span": "A lantern swung.", "speaker": "Narrator", "direction": "d", "prompt": "p"}]}]}))
    (book / "05_audio/01").mkdir(parents=True)
    (book / "05_audio/01/manifest.json").write_text(json.dumps({"beats": [
        {"beat_id": "01_s1_b0000", "scene_id": "01_s1", "duration_seconds": 2.0}]}))

    from lnvox.stages import s6_sync
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s6_sync.run("x", book, epub, novel, book / "07_sync", progress=lambda _m: None)
    assert not [w for w in caught if issubclass(w.category, XMLParsedAsHTMLWarning)]

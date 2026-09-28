"""Regression tests for Stage 6 EPUB sync (DESIGN.md §2.8).

Covers three DOM-corruption bugs found in review (2026-07-03), all latent —
no shipped book had tripped them, but each silently corrupts the synced EPUB
when it fires:

  1. `_wrap_matches` located the node to replace with `list.index()`, which
     compares NavigableStrings by VALUE — two identical text nodes under one
     parent made the replacement land at the first twin, reordering the
     chapter text.
  2. `_build_shadow` let Comment/CDATA/etc. (NavigableString subclasses)
     into the shadow, so a match landing inside a comment turned hidden
     markup into visible book text.
  3. `_normalize_with_map` emitted one map entry per INPUT char, but a few
     codepoints lowercase to more than one char ('İ' → 'i̇'), shifting every
     wrap offset after them.

Runnable directly (`python tests/test_s6_sync.py`) or under pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bs4 import BeautifulSoup

from lnvox.stages.s6_sync import (
    _build_shadow,
    _Match,
    _normalize_with_map,
    _wrap_matches,
)


# ---- normalization -----------------------------------------------------------


def test_normalize_map_stays_aligned_on_multichar_lowercase():
    norm, orig = _normalize_with_map("İstanbul is a city")
    assert len(norm) == len(orig)
    # Both output chars of the expanded 'İ' map back to original index 0.
    assert orig[0] == 0 and orig[1] == 0
    # The rest of the map still points at sensible original positions.
    assert "istanbul" in norm.replace("̇", "")


def test_normalize_collapses_whitespace_and_quotes():
    norm, orig = _normalize_with_map("“Hello,”\n\n  said—he")
    assert norm == '"hello," said-he'
    assert len(norm) == len(orig)


# ---- shadow building ----------------------------------------------------------


def test_shadow_excludes_comments_and_processing_instructions():
    soup = BeautifulSoup(
        "<html><body><p>Real text.</p><!-- secret comment -->"
        "<p>More text.</p></body></html>",
        "html.parser",
    )
    shadow, segments, _ = _build_shadow(soup)
    assert "secret" not in shadow
    assert "real text" in shadow and "more text" in shadow
    # Only the two visible text nodes became segments.
    assert len(segments) == 2


# ---- DOM wrapping --------------------------------------------------------------


def test_wrap_uses_node_identity_not_string_equality():
    # Two IDENTICAL text nodes ("Boom. ") under one parent. Wrapping a match
    # in the SECOND must not move text to the first twin's position.
    soup = BeautifulSoup(
        "<html><body><p>Boom. <i>crash</i>Boom. <i>thud</i>end</p></body></html>",
        "html.parser",
    )
    shadow, segments, _ = _build_shadow(soup)
    second = shadow.find("boom.", shadow.find("boom.") + 1)
    _wrap_matches(soup, segments, [_Match("B1", second, second + 5)])
    out = str(soup)
    # The span wraps the second Boom: it must appear AFTER <i>crash</i>…
    assert out.index("<span") > out.index("<i>crash</i>")
    # …and the document text order is preserved.
    assert soup.get_text() == "Boom. crashBoom. thudend"


def test_wrap_multiple_beats_in_one_node():
    soup = BeautifulSoup(
        "<html><body><p>First sentence. Second sentence.</p></body></html>",
        "html.parser",
    )
    shadow, segments, _ = _build_shadow(soup)
    a = shadow.find("first sentence.")
    b = shadow.find("second sentence.")
    _wrap_matches(
        soup,
        segments,
        [_Match("A", a, a + 15), _Match("B", b, b + 16)],
    )
    spans = soup.find_all("span", class_="lnvox-beat")
    assert [s["data-beat-id"] for s in spans] == ["A", "B"]
    assert spans[0].string == "First sentence."
    assert spans[1].string == "Second sentence."
    assert soup.get_text() == "First sentence. Second sentence."


# ---- matching ladder -----------------------------------------------------------


def _one_page_book(root: Path, page_text: str, beats: list[str]):
    """Minimal EPUB + ingest + directed/rendered artifacts: one chapter, one page."""
    import json
    import zipfile

    epub = root / "b.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0"><manifest><item id="p" href="c.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="p"/></spine></package>')
        z.writestr("c.xhtml", '<?xml version="1.0" encoding="utf-8"?><html '
                   'xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
                   f"<body><p>{page_text}</p></body></html>")
    novel = root / "novel"
    novel.mkdir()
    (novel / ".epub_meta.json").write_text(json.dumps({
        "images": [], "visual_elements": [],
        "chapters": [{"file": "01-x.txt", "title": "x", "source_parts": ["c"]}],
    }))
    book = root / "book"
    (book / "03_directed").mkdir(parents=True)
    (book / "05_audio" / "01").mkdir(parents=True)
    (book / "03_directed" / "01.json").write_text(json.dumps({
        "chapter_id": "01", "scenes": [{"scene_id": "01_s1", "beats": [
            {"type": "narration", "text": t, "speaker": "Narrator",
             "direction": "d", "prompt": "p"} for t in beats
        ]}],
    }))
    (book / "05_audio" / "01" / "manifest.json").write_text(json.dumps({"beats": [
        {"beat_id": f"01_s1_b{i:04d}", "scene_id": "01_s1", "duration_seconds": 1.0}
        for i in range(len(beats))
    ]}))
    return book, novel, epub


def test_pass2_slack_places_a_beat_stage2_moved_after_its_neighbour():
    """Stage 2 sometimes emits a line of dialogue BEFORE the attribution that
    precedes it in the source. The attribution then sits behind Pass 1's
    cursor. Pass 2 searches from 2000 chars before the previous match, which
    is what finds it — the retired `backtrack` rung (1500 chars before the
    cursor) never could, so its histogram slot stays 0."""
    import tempfile

    from lnvox.stages import s6_sync

    assert not hasattr(s6_sync, "_BACKTRACK_WINDOW")
    page = ("Patrick admitted, “I lost the map somewhere past the old bridge.” "
            "Nobody spoke for a while after that.")
    beats = [
        "I lost the map somewhere past the old bridge.",   # dialogue first …
        "Patrick admitted,",                               # … attribution second
        "Nobody spoke for a while after that.",
    ]
    import re
    import zipfile

    with tempfile.TemporaryDirectory() as d:
        book, novel, epub = _one_page_book(Path(d), page, beats)
        m = s6_sync.run("x", book, epub, novel, book / "07_sync", progress=lambda _m: None)
        with zipfile.ZipFile(m["epub"]) as z:
            order = re.findall(r'data-beat-id="([^"]+)"', z.read("c.xhtml").decode())
    by_id = {b["beat_id"]: b for b in m["beats"]}
    assert m["matched"] == 3, m["beats"]
    assert by_id["01_s1_b0001"]["match_confidence"] == "exact+pass2"
    # Wrapped where the source has it: attribution, then the dialogue.
    assert order == ["01_s1_b0001", "01_s1_b0000", "01_s1_b0002"], order
    assert m["match_confidence"]["backtrack"] == 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

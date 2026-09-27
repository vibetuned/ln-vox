"""Stage 6 synced-EPUB output: beat spans and viewport containment.

Both bugs below were reported from a real reader against the demo build:

  1. A `--split-chapters` ingest (DESIGN.md §18) puts several chapters in ONE
     spine file. Stage 6 re-read that file from the zip per chapter and
     overwrote its entry in `modifications`, so only the last chapter's beat
     spans survived — Oz shipped 102 of 1396, Flatland 23 of 687, and reader
     auto-follow had nothing to anchor to. The one-chapter-per-file books were
     unaffected, which is why it went unnoticed.
  2. Gutenberg's Ebookmaker sizes illustrations in absolute pixels and ships
     no max-width rule, so the reader scrolled sideways and chapter-heading
     images (which in the illustrated editions ARE the headings) clipped.
"""

import zipfile
from pathlib import Path

from lnvox.stages.s6_sync import (
    _RESPONSIVE_MARKER,
    _inject_responsive_css,
    _write_modified_epub,
)

PAGE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
    '<body><img src="i.jpg" style="width: 600px; height: 730px"/>'
    "<p>A lantern swung over the gate.</p></body></html>"
).encode()


def _epub(tmp_path: Path, pages: dict[str, bytes]) -> Path:
    p = tmp_path / "src.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        for name, data in pages.items():
            z.writestr(name, data)
    return p


# ---- responsive containment -------------------------------------------------


def test_injects_style_into_head():
    out = _inject_responsive_css(PAGE)
    assert out is not None
    assert _RESPONSIVE_MARKER.encode() in out
    assert b"max-width:100%!important" in out


def test_height_auto_is_present_so_capping_width_keeps_aspect_ratio():
    """Capping width against a fixed pixel height would distort every image."""
    out = _inject_responsive_css(PAGE)
    assert b"height:auto!important" in out


def test_injection_is_idempotent():
    once = _inject_responsive_css(PAGE)
    assert _inject_responsive_css(once) is None


def test_page_without_head_is_left_alone():
    assert _inject_responsive_css(b"<p>bare fragment</p>") is None


def test_every_xhtml_gets_the_style_even_with_no_beats(tmp_path):
    src = _epub(tmp_path, {"a.xhtml": PAGE, "b.xhtml": PAGE})
    out = tmp_path / "out.epub"
    _write_modified_epub(src, {}, out)          # no modifications at all
    with zipfile.ZipFile(out) as z:
        for name in ("a.xhtml", "b.xhtml"):
            assert _RESPONSIVE_MARKER.encode() in z.read(name)


def test_non_xhtml_entries_are_copied_untouched(tmp_path):
    src = _epub(tmp_path, {"a.xhtml": PAGE, "i.jpg": b"\xff\xd8binary"})
    out = tmp_path / "out.epub"
    _write_modified_epub(src, {}, out)
    with zipfile.ZipFile(out) as z:
        assert z.read("i.jpg") == b"\xff\xd8binary"


def test_mimetype_stays_first_and_stored(tmp_path):
    """EPUB spec: `mimetype` must be the first entry and uncompressed."""
    src = _epub(tmp_path, {"a.xhtml": PAGE})
    out = tmp_path / "out.epub"
    _write_modified_epub(src, {}, out)
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"


# ---- modifications survive alongside injection ------------------------------


def test_modified_page_keeps_its_beat_spans_and_gains_the_style(tmp_path):
    src = _epub(tmp_path, {"a.xhtml": PAGE})
    wrapped = PAGE.replace(
        b"<p>A lantern swung over the gate.</p>",
        b'<p><span data-beat-id="s1_b0000">A lantern swung over the gate.</span></p>',
    )
    out = tmp_path / "out.epub"
    _write_modified_epub(src, {"a.xhtml": wrapped}, out)
    with zipfile.ZipFile(out) as z:
        got = z.read("a.xhtml")
    assert b'data-beat-id="s1_b0000"' in got     # the wrap survived
    assert _RESPONSIVE_MARKER.encode() in got    # and the style was added


def test_mimetype_is_moved_first_when_the_source_has_it_later(tmp_path):
    """Publisher EPUBs sometimes put `mimetype` mid-archive (Index v07-v09:
    entry 21-26). The synced copy must not inherit that."""
    src = tmp_path / "bad.epub"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("content.opf", "<package/>", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("a.xhtml", PAGE)
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_DEFLATED)       # wrong place AND compressed
    out = tmp_path / "out.epub"
    _write_modified_epub(src, {}, out)
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert [i.filename for i in infos].count("mimetype") == 1   # not duplicated
        assert z.read("content.opf") == b"<package/>"

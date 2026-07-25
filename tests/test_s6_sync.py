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


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

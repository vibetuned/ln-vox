"""Tests for lecture mode (DESIGN.md §13).

Covers the deterministic, LLM-free pieces: block classification, page-drop
detection, the beat split, prompt assembly, the HTML render builder, and the
Stage-6 visual-element placement. Runnable directly (`python tests/test_lecture.py`)
or under pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bs4 import BeautifulSoup

from lnvox.ingest import blocks, render
from lnvox.ingest.blocks import classify_page, page_drop_reason
from lnvox.ingest.text import Chapter
from lnvox.stages import lecture
from lnvox.stages.s6_sync import place_visual_element


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{html}</body></html>", "html.parser")


# ---- block classification ---------------------------------------------------


def test_classify_prose_code_table_figure():
    soup = _soup(
        "<h1>Chapter One</h1>"
        "<p>Plain narratable prose.</p>"
        "<pre><code>print('hi')</code></pre>"
        "<table><tr><td>a</td></tr></table>"
        "<figure><img src='Fig1.png'/><figcaption>cap</figcaption></figure>"
        "<p>More prose.</p>"
    )
    title, bs = classify_page(soup, classifier="none")
    assert title == "Chapter One"
    kinds = [b.kind for b in bs]
    # The <h1> title is NOT re-emitted as a block.
    assert kinds == ["prose", "code", "table", "figure", "prose"], kinds
    assert bs[0].text == "Plain narratable prose."
    assert "print('hi')" in bs[1].html
    assert bs[1].text == ""  # non-prose blocks aren't narrated


def test_code_as_styled_paragraph_rules_only_is_prose():
    # A monospace-styled <p> is ambiguous; with classifier="none" it stays prose.
    soup = _soup("<p class='code'>x = 1</p>")
    _, bs = classify_page(soup, classifier="none")
    assert [b.kind for b in bs] == ["prose"]
    assert bs[0].text == "x = 1"


def test_nested_blocks_not_double_counted():
    # A <p> inside a <blockquote> must not also appear as its own block.
    soup = _soup("<blockquote><p>quoted</p></blockquote>")
    _, bs = classify_page(soup, classifier="none")
    assert len(bs) == 1
    assert bs[0].kind == "prose" and "quoted" in bs[0].text


def test_page_drop_reasons():
    assert page_drop_reason(_soup("<nav epub:type='toc'><ol></ol></nav>"), "nav") is not None
    assert page_drop_reason(_soup("<p>x</p>"), "copyright") is not None
    assert page_drop_reason(_soup("<section epub:type='index'><p>x</p></section>"), "ch1") is not None
    # A normal chapter is kept.
    assert page_drop_reason(_soup("<h1>Ch 1</h1><p>body</p>"), "chapter1") is None


def test_drop_gutenberg_boilerplate_and_contents():
    # Project Gutenberg header/footer wrapper.
    pg = _soup(
        "<header class='pg-boilerplate pgheader' id='pg-header'>"
        "<h2>The Project Gutenberg eBook</h2><div>License text…</div></header>"
    )
    assert page_drop_reason(pg, "97-h-0") is not None
    # A "Contents" page whose body is a link table (Gutenberg-style TOC).
    toc = _soup(
        "<div class='chapter'><h2>Contents</h2><table>"
        + "".join(f"<tr><td><a href='#c{i}'>Chapter {i} title here</a></td></tr>" for i in range(8))
        + "</table></div>"
    )
    assert page_drop_reason(toc, "97-h-2") is not None


def test_text_extraction_is_s6_compatible():
    # source_span is the sync anchor: it must MIRROR Stage 6's shadow (a space
    # at every node boundary), so small-caps collapse to "S PACE" here — the
    # normalize pass respells that, the extractor must not (else the anchor
    # desyncs). <br> line breaks also become spaces.
    soup = _soup("<p>To S<small>PACE</small><br/>The end.</p>")
    _, bs = classify_page(soup, classifier="none")
    assert bs[0].text == "To S PACE The end.", bs[0].text


# ---- render builder (pure; no Playwright needed) ----------------------------


def test_build_block_html_code_contains_source():
    block = blocks.ClassifiedBlock(kind="code", html="<pre>def f():\n    return 7</pre>")
    doc = render.build_block_html(block)
    assert "<html" in doc and "return" in doc and "f()" in doc


def test_render_unavailable_degrades():
    # When Playwright is absent, render_block must return False, not raise —
    # the caller then records an HTML-only visual element. Force the
    # unavailable path regardless of whether Playwright is installed here.
    orig = render.available
    render.available = lambda: False
    try:
        block = blocks.ClassifiedBlock(kind="table", html="<table><tr><td>x</td></tr></table>")
        assert render.render_block(block, Path("/tmp/lnvox_should_not_exist.png")) is False
    finally:
        render.available = orig


# ---- beat split + directing -------------------------------------------------


def test_split_chapter_verbatim_and_paragraph_index():
    ch = Chapter(chapter_id="01", title="T", text="First para.\n\nSecond para.")
    beats = lecture.split_chapter(ch)
    assert [b.text for b in beats] == ["First para.", "Second para."]
    # Before normalize, text == source_span (verbatim) and all are Narrator.
    assert all(b.text == b.source_span for b in beats)
    assert all(b.type == "narration" and b.speaker == "Narrator" for b in beats)
    assert [b.source_paragraph for b in beats] == [0, 1]


def test_long_paragraph_splits_at_sentences():
    long_para = " ".join(f"Sentence number {i}." for i in range(80))
    ch = Chapter(chapter_id="01", title="T", text=long_para)
    from lnvox.stages.s3_director import MAX_MERGED_BEAT_CHARS

    beats = lecture.split_chapter(ch)
    assert len(beats) > 1
    assert all(len(b.text) <= MAX_MERGED_BEAT_CHARS for b in beats)
    # All sub-beats came from paragraph 0.
    assert all(b.source_paragraph == 0 for b in beats)


def test_direct_chapter_builds_prompt_without_llm():
    ch = Chapter(chapter_id="03", title="T", text="Hello world.")
    cd = lecture.direct_chapter(ch, "adult, British, male, clear voice", client=None, normalize=False)
    assert cd.chapter_id == "03"
    beat = cd.scenes[0].beats[0]
    assert beat.direction == "adult, British, male, clear voice"
    # Dramabox format: direction lowercased, commas → ' - ', wrapped in parens.
    assert beat.prompt == '(adult - british - male - clear voice) "Hello world."'
    assert cd.scenes[0].scene_id == "03_s1"


# ---- Stage 6 visual-element placement ---------------------------------------


def test_place_visual_element_between_beats():
    # beat_id, source_paragraph, start, end
    seq = [
        ("03_s1_b0000", 0, 0.0, 5.0),
        ("03_s1_b0001", 1, 6.0, 10.0),
        ("03_s1_b0002", 2, 11.0, 15.0),
    ]
    # A code block after paragraph 1 → between beat b0001 and b0002.
    after, before, trigger = place_visual_element(1, seq)
    assert after == "03_s1_b0001"
    assert before == "03_s1_b0002"
    assert trigger == 11.0


def test_place_visual_element_front_and_end():
    seq = [("b0", 0, 0.0, 5.0), ("b1", 1, 6.0, 10.0)]
    # Before any paragraph (after_paragraph = -1) → trigger at the first beat.
    after, before, trigger = place_visual_element(-1, seq)
    assert after is None and before == "b0" and trigger == 0.0
    # After the last paragraph → no before-beat; trigger at the last beat's end.
    after, before, trigger = place_visual_element(99, seq)
    assert after == "b1" and before is None and trigger == 10.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

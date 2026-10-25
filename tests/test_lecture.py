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


def _page(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f"{body}</body></html>"
    ).encode()


def test_drop_stem_rule_uses_the_file_stem_not_the_heading():
    from lnvox.ingest.epub import _xhtml_to_blocks

    def parse(body, stem):
        return _xhtml_to_blocks(_page(body), stem=stem, classifier="none")

    # Headings that merely START with a boilerplate word are chapters.
    assert parse("<h1>Index Funds and You</h1><p>Buy the market.</p>", "chapter3") is not None
    assert parse("<h1>Glossary of Terms in Use</h1><p>A term.</p>", "ch4") is not None
    # The file stem is what the stem rule reads: index.xhtml goes, whatever
    # its heading says.
    assert parse("<h1>Alphabetical Listing</h1><p>apple, 3</p>", "index") is None
    # Whole-title boilerplate headings still drop the page.
    assert parse("<h1>Glossary</h1><p>A term.</p>", "ch9") is None
    assert parse("<h2>Index</h2><p>apple, 3</p>", "ch10") is None


def test_nested_drop_landmark_drops_its_blocks_not_the_page():
    from lnvox.ingest.epub import _xhtml_to_blocks

    chapter = (
        "<section epub:type='chapter'><h1>Chapter 3</h1><p>Real prose.</p>"
        "<section epub:type='bibliography'><h2>References</h2>"
        "<ol><li>Some Author, Some Book.</li></ol></section></section>"
    )
    assert page_drop_reason(_soup(chapter), "ch3") is None
    title, paragraphs, blocks_ = _xhtml_to_blocks(_page(chapter), stem="ch3", classifier="none")
    assert title == "Chapter 3"
    assert paragraphs == ["Real prose."]  # the references are excised
    assert blocks_ == []

    # A top-level bibliography BESIDE real prose drops only itself too …
    sibling = (
        "<h1>Chapter 4</h1><p>Real prose.</p>"
        "<section epub:type='bibliography'><p>Some Author, Some Book.</p></section>"
    )
    assert page_drop_reason(_soup(sibling), "ch4") is None
    # … while one holding the whole page drops the page.
    alone = "<section epub:type='bibliography'><h1>Works Cited</h1><p>A Book.</p></section>"
    assert page_drop_reason(_soup(alone), "ch5") is not None

    # A chapter opening with a mini-TOC nav: the nav's "Contents" heading is
    # not the page's heading.
    mini_toc = (
        "<nav epub:type='toc'><h2>Contents</h2><ol><li><a href='#a'>A</a></li></ol></nav>"
        "<h1>Chapter 6</h1><p>Real prose.</p>"
    )
    assert page_drop_reason(_soup(mini_toc), "ch6") is None
    title, bs = classify_page(_soup(mini_toc), classifier="none")
    assert title == "Chapter 6"
    assert [b.text for b in bs] == ["Real prose."]


def test_footnote_types_on_any_element():
    soup = _soup(
        "<h1>T</h1><p>Prose.</p>"
        "<div epub:type='footnote'><p>First note para.</p><p>Second.</p></div>"
        "<section epub:type='endnotes'><h3>Notes</h3>"
        "<p id='n1'>1. A container note.</p></section>"
        "<ol><li epub:type='endnote'><p>An endnote.</p></li></ol>"
        "<aside epub:type='footnote'><p>Aside note.</p></aside>"
        "<div epub:type='note'><p>A callout box.</p></div>"
    )
    _, bs = classify_page(soup, classifier="none")
    kinds = [(b.kind, b.tag) for b in bs]
    assert kinds == [
        ("prose", "p"),
        ("footnote", "div"),       # one block, not one per <p>
        ("footnote", "h3"),        # everything in a notes container
        ("footnote", "p"),
        ("footnote", "ol"),        # a list made only of endnotes
        ("footnote", "aside"),     # historical rule, unchanged
        ("prose", "p"),            # epub:type="note" stays aside-only
    ], kinds
    assert all(b.text == "" for b in bs if b.kind == "footnote")


class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc

    def structured(self, **_kw):
        raise self.exc


def test_fallback_classifier_reraises_transport_errors():
    soup = _soup("<p class='code'>x = 1</p>")
    render_tpl = lambda _name, **_kw: "prompt"

    # A bad answer (validation failure, …) degrades to prose.
    _, bs = classify_page(
        soup, classifier="fallback", llm=_RaisingLLM(ValueError("bad json")),
        render_template=render_tpl,
    )
    assert [b.kind for b in bs] == ["prose"]

    # An unreachable server must fail the step, not narrate every block.
    import httpx
    import openai

    for exc in (
        ConnectionError("refused"),
        openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:1")),
    ):
        try:
            classify_page(
                soup, classifier="fallback", llm=_RaisingLLM(exc),
                render_template=render_tpl,
            )
        except type(exc):
            continue
        raise AssertionError(f"{type(exc).__name__} was swallowed")


def test_extract_epub_lecture_drops_by_file_stem():
    import tempfile
    import zipfile

    from lnvox.ingest.epub import extract_epub

    pages = {
        "ch1": "<h1>Index Funds and You</h1><p>Buy the market.</p>",
        "index": "<h1>Alphabetical Listing</h1><p>apple, 3</p>",
    }
    with tempfile.TemporaryDirectory() as tmp:
        epub = Path(tmp) / "b.epub"
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
                "<dc:title>Invented</dc:title></metadata><manifest>"
                + "".join(
                    f'<item id="{k}" href="{k}.xhtml" media-type="application/xhtml+xml"/>'
                    for k in pages
                )
                + "</manifest><spine>"
                + "".join(f'<itemref idref="{k}"/>' for k in pages)
                + "</spine></package>",
            )
            for k, body in pages.items():
                zf.writestr(f"{k}.xhtml", _page(body).decode())
        meta = extract_epub(
            epub, Path(tmp) / "out", mode="lecture", classifier="none",
            render=False, progress=lambda _m: None,
        )
    assert [c["title"] for c in meta.chapters] == ["Index Funds and You"]


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


# ---- per-chapter cache (normalize flag + narrator descriptor) ----------------


class _Normalizer:
    """Stub LLM for the normalize pass: upper-cases each passage."""

    def __init__(self, fail: Exception | None = None):
        from types import SimpleNamespace

        self.settings = SimpleNamespace(llm=SimpleNamespace(max_model_len=65536))
        self.calls = 0
        self.fail = fail

    def render(self, template, **kw):
        return kw["passages"]

    def budget_for(self, *, desired, **_):
        return desired

    def structured(self, *, user, schema, **_):
        import re

        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return schema(beats=[
            {"index": int(i), "text": t.upper()}
            for i, t in re.findall(r"^\[(\d+)\] (.*)$", user, re.M)
        ])


def _casting(descriptor: str):
    from lnvox.voices.schema import BookCasting, CharacterCasting, VoiceTarget

    return BookCasting(book_id="b", castings=[CharacterCasting(
        character_name="Narrator", target=VoiceTarget(gender="female", age_band="adult"),
        assigned_clip_id="f1", voice_descriptor=descriptor,
    )])


_CH = [Chapter(chapter_id="01", title="T", text="It was 1973.\n\nThe end.")]


def _texts(results):
    return [b.text for r in results for s in r.scenes for b in s.beats]


def test_cache_redoes_a_chapter_when_normalize_is_toggled():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        llm = _Normalizer()
        r = lecture.run(_CH, out, casting=_casting("adult, female"), client=llm)
        assert _texts(r) == ["IT WAS 1973.", "THE END."] and r[0].normalized is True
        # --no-normalize now: the normalized cache must not be reused.
        r = lecture.run(_CH, out, casting=_casting("adult, female"), client=None,
                        normalize=False)
        assert _texts(r) == ["It was 1973.", "The end."] and r[0].normalized is False
        # ...and back: normalized again (one more LLM batch), then reused.
        r = lecture.run(_CH, out, casting=_casting("adult, female"), client=llm)
        assert _texts(r) == ["IT WAS 1973.", "THE END."] and llm.calls == 2
        lecture.run(_CH, out, casting=_casting("adult, female"), client=llm)
        assert llm.calls == 2


def test_cache_revoices_a_new_narrator_without_renormalizing():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        llm = _Normalizer()
        lecture.run(_CH, out, casting=_casting("adult, female"), client=llm)
        r = lecture.run(_CH, out, casting=_casting("elder, male"), client=llm)
        beat = r[0].scenes[0].beats[0]
        assert llm.calls == 1  # text kept, only the voice changed
        assert beat.direction == "elder, male"
        assert beat.prompt == '(elder - male) "IT WAS 1973."'
        assert r[0].voice_descriptors == {"Narrator": "elder, male"}


def test_cache_rebuilds_a_chapter_whose_text_changed():
    """A re-extraction (a fuller PDF, a proofread .txt) must not reuse the old
    chapter's beats — they'd re-render the old text under the new chapter."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        llm = _Normalizer()
        lecture.run(_CH, out, casting=_casting("adult, female"), client=llm)
        longer = [Chapter(chapter_id="01", title="T", text="It was 1973.\n\nThen 1974.\n\nThe end.")]
        r = lecture.run(longer, out, casting=_casting("adult, female"), client=llm)
        assert _texts(r) == ["IT WAS 1973.", "THEN 1974.", "THE END."] and llm.calls == 2
        lecture.run(longer, out, casting=_casting("adult, female"), client=llm)
        assert llm.calls == 2  # unchanged text: the cache holds again


def test_cache_treats_an_unrecorded_chapter_as_current():
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "03_directed").mkdir()
        legacy = lecture.direct_chapter(_CH[0], "adult, female", client=_Normalizer())
        data = json.loads(legacy.model_dump_json())
        del data["voice_descriptors"], data["normalized"]
        (out / "03_directed" / "01.json").write_text(json.dumps(data), encoding="utf-8")
        r = lecture.run(_CH, out, casting=_casting("elder, male"), client=None,
                        normalize=False)
        # Reused as-is (still normalized, old voice) and stamped.
        assert _texts(r) == ["IT WAS 1973.", "THE END."]
        assert r[0].normalized is False and r[0].voice_descriptors == {"Narrator": "elder, male"}
        assert r[0].scenes[0].beats[0].direction == "adult, female"


def test_normalize_transport_error_fails_but_bad_answer_degrades():
    import httpx
    import openai
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        llm = _Normalizer(fail=openai.APIConnectionError(
            request=httpx.Request("POST", "http://localhost:8000/v1")))
        try:
            lecture.run(_CH, Path(d), client=llm)
        except openai.APIConnectionError:
            pass
        else:
            raise AssertionError("transport error was swallowed")
        assert not (Path(d) / "03_directed" / "01.json").exists()
    with tempfile.TemporaryDirectory() as d:
        r = lecture.run(_CH, Path(d), client=_Normalizer(fail=ValueError("bad json")))
        assert _texts(r) == ["It was 1973.", "The end."]


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


class _PromptCapture(_Normalizer):
    """The stub normalizer, rendering the real template so the prompt can be read."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def render(self, template, **kw):
        from lnvox.llm.client import _jinja

        self.prompts.append(_jinja.get_template(template).render(**kw))
        return kw["passages"]


def test_normalize_prompt_follows_the_book_language():
    ch = Chapter(chapter_id="01", title="T", text="En 1973, il partit.")
    english, french = _PromptCapture(), _PromptCapture()
    lecture.direct_chapter(ch, "adult, male", client=english)
    lecture.direct_chapter(ch, "adult, male", client=french, language="fr")
    assert "LANGUAGE:" not in english.prompts[0]  # English prompts are unchanged
    assert "Respell them IN FRENCH" in french.prompts[0]
    assert "mille neuf cent soixante-treize" in french.prompts[0]
    other = _PromptCapture()
    lecture.direct_chapter(ch, "adult, male", client=other, language="pt-BR")
    assert "in Portuguese" in other.prompts[0] and "For French" not in other.prompts[0]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

"""Block classification for lecture-mode ingest (DESIGN.md §13.2a).

Deterministic-first: the DOM tag + EPUB ``epub:type`` landmarks + stem regex
decide everything they can. The LLM is a *fallback only* — it classifies the
small tail of blocks the rules can't (e.g. a publisher who renders code as
monospace-styled ``<p>``). A book with clean semantic markup makes zero LLM
calls.

Each block ends up one of:
  prose      → narrated (headings are folded in here)
  code/table/figure/footnote/equation → reader-side visual element, NOT narrated
  drop       → excised entirely (only at the page/landmark level)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup
from bs4.element import PreformattedString, Tag


# Top-most flow elements we treat as one block each. Structural wrapper
# ``<div>``s are deliberately excluded — emitting them as blocks would collapse
# a whole chapter into one paragraph. Code that a publisher renders as a styled
# ``<div>``/``<p>`` is caught by the code-hint heuristic + LLM fallback instead.
_BLOCK_TAGS = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "pre", "table", "figure", "blockquote", "ul", "ol", "aside", "math",
)
_BLOCK_TAG_SET = set(_BLOCK_TAGS)

# Class / style tokens that hint a <p>/<div> is really a code listing.
_CODE_HINT_RE = re.compile(
    r"\b(code|sourcecode|source-code|codeblock|code-block|listing|"
    r"mono|monospace|verbatim|prettyprint|highlight|programlisting)\b",
    re.IGNORECASE,
)
_MONO_STYLE_RE = re.compile(r"font-family\s*:[^;]*(monospace|courier|consolas)", re.IGNORECASE)

# epub:type values (and stem patterns) whose whole spine page is dropped.
# Matches the §13.2b default drop set: TOC, copyright/title page, index,
# bibliography. Dedication / acknowledgments / preface are deliberately absent
# (kept and narrated).
DEFAULT_DROP_TYPES = frozenset({
    "toc", "landmarks", "copyright-page", "titlepage", "title-page",
    "index", "bibliography", "loi", "lot",
})

_DROP_STEM_RE = re.compile(
    r"^(toc|nav|index|bibliography|biblio|glossary|copyright|titlepage|"
    r"title-page|halftitle|half-title|colophon-toc)\b",
    re.IGNORECASE,
)

# Heading text (lowercased) that flags a whole page as a contents/index page.
# Whole-title matches only: "Index Funds and You" or "Glossary of Terms Used
# in Chapter 3" are chapters, not back matter.
_DROP_HEADING_TEXT = {
    "contents", "table of contents", "toc", "index", "bibliography",
    "glossary", "list of illustrations", "list of figures",
}

# Project Gutenberg wraps its header/footer license boilerplate in these.
_PG_BOILERPLATE_CLASSES = {"pg-boilerplate", "pgheader", "pgfooter"}
_PG_BOILERPLATE_IDS = {"pg-header", "pg-footer"}

# epub:type appears under the OPS namespace; bs4's html.parser surfaces it as a
# flat "epub:type" attribute. Some files use a plain "type"/"role" too.
_EPUB_TYPE_ATTRS = ("epub:type", "epubtype", "data-epub-type")
# On an <aside>, any of these makes it a footnote (the historical rule; "note"
# stays aside-only because a <div epub:type="note"> is as often a narrated
# callout box as a footnote).
_FOOTNOTE_TYPES = {"footnote", "endnote", "rearnote", "note", "footnotes"}
# On ANY element (§13.2a `<*[epub:type=footnote|endnote]>`): the element is one
# footnote block — a `<div>`/`<li>`/`<p>` note, not just an <aside>.
_NOTE_TYPES = {"footnote", "endnote", "rearnote"}
# Containers of notes (Standard Ebooks' `<section epub:type="endnotes">`):
# every block inside one is a footnote.
_NOTE_CONTAINER_TYPES = {"footnotes", "endnotes", "rearnotes"}
# Elements whose epub:type landmark is page-level when they are not nested in
# another one (see page_drop_reason).
_LANDMARK_TAGS = ("section", "nav", "article")


@dataclass
class ClassifiedBlock:
    kind: str           # see module docstring
    text: str = ""      # narratable text (prose/heading); "" for non-prose
    html: str = ""      # verbatim outer HTML (non-prose blocks, for the reader)
    tag: str = ""       # source tag name
    language: str = ""  # best-effort code language hint (code blocks only)


def _epub_type(el: Tag) -> str:
    for attr in _EPUB_TYPE_ATTRS:
        val = el.get(attr)
        if val:
            return " ".join(val) if isinstance(val, list) else str(val)
    return ""


def _class_tokens(el: Tag) -> str:
    cls = el.get("class") or []
    if isinstance(cls, str):
        cls = [cls]
    return " ".join(cls)


def _has_type(el, types) -> bool:
    return isinstance(el, Tag) and any(
        t in types for t in _epub_type(el).lower().split()
    )


def _drop_type(el: Tag, drop_types: frozenset[str]) -> Optional[str]:
    """The first drop-set ``epub:type`` token on `el` itself, else None."""
    for token in _epub_type(el).lower().split():
        if token in drop_types:
            return token
    return None


def _in_dropped_landmark(el: Tag, drop_types: frozenset[str]) -> bool:
    """True if `el` or any ancestor carries a drop-set ``epub:type``.

    Blocks under such a landmark are boilerplate even when the page around
    them is kept (a chapter-end bibliography section, a mini-TOC nav).
    """
    node = el
    while isinstance(node, Tag):
        if _drop_type(node, drop_types):
            return True
        node = node.parent
    return False


def _has_text_outside(body: Tag, containers: list[Tag]) -> bool:
    """True if `body` has any lettered text not inside one of `containers`.

    Digit/punctuation-only strings (page numbers, ornaments) don't count, so a
    stray page-break marker next to an index section doesn't save the page.
    """
    inside = {id(c) for c in containers}
    for s in body.find_all(string=True):
        if isinstance(s, PreformattedString):  # comment / doctype / CDATA
            continue
        if not any(ch.isalpha() for ch in s):
            continue
        if s.parent is not None and s.parent.name in ("script", "style", "title"):
            continue
        if not any(id(p) in inside for p in s.parents):
            return True
    return False


_WS_RE = re.compile(r"\s+")


def _text_of(el: Tag) -> str:
    """Readable text of a block, with whitespace collapsed to single spaces.

    A space separator is inserted at every node boundary to MIRROR Stage 6's
    shadow reconstruction ([s6_sync._build_shadow]), which inserts a space
    between adjacent text nodes. The lecture beat's ``source_span`` is this
    string, and it is the §2.8 sync key — so it MUST match what s6 rebuilds
    from the EPUB or matching falls through to the fuzzy ladder. Cosmetic
    artifacts this leaves (e.g. small-caps ``S<small>PACE</small>`` → "S PACE")
    are the speech-normalize pass's job to respell (§13.6), NOT the extractor's
    — fixing them here would desync the anchor.
    """
    return _WS_RE.sub(" ", el.get_text(separator=" ")).strip()


def page_drop_reason(
    soup: BeautifulSoup,
    stem: str,
    *,
    drop_types: frozenset[str] = DEFAULT_DROP_TYPES,
) -> Optional[str]:
    """Return a short reason if this whole spine page should be dropped, else None.

    Deterministic only: a boilerplate filename ``stem`` (the XHTML file's own
    stem — never heading text), a page-level ``epub:type`` landmark, a Project
    Gutenberg header/footer wrapper, a nav document, a leading heading whose
    WHOLE text names a contents/index page, or a link-dense page with no prose.
    """
    stem_l = stem.lower()
    if _DROP_STEM_RE.match(stem_l):
        return f"stem '{stem}' matches boilerplate"

    body = soup.find("body") or soup
    if not isinstance(body, Tag):
        return None

    # 1. EPUB3 epub:type landmark — page-level markup only: on <body>, or on
    # top-level <section>/<nav>/<article>s that hold ALL the page's text. A
    # landmark nested inside a chapter (a chapter-end bibliography section, a
    # mini-TOC nav), or sitting beside real prose, drops only its own blocks
    # (classify_page) — it used to drop the whole chapter page.
    token = _drop_type(body, drop_types)
    if token:
        return f"epub:type '{token}'"
    top_level = [
        el
        for el in body.find_all(_LANDMARK_TAGS)
        if not any(
            isinstance(p, Tag) and p.name in _LANDMARK_TAGS
            for p in el.parents
            if p is not body
        )
    ]
    dropped = [el for el in top_level if _drop_type(el, drop_types)]
    if dropped and not _has_text_outside(body, dropped):
        return f"epub:type '{_drop_type(dropped[0], drop_types)}'"

    # 2. Project Gutenberg (and similar) boilerplate header/footer wrappers.
    # Page-wide on purpose: PG puts its header in the same file as the title
    # page and the linked contents table, all of which should go.
    for el in body.find_all(True):
        if (set(_class_tokens(el).lower().split()) & _PG_BOILERPLATE_CLASSES) or (
            (el.get("id") or "").lower() in _PG_BOILERPLATE_IDS
        ):
            return "publisher boilerplate (pg-header/footer)"

    # 3. A <nav> document (EPUB3 TOC) with no real prose.
    if body.find("nav") is not None and not body.find("p"):
        return "nav document (no prose)"

    # 4. Content-based contents/index page: a leading heading that names it
    # (whole title only — see _DROP_HEADING_TEXT). A heading inside a nested
    # drop landmark (a mini-TOC's "Contents") is that landmark's, not the page's.
    heading = next(
        (
            h
            for h in body.find_all(["h1", "h2", "h3"])
            if not _in_dropped_landmark(h, drop_types)
        ),
        None,
    )
    if heading is not None:
        htext = _WS_RE.sub(" ", heading.get_text()).strip().lower().rstrip(".")
        if htext in _DROP_HEADING_TEXT:
            return f"heading '{htext}'"

    # 5. Link-dense page (TOC / index / list-of-figures) with no real prose:
    # most of the text sits inside <a> links and there are no paragraphs.
    total = len(_WS_RE.sub(" ", body.get_text()).strip())
    if total:
        link_chars = sum(len(a.get_text(strip=True)) for a in body.find_all("a"))
        n_links = len(body.find_all("a"))
        if n_links >= 5 and link_chars / total > 0.6 and not body.find("p"):
            return "link-dense (TOC/index)"
    return None


def _classify_one(
    el: Tag, *, title_el: Optional[Tag]
) -> Optional[tuple[ClassifiedBlock, bool]]:
    """Classify a single top-level block.

    Returns ``(block, needs_llm)`` or ``None`` to skip the element (empty text /
    the title heading, which the caller emits separately).
    """
    if el is title_el:
        return None  # caller emits the chapter title separately
    name = (el.name or "").lower()
    has_text = bool(el.get_text(strip=True))
    has_img = el.find(["img", "image"]) is not None

    if name == "pre":
        if not has_text:
            return None  # empty <pre> — formatting artifact, not a listing
        code_el = el.find("code") or el
        return (
            ClassifiedBlock(
                kind="code",
                html=str(el),
                tag=name,
                language=_code_language(code_el),
            ),
            False,
        )
    if name == "table":
        if not (has_text or has_img):
            return None  # empty table — skip
        return ClassifiedBlock(kind="table", html=str(el), tag=name), False
    if name == "figure":
        if not (has_text or has_img):
            return None
        return ClassifiedBlock(kind="figure", html=str(el), tag=name), False
    if name == "math":
        if not has_text:
            return None
        return ClassifiedBlock(kind="equation", html=str(el), tag=name), False
    if name == "aside":
        if any(t in _FOOTNOTE_TYPES for t in _epub_type(el).lower().split()):
            return ClassifiedBlock(kind="footnote", html=str(el), tag=name), False
        # A non-note aside is sidebar prose — narrate it (unless it sits in a
        # notes container, below).
    if _is_note_block(el):
        if not (has_text or has_img):
            return None
        return ClassifiedBlock(kind="footnote", html=str(el), tag=name), False

    text = _text_of(el)
    if not text:
        return None

    if name in ("p", "blockquote") or name in ("ul", "ol") or name == "aside" or name.startswith("h"):
        # Code-as-styled-paragraph: hint-detect, defer the judgment to the LLM.
        if name in ("p", "blockquote"):
            hint = _CODE_HINT_RE.search(_class_tokens(el)) or _MONO_STYLE_RE.search(
                el.get("style") or ""
            )
            if hint:
                return (
                    ClassifiedBlock(kind="code", text=text, html=str(el), tag=name),
                    True,  # ambiguous → LLM fallback decides prose vs code
                )
        return ClassifiedBlock(kind="prose", text=text, tag=name), False

    return ClassifiedBlock(kind="prose", text=text, tag=name), False


_LANG_CLASS_RE = re.compile(r"\b(?:language|lang|brush|sourceCode)[-:]([a-z0-9+#]+)\b", re.IGNORECASE)


def _code_language(el: Tag) -> str:
    m = _LANG_CLASS_RE.search(_class_tokens(el))
    if m:
        return m.group(1).lower()
    # Bare language class, e.g. class="python".
    for tok in _class_tokens(el).split():
        if tok.lower() in _KNOWN_LANGS:
            return tok.lower()
    return ""


_KNOWN_LANGS = {
    "python", "javascript", "js", "typescript", "ts", "java", "c", "cpp",
    "cs", "go", "rust", "ruby", "php", "swift", "kotlin", "scala", "bash",
    "sh", "shell", "sql", "html", "css", "json", "yaml", "xml", "haskell",
}


def _is_note_block(el: Tag) -> bool:
    """True if a block is a footnote/endnote by its ``epub:type`` markup.

    Three shapes (§13.2a — any element, not just <aside>): the block carries
    a note type itself (`<div epub:type="footnote">`); it sits inside a notes
    container (`<section epub:type="endnotes">`); or every lettered string in
    it belongs to a note inside it (an `<ol>` of `<li epub:type="endnote">`).
    """
    if _has_type(el, _NOTE_TYPES):
        return True
    if any(_has_type(p, _NOTE_TYPES | _NOTE_CONTAINER_TYPES) for p in el.parents):
        return True
    if el.find(lambda t: _has_type(t, _NOTE_TYPES)) is None:
        return False
    found = False
    for s in el.find_all(string=True):
        if isinstance(s, PreformattedString) or not any(ch.isalpha() for ch in s):
            continue
        owner = s.parent
        while owner is not None and owner is not el and not _has_type(owner, _NOTE_TYPES):
            owner = owner.parent
        if owner is None or owner is el:
            return False  # prose outside any note
        found = True
    return found


def _is_block_el(el: Tag) -> bool:
    # A note-typed element is one block whatever its tag (a <div>/<li> note
    # holding several <p>s is ONE footnote, not several).
    return (el.name or "").lower() in _BLOCK_TAG_SET or _has_type(el, _NOTE_TYPES)


def _top_level_blocks(body: Tag) -> list[Tag]:
    """Top-most block elements in document order (skip blocks nested in blocks)."""
    out: list[Tag] = []
    for el in body.find_all(_is_block_el):
        if any(isinstance(p, Tag) and _is_block_el(p) for p in el.parents):
            continue
        out.append(el)
    return out


def classify_page(
    soup: BeautifulSoup,
    *,
    classifier: str = "fallback",
    llm=None,
    render_template=None,
    drop_types: frozenset[str] = DEFAULT_DROP_TYPES,
) -> tuple[str, list[ClassifiedBlock]]:
    """Classify one XHTML page into a title + ordered blocks.

    ``classifier``: ``"fallback"`` runs the LLM only on ambiguous blocks (needs
    ``llm`` + ``render_template``); ``"none"`` resolves ambiguous blocks to
    prose deterministically.

    Blocks under a ``drop_types`` landmark that did not drop the whole page
    (see `page_drop_reason`) are excised here: not narrated, not surfaced.
    """
    body = soup.find("body") or soup
    if not isinstance(body, Tag):
        return "", []

    h = next(
        (
            x
            for x in body.find_all(["h1", "h2"])
            if not _in_dropped_landmark(x, drop_types)
        ),
        None,
    )
    title = _text_of(h) if h else ""

    blocks: list[ClassifiedBlock] = []
    for el in _top_level_blocks(body):
        if _in_dropped_landmark(el, drop_types):
            continue
        res = _classify_one(el, title_el=h)
        if res is None:
            continue
        block, needs_llm = res
        if needs_llm:
            block = _resolve_ambiguous(
                block, el, classifier=classifier, llm=llm, render_template=render_template
            )
        blocks.append(block)
    return title, blocks


def _resolve_ambiguous(
    block: ClassifiedBlock,
    el: Tag,
    *,
    classifier: str,
    llm,
    render_template,
) -> ClassifiedBlock:
    """Decide an ambiguous block's kind. LLM if available, else fall to prose.

    Transport errors (server down / 5xx) propagate; a bad answer falls to prose.
    """
    from lnvox.llm.client import is_transport_error
    from lnvox.llm.schemas import BlockClass

    if classifier == "none" or llm is None or render_template is None:
        # Rules-only: treat as prose (narrate). Honors "untagged ⇒ prose".
        return ClassifiedBlock(kind="prose", text=block.text, tag=block.tag)
    try:
        snippet = str(el)[:2000]
        user = render_template("block_classify.jinja", block_html=snippet)
        result: BlockClass = llm.structured(
            system=_CLASSIFY_SYSTEM, user=user, schema=BlockClass, max_tokens=256
        )
    except Exception as exc:
        # A bad or unparseable answer degrades to prose (narrate it). An
        # unreachable server must NOT: it would silently turn every ambiguous
        # block of the book into prose, and the step would still succeed.
        if is_transport_error(exc):
            raise
        return ClassifiedBlock(kind="prose", text=block.text, tag=block.tag)
    kind = result.kind
    if kind == "prose":
        return ClassifiedBlock(kind="prose", text=block.text, tag=block.tag)
    if kind == "drop":
        # A "drop" verdict on a single block → just don't narrate it and don't
        # surface it; represent as an empty prose block the caller filters out.
        return ClassifiedBlock(kind="prose", text="", tag=block.tag)
    # code / table / figure / footnote / equation → visual element.
    return ClassifiedBlock(
        kind=kind, text="", html=str(el), tag=block.tag, language=block.language
    )


_CLASSIFY_SYSTEM = (
    "You classify one block of an EPUB page for an audiobook pipeline. "
    "Output a single raw JSON object matching the requested schema. "
    "Do NOT wrap the output in markdown code fences."
)

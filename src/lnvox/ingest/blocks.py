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
from bs4.element import Tag


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
_DROP_HEADING_TEXT = {
    "contents", "table of contents", "toc", "index", "bibliography",
    "list of illustrations", "list of figures",
}

# Project Gutenberg wraps its header/footer license boilerplate in these.
_PG_BOILERPLATE_CLASSES = {"pg-boilerplate", "pgheader", "pgfooter"}
_PG_BOILERPLATE_IDS = {"pg-header", "pg-footer"}

# epub:type appears under the OPS namespace; bs4's html.parser surfaces it as a
# flat "epub:type" attribute. Some files use a plain "type"/"role" too.
_EPUB_TYPE_ATTRS = ("epub:type", "epubtype", "data-epub-type")
_FOOTNOTE_TYPES = {"footnote", "endnote", "rearnote", "note", "footnotes"}


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

    Deterministic only: an ``epub:type`` landmark on <body>/<section>/<nav>, an
    EPUB3 ``<nav epub:type="toc">``, or a boilerplate filename stem.
    """
    stem_l = stem.lower()
    if _DROP_STEM_RE.match(stem_l):
        return f"stem '{stem}' matches boilerplate"

    body = soup.find("body") or soup
    if not isinstance(body, Tag):
        return None

    # 1. EPUB3 epub:type landmark on body / section / nav.
    candidates = [body] + body.find_all(["section", "nav"], recursive=True)
    for el in candidates:
        if not isinstance(el, Tag):
            continue
        for token in _epub_type(el).lower().split():
            if token in drop_types:
                return f"epub:type '{token}'"

    # 2. Project Gutenberg (and similar) boilerplate header/footer wrappers.
    for el in body.find_all(True):
        if (set(_class_tokens(el).lower().split()) & _PG_BOILERPLATE_CLASSES) or (
            (el.get("id") or "").lower() in _PG_BOILERPLATE_IDS
        ):
            return "publisher boilerplate (pg-header/footer)"

    # 3. A <nav> document (EPUB3 TOC) with no real prose.
    if body.find("nav") is not None and not body.find("p"):
        return "nav document (no prose)"

    # 4. Content-based contents/index page: a leading heading that names it.
    heading = body.find(["h1", "h2", "h3"])
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
        # A non-note aside is sidebar prose — narrate it.

    text = _text_of(el)
    if not text:
        return None
    if el is title_el:
        return None  # caller emits the chapter title separately

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


def _top_level_blocks(body: Tag) -> list[Tag]:
    """Top-most block elements in document order (skip blocks nested in blocks)."""
    out: list[Tag] = []
    for el in body.find_all(_BLOCK_TAGS):
        if any(
            isinstance(p, Tag) and (p.name or "").lower() in _BLOCK_TAG_SET
            for p in el.parents
        ):
            continue
        out.append(el)
    return out


def classify_page(
    soup: BeautifulSoup,
    *,
    classifier: str = "fallback",
    llm=None,
    render_template=None,
) -> tuple[str, list[ClassifiedBlock]]:
    """Classify one XHTML page into a title + ordered blocks.

    ``classifier``: ``"fallback"`` runs the LLM only on ambiguous blocks (needs
    ``llm`` + ``render_template``); ``"none"`` resolves ambiguous blocks to
    prose deterministically.
    """
    body = soup.find("body") or soup
    if not isinstance(body, Tag):
        return "", []

    h = body.find(["h1", "h2"])
    title = _text_of(h) if h else ""

    blocks: list[ClassifiedBlock] = []
    for el in _top_level_blocks(body):
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
    """Decide an ambiguous block's kind. LLM if available, else fall to prose."""
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
    except Exception:
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

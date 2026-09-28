---
title: lnvox.ingest.blocks
description: Lecture-mode block classifier. Deterministic DOM and epub:type rules, the LLM fallback for ambiguous blocks, and the whole-page drop list.
sidebar:
  order: 3
  label: blocks
---

`lnvox.ingest.blocks` decides, for lecture-mode EPUB ingest, which parts of a page are narrated and which are shown to the reader instead. A page can be dropped entirely (TOC, copyright, index, …). Its blocks come out as `prose` (narrated) or as a non-prose kind (`code`, `table`, `figure`, `footnote`, `equation`) that becomes a reader-side visual element. The only caller is [`lnvox.ingest.epub._xhtml_to_blocks`](/library/ingest/epub/#_xhtml_to_blocks), reached through `lnvox ingest-epub --mode lecture` ([Stage 0a](/stages/ingest-epub/)).

The rules come first; the LLM only sees what they cannot decide. A book with clean semantic markup makes zero LLM calls (DESIGN.md §13.2a).

Source: `src/lnvox/ingest/blocks.py`.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`ClassifiedBlock`](#classifiedblock) | dataclass | One classified block |
| [`page_drop_reason`](#page_drop_reason) | function | Should this whole page be excised? Returns a reason or `None` |
| [`classify_page`](#classify_page) | function | Page → `(title, [ClassifiedBlock, …])` |
| [`DEFAULT_DROP_TYPES`](#default_drop_types) | constant | `epub:type` landmarks that drop a page |
| [`_classify_one`](#_classify_one) | function | The per-block rule ladder |
| [`_resolve_ambiguous`](#_resolve_ambiguous) | function | LLM fallback (or prose) for rule-ambiguous blocks |
| [`_text_of`](#_text_of) | function | Narrated text extraction. Must mirror Stage 6 |

## API

### `ClassifiedBlock`

```python
@dataclass
class ClassifiedBlock:
    kind: str
    text: str = ""
    html: str = ""
    tag: str = ""
    language: str = ""
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `str` | required | `prose`, `code`, `table`, `figure`, `footnote` or `equation`. `drop` exists only at page level (and as an LLM verdict, which is turned into an empty prose block) |
| `text` | `str` | `""` | narratable text; set for prose, `""` for non-prose |
| `html` | `str` | `""` | verbatim outer HTML (`str(el)`) for non-prose blocks |
| `tag` | `str` | `""` | source tag name |
| `language` | `str` | `""` | best-effort code language (see [`_code_language`](#_code_language)) |

### `page_drop_reason`

```python
def page_drop_reason(
    soup: BeautifulSoup,
    stem: str,
    *,
    drop_types: frozenset[str] = DEFAULT_DROP_TYPES,
) -> Optional[str]
```

Returns a short human-readable reason if the whole spine page should be excised, else `None`. The checks are deterministic and run in this order:

`stem` is the spine XHTML's **file** stem (`index` for `index.xhtml`); [`_xhtml_to_blocks`](/library/ingest/epub/#_xhtml_to_blocks) passes it straight through.

1. **Stem regex.** `stem` matches `_DROP_STEM_RE`:
   ```python
   r"^(toc|nav|index|bibliography|biblio|glossary|copyright|titlepage|"
   r"title-page|halftitle|half-title|colophon-toc)\b"   # re.IGNORECASE
   ```
   → `"stem '<stem>' matches boilerplate"`. The `\b` keeps `indexing` and `navigation` alive; `index_split_000` (Calibre's naming) survives too, since `_` is a word character.
2. **Page-level `epub:type` landmark.** A token of `epub:type` (also read from `epubtype` / `data-epub-type`) in `drop_types`, on `<body>` itself, or on top-level `<section>` / `<nav>` / `<article>` elements (ones not nested in another of those) that together hold **all** the page's lettered text → `"epub:type '<token>'"`. A drop landmark nested inside a chapter (a chapter-end `<section epub:type="bibliography">`, a mini-TOC `<nav>`), or sitting beside real prose, does not drop the page: [`classify_page`](#classify_page) drops just its blocks.
3. **Project Gutenberg wrappers.** Any element with class `pg-boilerplate`, `pgheader` or `pgfooter`, or id `pg-header` / `pg-footer` → `"publisher boilerplate (pg-header/footer)"`. This one is page-wide on purpose: PG puts its licence header in the same file as the title page and the linked contents table, and all of it should go.
4. **Nav document.** The body has a `<nav>` and no `<p>` at all → `"nav document (no prose)"`.
5. **Contents heading.** The first `<h1>`/`<h2>`/`<h3>` outside a drop landmark, lowercased, whitespace-collapsed and without a trailing `.`, is **exactly** one of `contents`, `table of contents`, `toc`, `index`, `bibliography`, `glossary`, `list of illustrations`, `list of figures` → `"heading '<text>'"`. Whole-title matches only: "Index Funds and You" and "Glossary of Terms" are chapters.
6. **Link density.** ≥ 5 `<a>` links, link text > 60 % of all body text, and no `<p>` → `"link-dense (TOC/index)"`.

A dropped page is logged by `extract_epub` as `· drop <stem>: boilerplate / contents page`. The specific reason is not printed. Call `page_drop_reason` on the page yourself to see it:

```python
from pathlib import Path
from bs4 import BeautifulSoup
from lnvox.ingest.blocks import page_drop_reason

page = Path("OEBPS/chapter9.xhtml")
soup = BeautifulSoup(page.read_bytes(), "html.parser")
print(page_drop_reason(soup, page.stem))
```

### `DEFAULT_DROP_TYPES`

```python
DEFAULT_DROP_TYPES = frozenset({
    "toc", "landmarks", "copyright-page", "titlepage", "title-page",
    "index", "bibliography", "loi", "lot",
})
```

The `epub:type` values that drop a page (`loi` / `lot` = list of illustrations / tables). `dedication`, `acknowledgments`, `preface`, `foreword` and `colophon` are deliberately absent, so they are kept and narrated.

DESIGN.md §13.2b and §13.7 describe this list as configurable through `config.yaml` (`ingest: { drop: [...] }`). No such setting exists. The list can only be overridden by passing `drop_types=` to `page_drop_reason` directly, and the ingest path never does.

### `classify_page`

```python
def classify_page(
    soup: BeautifulSoup,
    *,
    classifier: str = "fallback",
    llm=None,
    render_template=None,
    drop_types: frozenset[str] = DEFAULT_DROP_TYPES,
) -> tuple[str, list[ClassifiedBlock]]
```

Classifies one page into a title and an ordered block list.

- **Title**: [`_text_of`](#_text_of) of the first `<h1>`/`<h2>` in the body that is not inside a drop landmark, `""` if none. That element is *not* emitted as a block. Every other heading (later `<h1>`/`<h2>`, all `<h3>`–`<h6>`) is emitted as a prose block and narrated.
- **Blocks**: the top-level elements among
  ```python
  _BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6",
                 "pre", "table", "figure", "blockquote", "ul", "ol", "aside", "math")
  ```
  plus any element whose `epub:type` is `footnote`, `endnote` or `rearnote` (a `<div>` / `<li>` note is one block), in document order. An element with any ancestor in that set is skipped (`_top_level_blocks`), so the outer block owns everything inside it.
- **Nested boilerplate**: a block inside an element whose `epub:type` is in `drop_types` (a chapter-end bibliography section that did not drop the page, see [`page_drop_reason`](#page_drop_reason) check 2) is excised: neither narrated nor surfaced.
- Each remaining top-level element goes through [`_classify_one`](#_classify_one). Ambiguous results go through [`_resolve_ambiguous`](#_resolve_ambiguous).

**Parameters**
- `classifier`: `"fallback"` (the LLM decides ambiguous blocks, if `llm` and `render_template` are given) or `"none"` (ambiguous → prose).
- `llm`: an object with `.structured(system=, user=, schema=, max_tokens=)`, normally an [`LLMClient`](/library/llm/client/).
- `render_template`: a callable `(template_name, **kwargs) -> str`, normally `llm.render`.
- `drop_types`: the landmark types whose blocks are excised (default [`DEFAULT_DROP_TYPES`](#default_drop_types)).

```python
from bs4 import BeautifulSoup
from lnvox.ingest.blocks import classify_page

soup = BeautifulSoup(
    "<body><h1>Chapter One</h1><p>Prose.</p>"
    "<pre><code>print('hi')</code></pre>"
    "<table><tr><td>a</td></tr></table></body>", "html.parser")
title, blocks = classify_page(soup, classifier="none")
# title == "Chapter One"; [b.kind for b in blocks] == ["prose", "code", "table"]
```

### `_classify_one`

```python
def _classify_one(
    el: Tag, *, title_el: Optional[Tag]
) -> Optional[tuple[ClassifiedBlock, bool]]
```

The rule ladder for one top-level block. It returns `(block, needs_llm)`, or `None` to skip the element.

| Element | Result |
|---|---|
| `<pre>` with text | `code`, `html=str(el)`, language from the inner `<code>` if there is one, else from the `<pre>` |
| `<table>` with text or an image | `table` |
| `<figure>` with text or an image | `figure` |
| `<math>` with text | `equation` |
| `<aside>` whose `epub:type` has `footnote`, `endnote`, `rearnote`, `note` or `footnotes` | `footnote` |
| any element whose `epub:type` has `footnote`, `endnote` or `rearnote`; any block inside an element typed with one of those or `footnotes` / `endnotes` / `rearnotes`; a block (e.g. `<ol>`) whose every lettered string sits in such a note | `footnote` |
| empty element, or the title heading | `None` (skipped) |
| `<p>` / `<blockquote>` whose class matches `_CODE_HINT_RE`, or whose inline `style` sets a `monospace` / `courier` / `consolas` font-family | **ambiguous**: `code` with `needs_llm=True` |
| any other `<p>`, `<blockquote>`, `<ul>`, `<ol>`, heading, non-note `<aside>` | `prose` with [`_text_of`](#_text_of) text |

```python
_CODE_HINT_RE = re.compile(
    r"\b(code|sourcecode|source-code|codeblock|code-block|listing|"
    r"mono|monospace|verbatim|prettyprint|highlight|programlisting)\b",
    re.IGNORECASE,
)
```

What the rules do **not** catch. Check this list when non-fiction reads oddly:
- **Nesting.** A `<table>` or `<pre>` inside a `<blockquote>`, `<ul>`/`<ol>` or `<aside>` is part of that outer block, which is classified as prose, so the table or code is **read aloud**.
- **Loose text.** `<div>` is not a block tag. Text sitting directly in a `<div>` or `<span>` (not in a `<p>`) is **silently dropped**.
- **Inline images.** A `<p><img/></p>` has no text and is skipped. It becomes no visual element (the image is still in `images[]`, which Stage 6 places from the EPUB).
- **`epub:type="note"` outside `<aside>`.** Only an `<aside>` counts `note` as a footnote. A `<div epub:type="note">` is as often a narrated callout box, so it stays prose.
- **Inline notes.** A `<span epub:type="footnote">` inside a `<p>` is part of that paragraph and is narrated with it.
- **Display equations** that are not MathML (`<div class="equation">`, images of formulas) are not detected.

### `_code_language`

```python
def _code_language(el: Tag) -> str
```

Reads the class tokens of `el`. It looks for `language-X`, `lang-X`, `brush:X` or `sourceCode-X` (`_LANG_CLASS_RE`), then for a bare class in `_KNOWN_LANGS` (`python`, `js`, `rust`, `sql`, `bash`, …). It returns `""` if none match.

For `<pre>`, the classes read are the inner `<code>`'s when a `<code>` exists. `<pre class="language-python"><code>…</code></pre>` therefore yields `""`, and Pygments falls back to guessing. Code resolved by the LLM from a styled `<p>` never has a language.

### `_resolve_ambiguous`

```python
def _resolve_ambiguous(
    block: ClassifiedBlock,
    el: Tag,
    *,
    classifier: str,
    llm,
    render_template,
) -> ClassifiedBlock
```

Decides a code-hinted `<p>` / `<blockquote>`:

- `classifier == "none"`, or no `llm`, or no `render_template` → **prose**, with the text kept ("untagged ⇒ prose").
- Otherwise it makes one LLM call: `render_template("block_classify.jinja", block_html=str(el)[:2000])` → `llm.structured(system=_CLASSIFY_SYSTEM, user=…, schema=BlockClass, max_tokens=256)`. [`BlockClass`](/library/llm/schemas/) is `{kind, reason}`, with `kind` one of the seven `BlockKind` values.
  - `prose` → prose;
  - `drop` → prose with empty text. `_xhtml_to_blocks` filters it out, so the block is neither narrated nor shown;
  - `code` / `table` / `figure` / `footnote` / `equation` → that kind, with `html=str(el)` and empty text.
- **A bad answer** (schema failure after the client's retries, a template error) → prose.
- **A transport error** (server unreachable, 5xx; [`is_transport_error`](/library/llm/client/)) is **re-raised**, so `ingest-epub --mode lecture` fails instead of silently narrating every ambiguous block as prose.

:::note
Fallback classifications are **not cached**. Every `ingest-epub --mode lecture` run re-asks the LLM for each ambiguous block. To run without a server, pass `--ingest-classifier none`.
:::

### `_text_of`

```python
def _text_of(el: Tag) -> str
```

Returns `el.get_text(separator=" ")` with all whitespace collapsed to single spaces. The space at every node boundary is intentional. The narrated text becomes the lecture beat's `source_span`, which is the Stage 6 sync key, and Stage 6's shadow reconstruction inserts the same spaces. The result is artifacts like `S<small>PACE</small>` → `"S PACE"` and `<br/>` → space. Fixing those is the job of the lecture stage's normalize pass (DESIGN.md §13.6), not the extractor's: changing this function desynchronises sync anchors.

## Notes

- The module imports `bs4` at import time. It is a core dependency, so this is always safe.
- The LLM prompt lives in `src/lnvox/llm/prompts/block_classify.jinja`, and the system prompt is `_CLASSIFY_SYSTEM`.
- `.txt` sources never pass through this module. Lecture mode on a plain-text folder treats everything as prose.

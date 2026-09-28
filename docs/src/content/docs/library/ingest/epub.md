---
title: lnvox.ingest.epub
description: EPUB to novels-folder extractor. OPF parsing, cover and image order, skip rules, multi-part grouping, --split-chapters, lecture mode, .epub_meta.json.
sidebar:
  order: 2
  label: epub
---

`lnvox.ingest.epub` turns an EPUB into the folder layout that [`lnvox ingest`](/stages/ingest/) reads: one `NN-<slug>.txt` per chapter (`NNN-` from 100 chapters up), an `images/` directory, and a `.epub_meta.json` sidecar. It is the engine behind [`lnvox ingest-epub`](/stages/ingest-epub/) (Stage 0a). Stage 6 imports two helpers from it, [`chapter_markers`](#chapter_markers) and [`section_key`](#section_key), so that pictures on a split page are credited to the correct chapter.

Source: `src/lnvox/ingest/epub.py`. Design: DESIGN.md §2.0, §13.2, §18, §23.2.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`extract_epub`](#extract_epub) | function | The whole extraction. Returns [`EpubMeta`](#epubmeta) |
| [`EpubMeta`](#epubmeta) | dataclass | In-memory form of `.epub_meta.json` |
| [`chapter_markers`](#chapter_markers) | function | Chapter-start elements on a page (`--split-chapters` rule, shared with s6) |
| [`section_key`](#section_key) | function | `source_parts` key for a split section (shared with s6) |
| [`_SKIP_STEM_PATTERNS`](#_skip_stem_patterns--_is_skip) | constant | Spine stems never turned into chapters |
| [`_CHAPTER_BASE_RE`](#_chapter_base_re--_chapter_base) | constant | Multi-part chapter grouping (`chapter1` + `chapter1_1`) |
| [`_split_page_sections`](#_split_page_sections) | function | Cuts one page into several chapters |
| [`_nav_titles`](#_nav_titles--_is_bare_numeral_title) | function | Nav/NCX titles used to rescue numeral-only headings |
| [`_xhtml_to_text`](#_xhtml_to_text) | function | Narration-mode page to `(title, text)` |
| [`_xhtml_to_blocks`](#_xhtml_to_blocks) / [`_visual_element_for`](#_visual_element_for) | functions | Lecture-mode page parse and visual-element records |

Output layout:

```text
<output_dir>/
    01-prologue.txt
    02-the-lantern-road.txt
    ...
    images/
        cover.jpg
        insert1.jpg
        code_3a8f1c09b2d4.png      # lecture mode only (rendered blocks)
    .epub_meta.json
```

## API

### `extract_epub`

```python
def extract_epub(
    epub_path: Path,
    output_dir: Path,
    *,
    mode: str = "narration",
    classifier: str = "fallback",
    render: bool = True,
    split_chapters: bool = False,
    llm=None,
    progress=print,
) -> EpubMeta
```

Extracts `epub_path` into `output_dir` and returns the metadata it also writes to `.epub_meta.json`.

**Parameters**
- `epub_path`: the `.epub` (a zip).
- `output_dir`: destination. It is created along with `output_dir/images/`. When it already holds a `.epub_meta.json`, the chapter `.txt` files **that sidecar lists** are deleted before the new ones are written, so a re-extraction with other options leaves no stale chapters behind. Nothing else in the folder is touched (hand-written `.txt` files, `images/`), and a listed name that is not a bare `*.txt` inside `output_dir` is ignored.
- `mode`: `"narration"` (default) or `"lecture"`. The function does not validate this value: anything other than `"lecture"` takes the narration path. The CLI rejects other values.
- `classifier`: lecture only. `"fallback"` sends ambiguous blocks to the LLM when `llm` is given. `"none"` resolves them to prose. See [`lnvox.ingest.blocks`](/library/ingest/blocks/).
- `render`: lecture only. Rasterize code/table blocks to PNG when the `render` extra is available (default `True`). The CLI sets it from `--no-render`.
- `split_chapters`: narration only. Cut a spine page into several chapters at its chapter markers (DESIGN.md §18). Default `False`. **Ignored in lecture mode**: that branch runs before the split check.
- `llm`: an optional [`LLMClient`](/library/llm/client/). In lecture mode `llm.structured` does the fallback classification and `llm.render` renders the `block_classify.jinja` prompt. `None` means rules only.
- `progress`: a one-argument callable for log lines (default `print`).

**Returns** [`EpubMeta`](#epubmeta).

**Raises**
- `ValueError("Not a valid EPUB (missing META-INF/container.xml): …")` when the zip has no container.
- `ValueError("Could not locate OPF rootfile in …")` when `container.xml` has no `<rootfile full-path>`.
- `KeyError` from `zipfile` when the manifest lists an image (or a spine XHTML) that is not in the archive. Image extraction does not catch this.
- `zipfile.BadZipFile` for a file that is not a zip.

**Writes** `output_dir/NN-<slug>.txt`, `output_dir/images/*`, `output_dir/.epub_meta.json`. **Deletes** the previous run's chapter files (see `output_dir`).

#### Step by step

1. **Locate the OPF.** `META-INF/container.xml` → first `<rootfile full-path=…>`. The OPF's directory (`opf_dir`) is the base for every manifest href.
2. **Parse the OPF.**
   - `<dc:title>` → `title`, every `<dc:creator>` → `authors` (in order), `<dc:publisher>`, and `<dc:language>` (default `"en"` when absent).
   - EPUB 2 cover hint: `<meta name="cover" content="<item-id>"/>`. If there are several, the last one wins.
   - Manifest: `id → (href, media-type, properties)`. Spine: the ordered `itemref idref`s.
3. **Extract images** (3a). Every manifest item whose media type starts with `image/` is copied to `images/<basename of href>`. Two images with the same basename in different folders overwrite each other.
   - **Cover detection.** The *first* image in manifest order that matches any of these becomes `cover_image`:
     - `properties` contains `cover-image` (EPUB 3);
     - its id equals the EPUB 2 `<meta name="cover">` content;
     - its filename stem is `cover` (case-insensitive).
4. **Order images** (3b, 3c) into `meta.images`, which Stage 5 embeds in this order (DESIGN.md §23):
   1. the cover first, if one was found;
   2. then every `<img>` / `<image>` referenced by each spine page (`src`, `xlink:href` or `href`), in spine order and document order. The walk covers **every** HTML/XML spine item, including the cover, insert and TOC pages that are skipped for text. A reference is percent-decoded and resolved against the page's own directory with `posixpath.normpath` (`../Images/a.jpg` from `Text/ch1.xhtml` → `Images/a.jpg`), then looked up among the equally normalised manifest hrefs. When that fails (a reference escaping the OPF directory, a manifest that disagrees with the file layout), the image is matched by basename;
   3. then any manifest image no spine page references;
   4. each image once only (duplicates dropped).
5. **Nav titles.** [`_nav_titles`](#_nav_titles--_is_bare_numeral_title) builds `stem → title` and `stem#anchor → title` maps from the nav document or NCX. They are used only in step 7.
6. **Walk the spine for text.** For each spine item whose media type contains `html`/`xhtml`:
   - skip it when its stem matches [`_SKIP_STEM_PATTERNS`](#_skip_stem_patterns--_is_skip);
   - **lecture mode**: [`_xhtml_to_blocks`](#_xhtml_to_blocks). A dropped page logs `· drop <stem>: boilerplate / contents page`. A page with no prose and no blocks is skipped;
   - **narration mode**: [`_xhtml_to_text`](#_xhtml_to_text), or [`_split_page_sections`](#_split_page_sections) when `split_chapters`. Sections whose text is empty are skipped. A page with no `<p>` text therefore yields nothing;
   - **grouping**: each surviving part is appended to a group keyed by [`_chapter_base(stem)`](#_chapter_base_re--_chapter_base). When a page was split into ≥ 2 sections, each section gets its own group keyed by its section key. Groups are numbered in order of first appearance.
7. **Write chapters.** First the chapter files listed in an existing `.epub_meta.json` are removed (`removed N chapter file(s) from the previous extraction`). Then, for each group, numbered `i = 1, 2, …`:
   - parts are sorted by spine index;
   - **title**: the first non-empty part title. If there is none, the humanised group key is used: `base.replace("_", " ").title()`, e.g. `section0001` → `Section0001`;
   - **numeral rescue**: if the title is a bare numeral (`III`, `Chapter 3`, `IV.`), each part's own key is looked up in the nav titles. That key is the plain stem for an unsplit page, or `stem#anchor` for a split section. If a split section has no entry, its bare stem is tried, but only when that stem contributes exactly one part across the whole book. Without that guard, all 23 chapters sharing one Oz XHTML would get the same nav entry. A `stem#secNNN` key (a heading without an `id`) never matches a nav fragment. The first nav title that is not itself a bare numeral wins;
   - `chapter_id = f"{i:0{w}d}"` with `w = max(2, len(str(chapter_count)))`, filename `f"{chapter_id}-{_slugify(title)}.txt"`. Books under 100 chapters keep two-digit ids (`01`); a 100–999 chapter book gets `001`–`NNN`, so ids and file names sort correctly as strings everywhere downstream;
   - file content: `f"{title}\n\n{body}\n"`, where body is the parts' text joined by blank lines;
   - one progress line per chapter: `02-the-lantern-road.txt  (18,204 chars from 2 part(s))`. Lecture mode adds `, N visual block(s)`.
8. **Write `.epub_meta.json`** and log `Done. N chapter(s), M image(s) → <dir>`.

### `EpubMeta`

```python
@dataclass
class EpubMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    language: str = "en"
    cover_image: str = ""  # relative to output_dir
    images: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    visual_elements: list[dict] = field(default_factory=list)
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | `str` | `""` | `<dc:title>`, stripped |
| `authors` | `list[str]` | `[]` | every `<dc:creator>` in OPF order |
| `publisher` | `str` | `""` | `<dc:publisher>` |
| `language` | `str` | `"en"` | `<dc:language>` when present |
| `cover_image` | `str` | `""` | path relative to `output_dir` (`images/cover.jpg`); `""` if no cover was detected |
| `images` | `list[str]` | `[]` | every extracted image, cover first then document-flow order (step 4) |
| `chapters` | `list[dict]` | `[]` | `{"file", "title", "source_parts"}` per written chapter |
| `visual_elements` | `list[dict]` | `[]` | lecture mode only: non-prose blocks (see [below](#visual-element-records)) |

### `.epub_meta.json`

This is `EpubMeta` serialised with `indent=2, ensure_ascii=False`, with the keys in the field order above. Narration-mode example (invented book):

```json title="novels/my-series/volume-01/.epub_meta.json"
{
  "title": "The Lantern Road",
  "authors": ["A. N. Author"],
  "publisher": "Example Press",
  "language": "en",
  "cover_image": "images/cover.jpg",
  "images": [
    "images/cover.jpg",
    "images/insert1.jpg",
    "images/insert2.jpg",
    "images/insert10.jpg"
  ],
  "chapters": [
    {"file": "01-prologue.txt", "title": "Prologue", "source_parts": ["prologue"]},
    {"file": "02-the-lantern-road.txt", "title": "The Lantern Road", "source_parts": ["chapter1", "chapter1_1"]},
    {"file": "03-a-door-in-the-hill.txt", "title": "A Door in the Hill", "source_parts": ["chapter2"]}
  ],
  "visual_elements": []
}
```

With `--split-chapters`, a split page's parts carry the section key (`<stem>#<id>` or `<stem>#secNNN`):

```json
{"file": "04-the-road.txt", "title": "The Road", "source_parts": ["book-h-2#Chapter_II"]}
```

Who reads it:

| Consumer | Fields used |
|---|---|
| `lnvox ingest` (Stage 0) | copies the whole file to `artifacts/<book>/00_book_meta.json`, with `cover_image` made absolute |
| Stage 5 (`s5_mix.resolve_book_images`) | `cover_image` (through `00_book_meta.json`) and `images` (read from the novels folder) for the embedded pictures and their order |
| Stage 6 (`s6_sync`) | `chapters[].file` (chapter id = the prefix before the first `-`), `chapters[].source_parts` (the `#anchor` is stripped to find the XHTML, and kept to map pictures on shared pages), `visual_elements`, and `images` through `resolve_book_images` so manifest image slots match the m4b |
| `lnvox sample` | `chapters[].file` / `title` for chapter names. The command refuses to run without the file |

:::caution[The novels folder is not disposable]
Stages 5 and 6 read `.epub_meta.json` and `images/` from `novels/<book_id>/` directly, long after Stage 0. Keep the folder in place for the life of the book.
:::

#### Visual-element records

Lecture mode only. [`_visual_element_for`](#_visual_element_for) produces one record per non-prose block:

```json
{
  "kind": "code",
  "src": "images/code_3a8f1c09b2d4.png",
  "html": "<pre><code class=\"language-python\">def area(r):\n    return 3.14159 * r * r</code></pre>",
  "language": "python",
  "chapter_id": "03",
  "after_paragraph": 41,
  "spine_page": "chapter3"
}
```

| Key | Meaning |
|---|---|
| `kind` | `code`, `table`, `figure`, `footnote` or `equation` |
| `src` | rendered PNG (`images/<kind>_<hash12>.png`), the extracted illustration for a figure, or `""` when nothing was rendered |
| `html` | verbatim outer HTML of the block (the reader falls back to it) |
| `language` | code language hint, `""` if none |
| `chapter_id` | the `NN` (or `NNN`) of the chapter it belongs to |
| `after_paragraph` | 0-based **chapter-global** index of the prose paragraph it follows. `-1` means before the first paragraph. The index matches `split_paragraphs` over the written body, which is how Stage 6 turns it into beat ids and a trigger time |
| `spine_page` | XHTML stem the block came from |

DESIGN.md §13.2d also shows an `"xhtml"` key. The code does not write it; `spine_page` carries the stem.

### `_SKIP_STEM_PATTERNS` / `_is_skip`

```python
_SKIP_STEM_PATTERNS = (
    re.compile(r"^cover", re.IGNORECASE),
    re.compile(r"^toc", re.IGNORECASE),
    re.compile(r"^tocimg", re.IGNORECASE),
    re.compile(r"^copyright", re.IGNORECASE),
    re.compile(r"^signup", re.IGNORECASE),
    re.compile(r"^insert\d+", re.IGNORECASE),
    re.compile(r"^bonus\d+", re.IGNORECASE),
    re.compile(r"^color\d+", re.IGNORECASE),
)

def _is_skip(stem: str) -> bool
```

Spine items whose **file stem** (href basename without extension) matches any pattern are never turned into chapter text, in either mode. The images they reference are still extracted and ordered.

The patterns are anchored prefixes, not whole-name matches:
- `CoverPage`, `cover_back`, `copyright-page` are all skipped;
- `insert` (without a digit) is **not** skipped, and neither are `nav`, `index`, `titlepage`, `dedication` or `front-matter`;
- `toc` also catches any stem that merely starts with those letters. A chapter file called `tocsin.xhtml` would be skipped.

If a real chapter disappears, check its file stem against this list first.

### `_CHAPTER_BASE_RE` / `_chapter_base`

```python
_CHAPTER_BASE_RE = re.compile(
    r"^(chapter\d+|prologue|epilogue|afterword|interlude\d+|intro|preface)(?:_\d+)?$",
    re.IGNORECASE,
)

def _chapter_base(stem: str) -> str
```

Group key for multi-part chapters. A stem that matches returns the captured base, lowercased. Anything else returns the stem, lowercased.

| stem | group key | effect |
|---|---|---|
| `chapter1`, `chapter1_1`, `Chapter1_2` | `chapter1` | merged into one chapter |
| `prologue_2`, `interlude3_1` | `prologue`, `interlude3` | merged into the base |
| `chapter_1`, `chapter_1_2` | `chapter_1`, `chapter_1_2` | **not** merged (`chapter\d+` needs the digit right after `chapter`) |
| `part0001_split_000`, `part0001_split_001` | themselves | **not** merged: Calibre split files stay separate chapters |
| `Section0001` | `section0001` | own chapter |

Parts are merged even when they are not adjacent in the spine. The group is placed where its first part appeared, and its parts are sorted by spine index. Grouping is case-insensitive, so `Chapter1.xhtml` in one folder and `chapter1.xhtml` in another merge too.

### `_xhtml_to_text`

```python
def _xhtml_to_text(xhtml_bytes: bytes) -> tuple[str, str]
```

Narration-mode page parser, using BeautifulSoup `html.parser`:
- title: text of the **first `<h1>` or `<h2>`** (`""` if none). `<h3>` is not considered here;
- every `<img>` is removed;
- body: the text of **every `<p>`** (`get_text(" ", strip=True)`), empty ones dropped, joined by `\n\n`. Section breaks such as `◆◆◆` survive when they are in a `<p>`.

:::caution[Only `<p>` text is read]
Text that is not inside a `<p>` is dropped silently: bare text in `<div>`, `<li>`, `<pre>`, `<td>` without a `<p>`, or poetry laid out as `<div class="line">`. If a chapter comes out short or missing (a page with no `<p>` produces no chapter at all), open the XHTML and look for this first.
:::

### `chapter_markers`

```python
def chapter_markers(soup) -> list
```

Every element on the page, in document order, that [`_is_chapter_marker`](#_chapter_anchor_re--_is_chapter_marker--_marker_title) accepts, **except markers nested inside another marker**. Gutenberg's `<h2><a id="chap01"/>CHAPTER I.</h2>` and a `<section id="chapter12"><h1>…</h1>` are one chapter start each, not two; the outer element is kept. Used by `_split_page_sections` and by Stage 6's picture-ownership pass (DESIGN.md §23.2), so both split a page the same way.

### `section_key`

```python
def section_key(stem: str, marker, index: int) -> str
```

Returns `f"{stem}#{marker.get('id') or f'sec{index:03d}'}"`. The key uses the marker's own `id` when it has one (`book-h-2#Chapter_XI`); otherwise it uses its 0-based position among the page's markers (`p1#sec000`). This string is what ends up in `source_parts`. Stage 6 rebuilds it with the same function, so do not change the format without changing both sides.

### `_CHAPTER_ANCHOR_RE` / `_is_chapter_marker` / `_marker_title`

```python
_CHAPTER_ANCHOR_RE = re.compile(r"^(?:chap|chapter)[_\-]?[0-9ivxlcdm]+$", re.I)
_HEADING_TAGS = ("h1", "h2", "h3")

def _is_chapter_marker(el) -> bool
def _marker_title(el) -> str
```

An element is a chapter marker if it is an `<h1>`–`<h3>`, **or** any element whose `id` matches `_CHAPTER_ANCHOR_RE` (Gutenberg Ebookmaker's `<a id="Chapter_XI"/>`). Page anchors such as `Page_117` or `pgepubid00103` do not match.

Marker title, first match wins:
1. a heading's own text;
2. for an anchor that wraps a heading (`<a id="Chapter_I"><h2>…</h2></a>`), that heading's text;
3. otherwise the `alt` (or `title`) of the next `<img>` after the marker in document order. In illustrated editions this is the chapter-head image;
4. otherwise the id with `_` turned into spaces (`Chapter_XI` → `Chapter XI`).

DESIGN.md §18.3 says "nearest *preceding* `<img alt>`". The code uses `find_next("img")`, so it takes the *following* image, which can be any later image on the page.

### `_split_page_sections`

```python
def _split_page_sections(
    xhtml_bytes: bytes, stem: str
) -> list[tuple[str, str, str]]
```

Returns `(section_key, title, text)` tuples for one page. With fewer than two markers it returns `[(stem, title, text)]` from `_xhtml_to_text`, which is exactly the unsplit result. With two or more:

1. titles are read from every marker **before** images are removed, because the anchor case needs the image `alt`;
2. `<img>`s are removed, then the page is walked in document order. Each `<p>` goes into the bucket of the most recent marker;
3. `<p>`s before the first marker (an epigraph, a part title in a `<p>`) are prepended to the **first** section of the same page. They are never attached to the previous page's chapter, although DESIGN.md §18.3 allows either;
4. sections are keyed with [`section_key`](#section_key).

Back in `extract_epub`, a section with empty text is dropped, so a heading-only section (for example `<h2>Part One</h2>` directly followed by `<h2>Chapter 1</h2>`) disappears along with its title.

#### Debugging a bad split

| Symptom | Likely cause |
|---|---|
| A chapter per sub-heading | The page uses `<h3>` for subsections, and every `<h1>`–`<h3>` is a marker. This is why the flag is opt-in (DESIGN.md §18.4). Re-run without `--split-chapters` |
| A chapter titled `chap01` (or an image caption) | A bare `<a id="chap01"/>` anchor with no heading inside it or around it: the title falls back to the next image's `alt`, then the id. (An anchor *inside* a heading, or a heading inside a `chap…`-id element, counts once and takes the heading text.) |
| Page not split at all | Fewer than two markers: chapter titles are images inside `<div>`s with no heading and no `chap…` id |
| Epigraph attached to the wrong chapter | Rule 3 above: preamble joins the page's first section |

`source_parts` in `.epub_meta.json` tells you exactly which page and anchor each chapter came from.

### `_nav_titles` / `_is_bare_numeral_title`

```python
def _nav_titles(zf, opf_dir: str, manifest: dict) -> dict[str, str]

_BARE_NUMERAL_RE = re.compile(
    r"^(?:chapter\s+)?(?:[0-9]+|[ivxlcdm]+)[.:)]?$", re.I
)
def _is_bare_numeral_title(title: str) -> bool
```

Standard Ebooks and similar publishers put `<h2>III</h2>` in the body and the real name only in the nav. `_nav_titles` reads the nav document: a manifest item whose `properties` contain `nav`, or a file named `toc.xhtml` / `nav.xhtml`. For each `<a href>` it records `stem#fragment → text` and `stem → text`, and the first link for a key wins. The NCX (`navPoint` / `navLabel` / `content src`) is consulted **only if the nav yields nothing**, and it only records stem-level keys.

`_is_bare_numeral_title` collapses whitespace and matches an optional `chapter `, then digits or roman-numeral letters, then an optional `.`, `:` or `)`. `Chapter III: The Road` is not bare. A real heading always wins over the nav; the nav is only a rescue.

With `--split-chapters`, a book whose only TOC is an NCX cannot be rescued, because the NCX has no fragment keys and a shared stem is never looked up by stem alone.

### `_slugify`

```python
def _slugify(s: str, max_len: int = 80) -> str
```

Lowercases the title, removes everything except word characters, whitespace and `-`, collapses runs of whitespace/`_`/`-` to a single `-`, trims, and cuts to 80 characters. Unicode letters are kept (`L'été à Paris` → `lété-à-paris`). An empty result becomes `untitled`.

### `_xhtml_to_blocks`

```python
def _xhtml_to_blocks(
    xhtml_bytes: bytes,
    *,
    stem: str = "",
    classifier: str = "fallback",
    llm=None,
    render_template=None,
)
```

Lecture-mode page parse (DESIGN.md §13.2). Returns `None` when [`page_drop_reason`](/library/ingest/blocks/#page_drop_reason) says the page is boilerplate. Otherwise it returns `(title, paragraphs, blocks)`, where `paragraphs` are the prose block texts and `blocks` is `[(after_paragraph, ClassifiedBlock), …]`. `after_paragraph` is local to the page (`len(paragraphs) - 1` at the time the block is met, so `-1` means before any prose).

`stem` is the spine XHTML's file stem, passed to `page_drop_reason` for its boilerplate-stem rule, so `index.xhtml` is dropped by name while a chapter headed "Index Funds and You" is kept. Headings count only as whole titles ("Index", "Contents"); see [`page_drop_reason`](/library/ingest/blocks/#page_drop_reason).

### `_visual_element_for`

```python
def _visual_element_for(
    block,
    *,
    chapter_id: str,
    after_paragraph: int,
    stem: str,
    images_dir: Path,
    image_basename_to_rel: dict[str, str],
    do_render: bool,
) -> dict
```

Builds one [visual-element record](#visual-element-records):
- **figure**: finds the `<img>`/`<image>` inside the block and looks up its basename among the already-extracted `images`. On a hit, `src` points at that file and nothing is rendered. A caption-only figure, or one whose image cannot be resolved, falls through to rendering;
- **code / table / unresolved figure**, when `do_render`: the output name is `f"{kind}_{sha256(kind + (html or text) + language)[:12]}.png"` in `images/`. If that file already exists it is reused without re-rendering, which makes re-runs cheap. Otherwise [`render.render_block`](/library/ingest/render/#render_block) is called. `src` is set only on success;
- **footnote / equation**: never rendered, so `src` is `""`.

`do_render` is `mode == "lecture" and render`, and it is forced to `False` (with one `[render] Playwright not installed …` log line) when [`render.available()`](/library/ingest/render/#available) is false.

## Notes

- **Lecture paragraph coordinates.** Parts of a merged chapter are concatenated into one paragraph list. A block's page-local `after_paragraph` is shifted by the number of paragraphs in earlier parts. A block before any prose on a later part gets the previous part's last paragraph index. The written `.txt` body is exactly those paragraphs joined by blank lines, so `after_paragraph` lines up with `split_paragraphs` in [the lecture stage](/stages/lecture/) and Stage 6.
- **The Project Gutenberg header is kept in narration mode.** Only lecture mode's drop list removes `pg-header` / `pg-footer` pages. A Gutenberg EPUB ingested for narration typically starts with a chapter titled "The Project Gutenberg eBook of …". Delete that `.txt` before `lnvox ingest` if you do not want it narrated.
- **Idempotency.** Chapter numbering is deterministic for a given EPUB and flag set, and rendered PNGs are content-hash named. A re-extraction removes the chapter files the previous run listed in `.epub_meta.json`, so switching `--split-chapters` or `--mode` leaves exactly the new set. Stale rendered PNGs stay in `images/` (harmless: nothing references them).
- **Re-chaptering invalidates everything downstream.** Chapter ids are positions (`01`, `02`, …). Turning on `--split-chapters` for a book that already went through s1–s4 renumbers chapters, and every per-chapter artifact, cached voice decision and prior-volume continuity link then points at the wrong text (DESIGN.md §18.4).
- **Lecture mode ignores `--split-chapters`** (DESIGN.md §18.5).

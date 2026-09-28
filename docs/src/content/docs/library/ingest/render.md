---
title: lnvox.ingest.render
description: Rasterizes lecture-mode code and table blocks to PNG with Pygments and headless Chromium. Lazy imports, and it degrades to HTML-only without the render extra.
sidebar:
  order: 4
  label: render
---

`lnvox.ingest.render` turns a [`ClassifiedBlock`](/library/ingest/blocks/#classifiedblock) into a PNG. Code is syntax-highlighted with Pygments, tables get a bordered stylesheet, and the resulting HTML page is screenshotted by Playwright's headless Chromium. Its only caller is [`lnvox.ingest.epub._visual_element_for`](/library/ingest/epub/#_visual_element_for) during `lnvox ingest-epub --mode lecture` ([Stage 0a](/stages/ingest-epub/)). The PNGs become visual elements that Stage 6 shows to the reader at the right playback time (DESIGN.md §13.2c).

Both heavy dependencies are optional and imported lazily. Importing this module always works: `available()` reports whether rendering can run, and `render_block()` returns `False` instead of raising.

Source: `src/lnvox/ingest/render.py`.

## At a glance

| Name | Purpose |
|---|---|
| [`available`](#available) | Is Playwright importable? |
| [`build_block_html`](#build_block_html) | Block → self-contained HTML document (pure, no browser) |
| [`render_html_to_png`](#render_html_to_png) | HTML document → PNG via Chromium |
| [`render_block`](#render_block) | `build_block_html` + `render_html_to_png` |
| [`_highlight_code`](#_highlight_code) / [`_code_text`](#_code_text) | Pygments highlighting and code extraction |
| `_PAGE_CSS` | The stylesheet every rendered block gets |

## Installing the `render` extra

```bash
uv sync --extra render            # add alongside the extras you already use
uv run playwright install chromium   # one-time browser download
```

The extra (in `pyproject.toml`) is `pygments>=2.17` and `playwright>=1.40`. `uv sync` removes extras you do not list. The launcher's `uv sync --extra …` calls do not list `render`, so after a launcher run the extra is gone from the venv. Re-add it before the next lecture extraction. The Chromium download lives outside the venv and survives.

## API

### `available`

```python
def available() -> bool
```

`True` if `import playwright.sync_api` succeeds. It checks only the Python package, **not** whether the Chromium browser was downloaded.

### `build_block_html`

```python
def build_block_html(block: "ClassifiedBlock") -> str
```

Wraps a block in a complete HTML document: `<!doctype html>`, UTF-8, `<style>` holding `_PAGE_CSS`, and the content in `<body>`. It is pure and needs no Playwright, so it can be unit-tested (`tests/test_lecture.py`).

- `kind == "code"`: the code text is [`_code_text(block.html)`](#_code_text), or `block.text` when there is no HTML. It is highlighted with [`_highlight_code(code, block.language)`](#_highlight_code).
- any other kind: `block.html` verbatim (or the escaped `block.text`), styled by the page CSS.

`_PAGE_CSS` sets a white body with 24 px padding. Code uses `"JetBrains Mono", Menlo, Consolas, monospace` at 15 px / 1.5 line height on a `#f6f8fa` panel, with `pre-wrap` so long lines wrap instead of overflowing. Tables are `border-collapse` at full width with `1px solid #d0d7de` cells and a shaded header row. Figcaptions are 13 px grey.

### `render_html_to_png`

```python
def render_html_to_png(
    html_doc: str, out_path: Path, *, width: int = 1080, scale: int = 2
) -> bool
```

Launches Chromium and opens a page with a `width × 100` viewport and `device_scale_factor=scale`. It calls `set_content(html_doc, wait_until="networkidle")` and screenshots the `<body>` element, so the PNG is cropped to the content. Tall listings produce tall images. With the defaults the image is 2160 px wide. `out_path.parent` is created if needed.

**Returns** `False` if `available()` is false or **any** exception is raised (browser binary missing, launch failure, timeout). Otherwise it returns whether `out_path` exists afterwards.

A new browser is launched **for every call**. A book with hundreds of code blocks pays the Chromium start-up cost hundreds of times. Content-hash file names in the caller keep re-runs from repeating it.

### `render_block`

```python
def render_block(
    block: "ClassifiedBlock", out_path: Path, *, width: int = 1080, scale: int = 2
) -> bool
```

`render_html_to_png(build_block_html(block), out_path, width=width, scale=scale)`. The caller in `epub.py` passes only `block` and `out_path`, so the defaults apply.

### `_highlight_code`

```python
def _highlight_code(code: str, language: str = "") -> str
```

Pygments lexer choice: `get_lexer_by_name(language)` when a language is given, then `guess_lexer(code)`, then `TextLexer`. The formatter is `HtmlFormatter(noclasses=True, nowrap=False, style="default")`, which uses inline styles so no external stylesheet is needed. If Pygments is not installed or anything else fails, it returns an HTML-escaped `<pre>`, so the output is plain monospace but still correct.

### `_code_text`

```python
def _code_text(block_html: str) -> str
```

The raw text of the first `<pre>` (or `<code>`, or the whole fragment) in the block HTML.

## Degradation

| Situation | Result |
|---|---|
| `render` extra not installed | `extract_epub` logs `[render] Playwright not installed — code/table blocks recorded as HTML-only visual elements …` once. Every code/table visual element gets `src: ""` plus its `html` |
| Playwright installed, Chromium not downloaded | `available()` is `True`, so there is no log line. Each `render_block` fails inside `render_html_to_png` and returns `False`, and the element is HTML-only, **silently**. Look for `"src": ""` on code/table entries in `.epub_meta.json` |
| Pygments missing, Playwright present | PNG rendered from an unhighlighted `<pre>` |
| `--no-render` | Rendering is never attempted; all code/table elements are HTML-only |

The pipeline never fails because of rendering (DESIGN.md §13.8 item 5). The reader falls back to rendering the `html` itself.

## Notes

- Which kinds are rendered is decided by the caller, not by this module. `_visual_element_for` renders `code`, `table`, and any `figure` whose `<img>` could not be resolved to an extracted image, which includes caption-only figures. `footnote` and `equation` are never rendered. DESIGN.md §13.9 says only code/tables are rasterized in v1. The unresolved-figure case is an addition in code.
- Output file name, chosen by the caller: `images/<kind>_<first 12 hex of sha256(kind + html + language)>.png`. An existing file is reused without launching a browser.

---
title: Stage 0a — EPUB extraction
description: lnvox ingest-epub converts an EPUB into the novels/<book_id>/ folder of chapter .txt files, images and .epub_meta.json that Stage 0 and Stages 5–6 read.
sidebar:
  order: 1
  label: 0a · ingest-epub
---

`lnvox ingest-epub` converts a publisher or Gutenberg EPUB into the folder layout the rest of the pipeline reads from `novels/<book_id>/`:
- one `NN-<slug>.txt` per chapter (`NNN-` for a book with 100+ chapters);
- every illustration in `images/`;
- a `.epub_meta.json` sidecar with the title, authors, cover, image order and the chapter → XHTML map.

Stage 0 turns the `.txt` files into `00_text.jsonl`. Stage 5 embeds the cover and images from this folder, and Stage 6 uses the chapter map to align audio back to the original EPUB. You can skip it if you already have a folder of `.txt` chapters, but then there is no `.epub_meta.json` and Stage 6 cannot run (DESIGN.md §2.0).

`--mode lecture` adds non-fiction handling: boilerplate pages are dropped, code/tables/figures/footnotes/equations are pulled out of the narrated text, and code and tables are rendered to PNG (DESIGN.md §13.2).

## Command

```bash
uv run lnvox ingest-epub EPUB_PATH OUTPUT_DIR [--mode narration|lecture] \
    [--ingest-classifier fallback|none] [--no-render] [--split-chapters]
```

| Argument / option | Default | Meaning |
|---|---|---|
| `EPUB_PATH` | required | The `.epub` file. It must exist and be a file |
| `OUTPUT_DIR` | required | Destination folder, conventionally `novels/<series>/<volume>`. Created if missing. On a re-run, the chapter `.txt` files the previous extraction listed in its `.epub_meta.json` are removed first; nothing else is deleted |
| `--mode` | `narration` | `narration` (multi-voice novel) or `lecture` (single-voice non-fiction). Any other value exits with code 2 |
| `--ingest-classifier` | `fallback` | Lecture mode only. `fallback` builds an `LLMClient` and asks it only about blocks the rules cannot classify. `none` uses the rules only (ambiguous → prose) and needs no server |
| `--no-render` | off | Lecture mode only. Skip PNG rendering; code/table elements keep their HTML only |
| `--split-chapters` | off | Cut a spine page into several chapters at its chapter markers (`<h1>`–`<h3>`, or Gutenberg `id="Chapter_XI"` anchors). For EPUBs that pack a whole book into a few XHTML files (DESIGN.md §18). Narration mode only: lecture mode ignores it |

Examples:

```bash
# Novel (publisher EPUB, one XHTML per chapter)
uv run lnvox ingest-epub epubs/my-series/volume-01.epub novels/my-series/volume-01

# Gutenberg Ebookmaker edition with several chapters per XHTML
uv run lnvox ingest-epub epubs/oz.epub novels/oz --split-chapters

# Technical book, rules-only classifier (no LLM server needed)
uv run lnvox ingest-epub epubs/my-book.epub novels/my-book --mode lecture --ingest-classifier none
```

The command prints a summary table with authors, publisher, language, cover image, image count, chapter count, and visual element count when there are any.

## In the launcher

`scripts/run_pipeline.sh` **does not run `ingest-epub`**. Run it yourself first. The launcher then expects the extracted folder at `$NOVELS_ROOT/$BOOK_ID` (default `novels/<book_id>`, changed with `--novels-root`). It exits with `ERROR: novel dir not found` if that folder is missing, unless you passed `--skip-llm` or `--mode scenario`.

```bash title="scripts/run_pipeline.sh"
NOVEL_DIR="$NOVELS_ROOT/$BOOK_ID"
# ...
if [ "$MODE" != "scenario" ] && [ ! -d "$NOVEL_DIR" ] && [ "$SKIP_LLM" -eq 0 ]; then
    echo "ERROR: novel dir not found: $NOVEL_DIR" >&2
    exit 1
fi
```

Stage 6 later needs the same EPUB, by default at `epubs/<book_id>.epub` (`--epub` overrides). Using matching paths for both keeps the launcher invocation short:

```bash
uv run lnvox ingest-epub epubs/my-series/volume-01.epub novels/my-series/volume-01
./scripts/run_pipeline.sh my-series/volume-01
```

For lecture mode, pass `--mode lecture` to **both** commands. `ingest-epub --mode lecture` extracts the blocks. The launcher's `--mode lecture` makes Stage 0 write the Narrator stub and routes the run through the lecture stage.

:::caution[Lecture fallback needs a server you start]
With `--mode lecture` and the default `--ingest-classifier fallback`, ingest-epub talks to the LLM at `LNVOX_LLM__ENDPOINT` / `LNVOX_LLM_ENDPOINT` (default `http://localhost:8000/v1`). The launcher's server is not running yet at this point. Start one yourself (for example `scripts/serve_llama.sh`, which listens on port 8000) or pass `--ingest-classifier none`. If no server answers, the run **fails** on the first ambiguous block (a connection error) rather than quietly narrating every such block as prose. A book with clean markup has no ambiguous blocks and never contacts the server.
:::

## Inputs and outputs

| | Path | Notes |
|---|---|---|
| reads | `EPUB_PATH` | `META-INF/container.xml`, the OPF, spine XHTML, images, nav/NCX |
| writes | `OUTPUT_DIR/NN-<slug>.txt` | first line = title, blank line, body (paragraphs separated by blank lines) |
| writes | `OUTPUT_DIR/images/<basename>` | every `image/*` manifest item, plus `code_<hash>.png` / `table_<hash>.png` in lecture mode |
| writes | `OUTPUT_DIR/.epub_meta.json` | title, authors, publisher, language, `cover_image`, `images` (embed order), `chapters[{file, title, source_parts}]`, `visual_elements` |
| deletes | `OUTPUT_DIR/<file>` for each `chapters[].file` of the **previous** `.epub_meta.json` | before writing, so a re-extraction leaves only the new chapter set |

The `.epub_meta.json` format is documented field by field on [`lnvox.ingest.epub`](/library/ingest/epub/#epub_metajson).

## How it works

`cli.ingest_epub` → [`extract_epub`](/library/ingest/epub/#extract_epub) → per page either [`_xhtml_to_text`](/library/ingest/epub/#_xhtml_to_text) / [`_split_page_sections`](/library/ingest/epub/#_split_page_sections) (narration) or [`_xhtml_to_blocks`](/library/ingest/epub/#_xhtml_to_blocks) → [`blocks.page_drop_reason`](/library/ingest/blocks/#page_drop_reason) + [`blocks.classify_page`](/library/ingest/blocks/#classify_page) (lecture) → [`render.render_block`](/library/ingest/render/#render_block) for code/tables.

1. Find the OPF through `container.xml`. Read the Dublin Core metadata, the manifest and the spine.
2. Copy every image into `images/`. Detect the cover (`properties="cover-image"`, EPUB 2 `<meta name="cover">`, or a file stem `cover`). Order `images` as: cover, then images in reading order across **all** spine pages (each `<img src>` resolved against its page's directory), then the leftovers.
3. Walk the spine. Skip stems starting with `cover`, `toc`, `tocimg`, `copyright`, `signup`, `insert<N>`, `bonus<N>` or `color<N>`. Parse each remaining page: in narration, `<h1>`/`<h2>` becomes the title and each `<p>` a paragraph. In lecture mode, pages go through the block classifier and the drop list (keyed on the page's file stem and page-level markup).
4. Group multi-part chapters: `chapter1` + `chapter1_1` + `chapter1_2` → one chapter, and the same for `prologue`, `epilogue`, `afterword`, `interlude<N>`, `intro`, `preface`. With `--split-chapters`, a page holding two or more chapter markers becomes one chapter per marker instead (a marker nested in another, like `<h2><a id="chap01"/>…</h2>`, counts once).
5. Remove the chapter files of the previous extraction, if any. Number the groups `01`, `02`, … in spine order (`001`, … from 100 chapters). If a title is only a numeral (`III`), take the real title from the nav document when it has one. Write `NN-<slug>.txt` and `.epub_meta.json`.

The progress log shows one line per chapter, with the number of source parts:

```text
  01-prologue.txt  (4,812 chars from 1 part(s))
  02-the-lantern-road.txt  (18,204 chars from 2 part(s))
  · drop index: boilerplate / contents page          # lecture mode
Done. 24 chapter(s), 31 image(s) → novels/my-series/volume-01
```

## Re-running and caching

- Deterministic for a given EPUB and flag set. There is no cache apart from lecture PNGs, which are content-hash named and reused if present.
- Re-extracting into the same folder is safe: the chapter files the previous run listed in `.epub_meta.json` are removed first, so switching `--split-chapters` or `--mode` leaves exactly the new set. A `.txt` you added by hand is left alone, and [`lnvox ingest`](/stages/ingest/) will read it as a chapter.
- Changing the chapter split after s1–s4 have run renumbers chapters. Every downstream artifact is then keyed to the wrong text, so re-run the pipeline from Stage 0.

## Failure modes

| Symptom | Where to look |
|---|---|
| `ValueError: Not a valid EPUB (missing META-INF/container.xml)` | Not an EPUB, or a DRM-wrapped file |
| `KeyError: "There is no item named '…' in the archive"` | The manifest lists a file the zip does not contain. Image extraction does not tolerate this; fix the EPUB (e.g. re-save it with Calibre) |
| A chapter is missing | Its file stem matches a skip pattern ([list](/library/ingest/epub/#_skip_stem_patterns--_is_skip)), or its text is not inside `<p>` elements ([`_xhtml_to_text`](/library/ingest/epub/#_xhtml_to_text)), or (lecture) its file stem, a whole-title heading ("Index", "Contents") or a page-level `epub:type` landmark triggered the [drop list](/library/ingest/blocks/#page_drop_reason) |
| Two-part chapter shows up as two chapters | The stems are not in `chapterN_M` form (`chapter_1`, Calibre `…_split_001`). See [`_chapter_base`](/library/ingest/epub/#_chapter_base_re--_chapter_base). Merge the `.txt` files by hand and fix `source_parts` |
| Few enormous chapters | Gutenberg Ebookmaker packing. Re-extract with `--split-chapters` |
| Too many chapters with `--split-chapters` | Sub-headings (`<h3>`) are markers too. See [Debugging a bad split](/library/ingest/epub/#debugging-a-bad-split) |
| First chapter is "The Project Gutenberg eBook of …" | Narration mode keeps Gutenberg boilerplate. Delete that `.txt` before `lnvox ingest` without renumbering the rest |
| Lecture: code/tables have `"src": ""` | `render` extra missing (logged once), or Chromium not downloaded (silent). See [render degradation](/library/ingest/render/#degradation) |
| No cover in the m4b | No image matched the cover rules (`cover_image` is `""`). Pass `lnvox s5 --cover <path>` |

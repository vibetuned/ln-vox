---
title: lnvox.ingest.text
description: The Chapter record and the folder-of-.txt reader/writer behind Stage 0 and every later stage's 00_text.jsonl access.
sidebar:
  order: 1
  label: text
---

`lnvox.ingest.text` defines the `Chapter` record and the three helpers that move chapters between a folder of `.txt` files and `artifacts/<book>/00_text.jsonl`. [`lnvox ingest`](/stages/ingest/) is its only writer from disk. The CLI commands for s1, s2, lecture and s5 read `00_text.jsonl` back through `read_jsonl`. [Scenario ingest](/library/ingest/scenario/) reuses `Chapter` + `write_jsonl` to give s5 one chapter per scene.

Source: `src/lnvox/ingest/text.py`.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`Chapter`](#chapter) | Pydantic model | One chapter: `chapter_id`, `title`, `text` |
| [`ingest_folder`](#ingest_folder) | function | Folder of `NN-<slug>.txt` → `list[Chapter]` |
| [`write_jsonl`](#write_jsonl) | function | `list[Chapter]` → JSONL file (one record per line) |
| [`read_jsonl`](#read_jsonl) | function | JSONL file → `list[Chapter]` |

## API

### `Chapter`

```python
class Chapter(BaseModel):
    chapter_id: str
    title: str
    text: str
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `chapter_id` | `str` | required | Chapter id. For `.txt` ingest this is the filename prefix before the first `-` (`"01"`, `"02"`, …). Every later artifact is keyed on it (`03_directed/<id>.json`, `05_audio/<id>/…`, `--chapters` filters). |
| `title` | `str` | required | Chapter title (first line of the file). Stage 5 uses it for m4b chapter markers. |
| `text` | `str` | required | Chapter body. Paragraphs are separated by blank lines; later stages split on those (`split_paragraphs` in `lnvox.llm.chunker`). |

There is no validation beyond the three string fields. Two chapters can share a `chapter_id`, and nothing checks that the body is non-empty.

### `ingest_folder`

```python
def ingest_folder(folder: Path) -> list[Chapter]
```

Reads every `*.txt` directly inside `folder` (not recursive) and returns one `Chapter` per non-empty file.

For each file, in order:

1. **Ordering.** Files are processed in **numeric** order of their leading digits (`9-…` < `10-…` < `100-…`), ties broken by file name. Files whose name does not start with a digit come after all numbered ones, in name order. See [ordering](#ordering-is-numeric).
2. **Empty files.** The whole file is read as UTF-8 and `.strip()`ped. If nothing is left, the file is skipped with no error and no chapter.
3. **Title / body split.** Everything up to the first `\n` of the stripped text is the title, `.strip()`ped. Because leading blank lines were already stripped, this is the first non-empty line. The rest, also stripped, is the body. A file with a single line becomes a chapter with that title and an empty body.
4. **Chapter id.** `path.stem.split("-", 1)[0]`, i.e. everything before the first hyphen. `07-the-road.txt` → `"07"`. A file with no hyphen uses its whole stem: `epilogue.txt` → `"epilogue"`.

**Parameters**
- `folder`: directory holding the chapter files. Other files (`images/`, `.epub_meta.json`) are ignored.

**Returns** chapters in chapter-number order. The list is empty if no `.txt` file has content.

**Reads** `<folder>/*.txt`.

Expected layout. This is what [`extract_epub`](/library/ingest/epub/#extract_epub) writes:

```text
novels/my-series/volume-01/
    01-prologue.txt
    02-the-lantern-road.txt
    03-a-door-in-the-hill.txt
    images/...
    .epub_meta.json
```

and a chapter file:

```text title="novels/my-series/volume-01/02-the-lantern-road.txt"
The Lantern Road

The lantern swung in the wind and nobody answered.

"Is anyone there?" she called.
```

### `write_jsonl`

```python
def write_jsonl(chapters: list[Chapter], output: Path) -> None
```

Creates `output.parent` if needed, then **overwrites** `output`. Each chapter is written as `Chapter.model_dump_json()` plus a newline. Nothing is appended or merged, so a rewrite drops any chapter that is no longer in `chapters`.

**Writes** `output`. For Stage 0 this is `artifacts/<book_id>/00_text.jsonl`:

```json
{"chapter_id":"01","title":"Prologue","text":"The rain had not stopped for three days.\n\nMara counted the lanterns."}
{"chapter_id":"02","title":"The Lantern Road","text":"The lantern swung in the wind and nobody answered.\n\n\"Is anyone there?\" she called."}
```

### `read_jsonl`

```python
def read_jsonl(path: Path) -> list[Chapter]
```

Reads `path` as UTF-8, skips blank lines, and validates each remaining line with `Chapter.model_validate_json`. A malformed line raises pydantic's `ValidationError`. Records come back in file order, which is the order every stage processes chapters.

## Notes

### Ordering is numeric

`ingest_folder` orders files by the number their name starts with, so `2-…`, `10-…`, `100-…` come out in that order however they are padded:

```text
2-b.txt  10-c.txt  11-e.txt  100-d.txt  extra.txt      # order read
```

The ids are still the literal prefixes (`"2"`, `"10"`, `"100"`), and later stages order chapter ids and per-chapter files **as strings**. Keep the prefixes one fixed width, as `ingest-epub` does: two digits (`01`…`99`), or three from 100 chapters up (`001`…). Pad a hand-made folder before any later stage has run, since renaming changes chapter ids.

:::caution[Every `*.txt` is a chapter]
Stage 0 reads every `*.txt` in the folder. A re-run of `lnvox ingest-epub` removes the chapter files its previous run listed in `.epub_meta.json`, but a `.txt` you add by hand (notes, an old copy) is ingested as a chapter.
:::

- To leave a chapter out of the audiobook (e.g. a Project Gutenberg licence page), delete its `.txt` before `lnvox ingest`, and **do not renumber** the remaining files. Stage 6 maps chapters back to `.epub_meta.json` by the `NN` filename prefix (see [`lnvox.ingest.epub`](/library/ingest/epub/#epub_metajson)).
- DESIGN.md §2.1 lists `.md` (split on H1/H2) as an ingest format. It is not implemented: only `*.txt` is read. Markdown theater scripts go through [`ingest-scenario`](/stages/ingest-scenario/) instead.

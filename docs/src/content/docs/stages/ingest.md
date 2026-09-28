---
title: Stage 0 — ingest
description: lnvox ingest reads a folder of chapter .txt files into artifacts/<book>/00_text.jsonl, copies EPUB metadata, and writes the lecture-mode Narrator stub.
sidebar:
  order: 2
  label: 0 · ingest
---

`lnvox ingest` is the entry point for narration and lecture books. It reads a folder of `NN-<slug>.txt` chapter files (usually produced by [`ingest-epub`](/stages/ingest-epub/)) and writes `artifacts/<book_id>/00_text.jsonl`, one record per chapter. Every later stage reads its chapters from that file. Two side jobs:
- it copies the EPUB metadata sidecar to `00_book_meta.json` so Stage 5 can find the cover;
- in lecture mode, it writes a Narrator-only cast stub so `voice cast` runs without Stage 1.

Scenario mode does not use this command; see [`ingest-scenario`](/stages/ingest-scenario/).

## Command

```bash
uv run lnvox ingest FOLDER [--book-id <id>] [--mode narration|lecture]
```

| Argument / option | Default | Meaning |
|---|---|---|
| `FOLDER` | required | Folder of `*.txt` chapter files. It must exist and be a directory |
| `--book-id` | `FOLDER`'s path under `./novels`, else its name | Artifact id, so output goes to `artifacts/<book_id>/`. `novels/my-series/volume-01` defaults to `my-series/volume-01`; a folder outside `./novels` defaults to its last path component |
| `--mode` | `narration` | `narration` or `lecture`. In lecture mode a Narrator-only `01_characters.json` stub is written. The value is not validated: anything other than `lecture` behaves like `narration` |

:::note[Default book id]
Series continuity (prior-volume casting) depends on the `<series>/<volume>` id, so the default keeps the folder's path relative to `./novels` (resolved against the current directory): `lnvox ingest novels/my-series/volume-01` writes to `artifacts/my-series/volume-01/`. For a folder anywhere else, the default is just the folder name, so pass `--book-id` there. The launcher always passes it.
:::

The command exits with code 1 and `No .txt files found in <folder>` when no file yields a chapter. On success it prints a table of `id`, `title` and character count per chapter.

## In the launcher

```bash title="scripts/run_pipeline.sh"
banner "Stage 0: ingest (mode=$MODE)"
run_step - "Stage 0 (ingest)" uv run lnvox ingest "$NOVEL_DIR" --book-id "$BOOK_ID" --mode "$MODE"
```

- `NOVEL_DIR` is `$NOVELS_ROOT/$BOOK_ID`: `novels/<book_id>` by default, changed with `--novels-root`.
- It runs for `--mode narration` and `--mode lecture`. In `--mode scenario` the launcher runs `ingest-scenario` in its place.
- It runs **inside the LLM phase** (after the server is started, or after the server step is skipped for a lecture run with `--no-normalize` and `--narrator-clip`). It needs no LLM itself. `--skip-llm` skips the whole phase, Stage 0 included, so the existing `00_text.jsonl` is reused.
- `run_step -` has no pre-retry hook: the step is attempted up to `--step-retries` times in total (default 3, `RETRY_DELAY` 5 s apart) without touching the LLM server.
- The launcher never runs `ingest-epub`. The novels folder must already exist.

## Inputs and outputs

| | Path | Notes |
|---|---|---|
| reads | `FOLDER/*.txt` | numeric order of the filename prefix; first line = title |
| reads | `FOLDER/.epub_meta.json` | optional, written by `ingest-epub` |
| writes | `artifacts/<book_id>/00_text.jsonl` | overwritten on every run |
| writes | `artifacts/<book_id>/00_book_meta.json` | only when `.epub_meta.json` exists |
| writes | `artifacts/<book_id>/01_characters.json` | lecture mode only, and only if the file does not exist yet |

`00_text.jsonl`, one [`Chapter`](/library/ingest/text/#chapter) per line:

```json title="artifacts/my-series/volume-01/00_text.jsonl"
{"chapter_id":"01","title":"Prologue","text":"The rain had not stopped for three days.\n\nMara counted the lanterns."}
{"chapter_id":"02","title":"The Lantern Road","text":"The lantern swung in the wind and nobody answered."}
```

`00_book_meta.json` is the `.epub_meta.json` content unchanged, except that `cover_image` is resolved to an **absolute path** (`(FOLDER / cover_image).resolve()`), so Stage 5 does not need to know the source folder:

```json title="artifacts/my-series/volume-01/00_book_meta.json"
{
  "title": "The Lantern Road",
  "authors": ["A. N. Author"],
  "publisher": "Example Press",
  "language": "en",
  "cover_image": "/home/me/ln-vox/novels/my-series/volume-01/images/cover.jpg",
  "images": ["images/cover.jpg", "images/insert1.jpg"],
  "chapters": [{"file": "01-prologue.txt", "title": "Prologue", "source_parts": ["prologue"]}],
  "visual_elements": []
}
```

Only `cover_image` is read from this copy (by `s5_mix.resolve_book_images`). `images` keep their relative paths, and Stages 5 and 6 read those from the novels folder's `.epub_meta.json` directly. Moving the repo checkout makes the absolute cover path stale; s5 then logs `Cover image referenced by 00_book_meta.json is missing … skipping`. Re-run Stage 0 to fix it.

Lecture-mode cast stub:

```json title="artifacts/my-book/01_characters.json"
{"characters": []}
```

The empty list is intentional. `voice cast` synthesises the Narrator itself: `cast_book()` adds a `Narrator` character whenever the cast has none. DESIGN.md §13.2 describes the stub as "a single `Character` named `Narrator`". The code writes an empty list, with the same effect.

## How it works

`cli.ingest` → [`ingest_folder`](/library/ingest/text/#ingest_folder) → [`write_jsonl`](/library/ingest/text/#write_jsonl).

1. `book_id = --book-id`, else `FOLDER` relative to `./novels` (as a POSIX path) when it is inside it, else `FOLDER.name`.
2. `ingest_folder(FOLDER)`: every `*.txt`, ordered by the number its name starts with (`9-…` < `10-…` < `100-…`). Empty files are skipped. The title is the first line, the body is the rest, and `chapter_id` is the filename before the first `-`.
3. Write `00_text.jsonl`, replacing any previous one.
4. Lecture mode: write `{"characters": []}` to `01_characters.json` **unless it already exists**, so a real cast is never overwritten. This logs `Lecture mode: wrote Narrator-only cast stub → …`.
5. If `FOLDER/.epub_meta.json` exists, copy it to `00_book_meta.json` with the absolute cover path, and log `Propagated EPUB metadata to …`.

## Re-running and caching

- Cheap and idempotent: no LLM, no cache. `00_text.jsonl` and `00_book_meta.json` are rewritten from the folder each time.
- **Editing chapter text** and re-running Stage 0 is how you fix a typo at the source. Later stages cache per chapter, so re-run the affected ones (e.g. `lnvox s2 <book> --chapters 03`, then s3, s4) and check each stage page for how it detects stale output.
- **Adding, removing or renumbering files** changes the chapter ids that every later artifact is keyed on. Deleting one `.txt` without renumbering keeps the other ids stable.
- Stale outputs are not cleaned. A `00_book_meta.json` from an earlier run survives if `.epub_meta.json` has since been deleted. Switching an existing book from narration to lecture leaves its Stage 1 `01_characters.json` in place, because the stub is only written when the file is missing.

## Failure modes

| Symptom | Cause |
|---|---|
| `No .txt files found in <folder>` (exit 1) | The folder has no `.txt` with content. Run `ingest-epub` first, or check `--novels-root` |
| Artifacts under `artifacts/volume-01/` instead of `artifacts/my-series/volume-01/` | `--book-id` omitted for a folder outside `./novels` (or run from another directory) |
| Duplicate chapter ids / chapters twice | Extra `*.txt` files in the folder that `ingest-epub` did not write (hand-made copies); `ingest-epub` itself removes its previous run's files |
| Later stages list chapters out of order | Mixed-width prefixes in a hand-made folder (`9-…`, `10-…`): Stage 0 reads them in numeric order, but later stages sort ids as strings. See [ordering](/library/ingest/text/#ordering-is-numeric) |
| Lecture book still casts many characters | A pre-existing `01_characters.json` from Stage 1 was kept. Delete it and re-run Stage 0 |

---
title: Utilities — sample and audio purge
description: lnvox sample cuts a small committable ln-reader demo from a finished book; lnvox audio purge deletes one speaker's renders and cache entries before a re-render.
sidebar:
  order: 16
  label: utilities
---

These two commands are not pipeline stages, and the launcher never calls them. `lnvox sample` packages a finished, synced book into a demo small enough to commit. `lnvox audio purge` clears one speaker's rendered audio so the next `lnvox s4` renders it again, for the cases where the cache would otherwise hand back the old take.

## `lnvox sample`

Cuts a window of about 12 minutes that **straddles a chapter boundary**, so every ln-reader feature can be tried from a file of a few MB:

- text-highlight auto-follow;
- chapter-relative scrubbing (two chapters);
- the chapter transition (the boundary sits well inside the window);
- the image viewer (the cover plus the pictures that fall in the window).

The audio is a `-c copy` slice, so it is bit-identical to the real render. The EPUB ships whole (DESIGN.md §22).

### Command

```bash
uv run lnvox sample <book_id> [--out DIR] [--chapters A,B] [--minutes N] [--title T] [--novels-root DIR]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | A book that has been through s5 **and** s6. |
| `--out` | `samples/<book_id with / → _>` | Output directory. |
| `--chapters` | best-scoring pair | Exactly two chapter ids, for example `14,15`. The window straddles the start of the second. Any other count exits with code 2. |
| `--minutes` | `12.0` | Sample length. The window slides to cover pictures and keeps the boundary at least 25% from either edge. It is not rigidly centred. |
| `--title` | `book_id` | Title metadata of the sample m4b and the README heading. If `06_final/<title>.m4b` exists, that mix is the one sliced. |
| `--novels-root` | `novels` | Where `<book_id>/.epub_meta.json` lives. It provides the chapter titles. |

With no `--chapters`, every consecutive chapter pair is scored on the window it would actually produce:

1. Keep the pairs whose window holds at least 3 pictures. If there are none, keep those with at least 1. If there are still none, keep all.
2. Within that tier, rank by the number of distinct speakers across the two chapters.

The pick is logged, for example `Auto-picked chapters 11+12: 14 speakers, 5 image(s) in the window`. The scoring is described in detail in [`score_pairs`](/library/stages/sample/#score_pairs).

### Inputs and outputs

**Reads**

- `artifacts/<book>/07_sync/sync_manifest.json` and `07_sync/*.epub`, from [Stage 6](/stages/s6-sync/);
- one m4b from `artifacts/<book>/06_final/`, from [Stage 5](/stages/s5-mix/). It is the one named by `--title` if that exists, else the one `sync_manifest.json` was timed against (its `m4b` key), else the newest mix. A pick other than the manifest's own m4b prints a `[WARN]`;
- `novels/<book>/.epub_meta.json`.

**Writes**, into `--out`, where `<slug>` is the last path segment of the book id, for example `volume-01`:

| File | Content |
|---|---|
| `<slug>-sample.m4b` | The audio window, the cover and in-window pictures, and chapter markers clipped and rebased to 0. |
| `<slug>-sample.epub` | The complete synced EPUB. |
| `sync_manifest.json` | Only beats inside the window that have a span in the EPUB, plus the visual elements inside the window, all rebased to 0. `images[]` mirrors the sample's embedded pictures, cover first. It adds a `sample` block `{window_start_seconds, window_end_seconds, of_book}`. |
| `README.md` | A feature checklist, a file table, and the chapters present with their start times. |

The command finishes with a summary table: chapters, duration, audio size, beats, embedded pictures (`N (cover + N-1)`) and visual elements.

```bash
uv run lnvox sample oz/volume-01 --minutes 10
uv run lnvox sample level99/volume-01 --chapters 04,05 --title "Level 99 — sample"
```

### Failure modes

| Symptom | Cause |
|---|---|
| `No .epub_meta.json under …` (exit 1) | The book was not imported with `ingest-epub`, or `--novels-root` is wrong. |
| `No …/sync_manifest.json. Run s6 first — a sample needs the sync layer.` (exit 1) | Stage 6 has not run. Scenario plays and VibeVoice books have no Stage-6 manifest and cannot be sampled. |
| `Need both …/06_final/*.m4b and …/07_sync/*.epub.` (exit 1) | Stage 5 or Stage 6 output is missing. |
| `Book has fewer than two chapters with beats.` (exit 1) | A one-chapter book cannot straddle a boundary. |
| A `KeyError` (exit 1) | A `--chapters` id is not among the chapters with matched beats. |
| `[WARN] slicing X.m4b, but sync_manifest.json was timed against Y.m4b …` | `--title` named a different mix than the one Stage 6 synced against, or that m4b was deleted. Re-run s6 against the mix you want (`lnvox s6 --title …`), or drop `--title`. |

## `lnvox audio purge`

Deletes the rendered WAVs **and** the TTS cache entries of every beat spoken by one speaker, so the next `lnvox s4` run renders them from scratch. With `--v2` it does the same for the VibeVoice sessions the speaker appears in.

### Command

```bash
uv run lnvox audio purge <book_id> --speaker NAME [--cache-dir DIR] [--yes] [--v2]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | Book id. |
| `--speaker` | required | The exact speaker name as it appears in the manifests (case-sensitive), for example `Narrator` or `Kamijou Touma`. |
| `--cache-dir` | `cache/tts` | The TTS cache to clean as well. |
| `--yes`, `-y` | off | Skip the confirmation prompt. |
| `--v2` | off | Purge VibeVoice sessions in `05_audio_v2/` instead of Dramabox beats in `05_audio/`. Mirrors `lnvox s5 --v2`. |

### What it deletes

The command scans every `artifacts/<book>/05_audio/<chapter>/manifest.json`. For each beat whose `speaker` equals `--speaker` exactly, it deletes two files:

- the rendered WAV, `05_audio/<chapter>/<beat_id>.wav`;
- the cache entry, `<cache-dir>/<cache_key>.wav`.

With `--v2` it scans `05_audio_v2/<chapter>/manifest.json` instead. A VibeVoice manifest has one entry per multi-speaker **session**, and its `speaker` lists every voice in it as `A + B + C`. Every session whose list contains `--speaker` is purged: its WAV `05_audio_v2/<chapter>/<session_id>.wav` and its cache entry. The other speakers' lines in those sessions are re-rendered along with it, because a session is rendered as one piece.

It prints the counts and asks for confirmation:

```text
About to delete:
  - 342 rendered beat WAV(s) under artifacts/level99/volume-01/05_audio
  - 342 cache entries under cache/tts
Proceed? [y/N]:
```

With `--v2` the first line counts `session WAV(s)`, and a note says the other speakers' lines are re-rendered too. Answering anything other than yes prints `Aborted.` and exits 1. `--yes` skips the prompt. When nothing matches, it prints `No beats found for speaker '…'` (or `No sessions found …`) and exits 0. If the chosen directory is missing, it exits 1, and suggests adding or dropping `--v2` when the other directory exists.

Some things are left alone:

- the `manifest.json` files are **not** edited;
- without `--v2`, `05_audio_v2/` is not touched, and with it, `05_audio/` is not;
- nothing under `05_audio/_staged/` is touched. A successful staged run has already removed each render's chunk intermediates, so a purged beat is denoised afresh, not re-decoded from old latents.

### When to purge

The Dramabox cache key is `sha256(prompt, reference-clip file name, model version)`, truncated to 16 hex characters. The VibeVoice key is the same over the session script and the file names of all its reference clips. That decides whether a purge is needed:

- **Recasting a speaker to a different voicebank clip does not need a purge.** The clip file name is part of the key, so `lnvox s4` misses the cache and re-renders that speaker's beats anyway. Purging only removes the stale WAVs and cache entries, which never get reused, and frees their disk space.
- **Purge when the key will not change but you want new audio.** Examples: you replaced or re-trimmed a reference clip **in place** under the same file name, or you want a fresh take of a speaker whose renders came out badly, with the same assignment and prompts. Without a purge, s4 would copy the old cached WAVs straight back.

After purging, re-run `lnvox s4 <book_id>` (with `--tts-backend vibevoice` after a `--v2` purge) or the launcher with `--skip-llm`, then [`lnvox s5`](/stages/s5-mix/) and the sync stage ([`lnvox s6`](/stages/s6-sync/) or [scenario-sync](/stages/scenario-sync/)), because the durations change.

:::caution
Until s4 has re-rendered, `lnvox s5` fails with `Beat WAV not found` for every purged beat. The cache is content-addressed and shared across books (`cache/tts/` survives book deletions), so a purged entry that another book used identically is gone for that book too. It will simply be re-rendered there as well.
:::

For changing a character's voice assignment itself, see the [casting guide](/guides/casting/) and [voice cast](/stages/voice-cast/).

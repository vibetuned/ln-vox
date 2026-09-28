---
title: Concepts
description: Book ids and series, the artifacts directory, idempotent stages and their caches, the LLM ↔ TTS hand-off, and the venv swap behind it.
sidebar:
  order: 4
---

A few ideas explain almost every behaviour of the pipeline.

## Book ids and series

A book id is a path: `<series>/<volume-NN>` (e.g. `toaru/volume-02`) or a flat id (`standalone-book`, `myplay`). It names three places:

| Place | Contents |
| --- | --- |
| `novels/<book_id>/` | Input chapters (`NN-<slug>.txt`, `images/`, `.epub_meta.json`) — `--novels-root` changes the root |
| `epubs/<book_id>.epub` | The source EPUB Stage 6 re-aligns onto — `--epub` overrides |
| `artifacts/<book_id>/` | Everything the pipeline writes — `LNVOX_ARTIFACTS_DIR` changes the root for the `lnvox` CLI |

A nested id makes the book part of a **series**: [`find_prior_volumes`](/library/series/) returns the sibling directories under `artifacts/<series>/` with the same name prefix and a smaller trailing number, in numeric order (`volume-9` before `volume-10`; stray folders are ignored). Voice cast reuses their clip assignments by name and alias, so recurring characters and the narrator keep their voices ([series guide](/guides/series/)).

## Artifacts: one directory, one file per stage

Every stage reads the previous stages' JSON from `artifacts/<book_id>/` and writes its own, numbered in pipeline order:

```text
00_text.jsonl  01_characters.json  02_scenes/  04_voice_assignments.json  03_voice_profiles.json
03_directed/   05_audio/<chapter>/  06_final/<title>.m4b  07_sync/
```

(The numbers predate the final stage order: voice cast, `04_…`, runs *before* the director, `03_…`, so the director can describe the voice actually assigned.) Everything is pretty-printed JSON meant to be read and, where useful, hand-edited. The full map, with producers, consumers and schemas, is [Artifacts](/reference/artifacts/).

## Stages are idempotent and cached

Each stage is a standalone `lnvox` command that skips work already on disk:

- **LLM stages** cache per chapter (s1's `01_characters_per_chapter/`, s2's and s3's per-chapter files), so a crash resumes at the next unfinished chapter. s1 and s2 key their caches by chapter id, not content: to redo a chapter after changing a prompt or the text, delete its file. s3, lecture and scenario direction also record the casting and settings each chapter was written with, so a recast or a changed flag refreshes just the affected chapters — a recast re-voices them without new LLM calls.
- **TTS** is content-addressed: `cache/tts/<key>.wav`, keyed by the beat's prompt, the reference clip and the model/recipe version. Edit one line and only that beat re-renders; switch a character's clip and only their beats re-render. The cache lives outside `artifacts/`, so it survives deleting a book.
- **Scenario ingest** caches LLM structuring under `cache/scenario/`, keyed on script text + prompt + model.

That's why the launcher is safe to re-run with the same arguments after any failure, and why "what to delete to redo stage X" has a precise answer ([Artifacts](/reference/artifacts/)).

## Two GPU phases and the hand-off

Gemma 4 (LLM stages) and Dramabox (TTS) each want most of the GPU, so they never run together:

```text
prepare_llm_env → start LLM server → ingest, s1, s2, voice cast, audit, s3 → stop server
                                                        ‖  sleep 5 s (VRAM release)
prepare_tts_env → s4 (Dramabox) → s5 (ffmpeg, CPU) → s6 (CPU)
```

The LLM server is any OpenAI-compatible endpoint — llama.cpp's `llama-server` (default, GGUF, any OS), vLLM (Linux + CUDA) or `mlx_lm.server` (Apple Silicon) — and [`LLMClient`](/library/llm/client/) talks to all three the same way, validating every structured response against a pydantic schema and retrying with the validation error on failure.

## One venv, swapped between phases

vLLM needs torch ≥ 2.11; Dramabox pins torch 2.8 (on x86_64) and needs kernel wheels built against it; VibeVoice pins an older transformers than Dramabox's Gemma encoder accepts. Rather than maintaining several venvs, the launcher re-shapes the one `.venv` before each phase: `uv sync` with the right extras for the LLM phase, then Dramabox's requirements overlaid for TTS. `uv sync` is exact, so each LLM phase strips the Dramabox overlay and each TTS phase puts it back — cheap when nothing changed. The details are in [launcher functions](/launcher/functions/#prepare_llm_env); `./scripts/run_pipeline.sh --prepare-env llm|tts|vibevoice` runs just that step.

## Beats, directions and prompts

The unit of performance is a **beat**: one narration passage or one line of dialogue with a speaker. s2 produces beats with `text` (what is spoken) and `source_span` (the verbatim source slice, the sync anchor). s3 adds a `direction` (voice descriptor matched to the assigned clip + performance cue) and composes the `prompt` Dramabox performs. s4 renders each beat to `05_audio/<chapter>/<beat_id>.wav`; s5 concatenates them with silences (0.25 s between beats, 1 s between scenes, 2 s between chapters by default); s5 records that layout — pads, lead-in, the chapters it mixed, the pictures it embedded — in `06_final/<title>.timings.json`, and s6 (like scenario-sync and `lnvox sample`) reads it to compute when each beat starts in that `.m4b`, so the two can't drift apart.

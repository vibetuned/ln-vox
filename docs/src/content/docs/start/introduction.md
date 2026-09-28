---
title: Introduction
description: What ln-vox is, what it produces, the three modes, and what hardware it expects.
sidebar:
  order: 1
---

ln-vox turns a text book into an acted, multi-voice audiobook on a single workstation, with no cloud service involved. It is the "creator" half of the Lectures and Narrations project; the other half, **ln-reader**, is an Android player that uses ln-vox's sync output to highlight the text as the audio plays.

## What you get

For each book, under `artifacts/<book_id>/`:

- **`06_final/<title>.m4b`** — the audiobook: AAC, loudness-normalized to −18 LUFS, one chapter marker per chapter (per scene for plays), cover and illustrations embedded.
- **`07_sync/`** — the text-sync layer. For EPUB books, a copy of the EPUB with every spoken beat wrapped in `<span class="lnvox-beat" data-beat-id="…">` plus `sync_manifest.json` mapping beats and illustrations to audio time. For plays, `play.json` and `play.srt` — every line, staging pause and sound/light cue on one timeline.
- Every intermediate as readable JSON: the cast, the scenes, the voice casting, the per-line directions — so you can inspect, hand-edit and re-run from any point.

## How it works

The pipeline has two GPU phases that can't share the card:

1. **LLM phase** — Gemma 4, served locally by llama.cpp (default), vLLM or mlx-lm, reads the book: it extracts the characters (s1), segments scenes and attributes every line to a speaker (s2), casts each character to a reference clip from a voicebank (V), and writes a voice + performance direction for every line (s3).
2. **TTS phase** — Dramabox (default) or VibeVoice clones each assigned reference voice and performs each directed line (s4).

Then ffmpeg mixes the beats into the `.m4b` (s5) and the sync stage re-aligns the beats onto the source EPUB (s6). The [launcher](/launcher/run-pipeline/) runs all of it and handles the GPU hand-off; the [stages overview](/stages/overview/) shows every artifact that flows between the stages.

## Three modes

| Mode | For | What changes |
| --- | --- | --- |
| **narration** (default) | Novels | Full cast, scene segmentation, acted delivery. [Guide](/guides/narration/) |
| **lecture** | Non-fiction, technical books | One narrator reading verbatim (speech-normalized: "1973" → "nineteen seventy-three"); code, tables and figures are rendered to images and shown at the right moment instead of read. [Guide](/guides/lecture/) |
| **scenario** | Theater scripts | Lines kept verbatim, per-line emotion and cue, full-cast audio, and a timed sync file as the main deliverable. [Guide](/guides/scenario/) |

## Hardware

- **Linux + NVIDIA** is the primary target: one consumer GPU, 24 GB+ VRAM recommended (the default render recipe loads a ~23 GB bf16 prompt encoder on its own, then the DiT).
- **Apple Silicon** runs the whole pipeline on MPS — validated on an M4 Max / 64 GB; 36 GB+ unified memory recommended, and expect roughly 4 h of rendering per hour of audio. See [Apple Silicon](/guides/apple-silicon/).
- Disk: the Common Voice tarball used to seed the English voicebank is ~96 GB; rendered WAVs take ~600 MB per hour of audio.

## Where the code lives

| Path | What |
| --- | --- |
| `src/lnvox/` | The Python package and the `lnvox` CLI — see the [library overview](/library/overview/) |
| `scripts/run_pipeline.sh` | The one-shot launcher |
| `scripts/serve_*.sh`, `scripts/setup_*.sh`, `scripts/s4_retry.sh` | [Helper scripts](/launcher/helper-scripts/) |
| `scripts/voicebank_studio.py` | [Voicebank Studio](/guides/voicebank-studio/), a PySide6 GUI for curating and casting by ear |
| `comfyui/` | [ComfyUI nodes](/guides/comfyui/) that expose every stage as a graph node |
| `DESIGN.md` | The design document these pages reference as "DESIGN.md §N" |

:::caution[Personal use]
Common Voice clips are CC0, so a seeded voicebank is fine to share. Cloning identifiable real people is for personal use only — never distribute audiobooks rendered from references of living people without their consent. Dramabox also watermarks its output.
:::

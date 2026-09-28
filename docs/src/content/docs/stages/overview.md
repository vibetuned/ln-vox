---
title: Pipeline stages
description: Every stage in order for each mode — command, inputs, outputs and the GPU phase it belongs to — with links to the detailed stage pages.
sidebar:
  order: 0
  label: Overview
---

Each stage is one `lnvox` command that reads the previous stages' files from `artifacts/<book_id>/` and writes its own. The [launcher](/launcher/run-pipeline/) runs them in this order; you can also run any of them by hand, or as [ComfyUI nodes](/guides/comfyui/).

## Narration (novels)

| # | Stage | Command | Reads | Writes | Phase |
| --- | --- | --- | --- | --- | --- |
| 0a | [EPUB extraction](/stages/ingest-epub/) | `lnvox ingest-epub <epub> novels/<book>` | `epubs/<book>.epub` | `novels/<book>/*.txt`, `images/`, `.epub_meta.json` | CPU, run by you |
| 0 | [Ingest](/stages/ingest/) | `lnvox ingest novels/<book> --book-id <book>` | chapter `.txt` | `00_text.jsonl`, `00_book_meta.json` | CPU |
| 1 | [Characters](/stages/s1-characters/) | `lnvox s1 <book>` | `00_text.jsonl` | `01_characters.json` (+ per-chapter cache, merge log) | LLM |
| 2 | [Scenes](/stages/s2-scenes/) | `lnvox s2 <book>` | text + cast | `02_scenes/<ch>.json` | LLM |
| V | [Voice cast](/stages/voice-cast/) | `lnvox voice cast <book>` | cast + voicebank (+ prior volumes) | `04_voice_assignments.json` | LLM |
| A | [Casting audit](/stages/voice-audit/) | `lnvox voice audit <book> [--fix]` | casting + scenes + clip acoustics | report (and a repaired casting) | CPU |
| 3 | [Director](/stages/s3-director/) | `lnvox s3 <book>` | scenes + cast + casting | `03_voice_profiles.json`, `03_directed/<ch>.json` | LLM |
| 4 | [TTS](/stages/s4-tts/) | `lnvox s4 <book> --staged` | directed beats + casting + clips | `05_audio/<ch>/<beat>.wav` + `manifest.json` | GPU (TTS) |
| 5 | [Mix](/stages/s5-mix/) | `lnvox s5 <book> --title "…"` | beat WAVs + manifests | `06_final/<title>.m4b` + `.timings.json` | CPU (ffmpeg) |
| 6 | [Sync](/stages/s6-sync/) | `lnvox s6 <book>` | directed beats + manifests + EPUB | `07_sync/<book>.epub`, `sync_manifest.json`, `unmatched.json` | CPU |

## Lecture (non-fiction)

s1, s2 and s3 are replaced by one stage; everything else is shared.

| # | Stage | Command | Notes |
| --- | --- | --- | --- |
| 0a | [EPUB extraction](/stages/ingest-epub/) | `lnvox ingest-epub … --mode lecture` | classifies blocks, renders code/tables to PNG |
| 0 | [Ingest](/stages/ingest/) | `lnvox ingest … --mode lecture` | also writes an empty cast stub |
| V | [Voice cast](/stages/voice-cast/) | `lnvox voice cast <book> --narrator-clip …` | Narrator only |
| L | [Lecture](/stages/lecture/) | `lnvox lecture <book> [--no-normalize]` | split + speech-normalize → `03_directed/` |
| 4–6 | as narration | | s6 also emits `visual_elements` |

## Scenario (theater scripts)

| # | Stage | Command | Writes |
| --- | --- | --- | --- |
| 0 | [Ingest scenario](/stages/ingest-scenario/) | `lnvox ingest-scenario <md> --id <id>` | `00_script.json`, `00_text.jsonl`, `01_characters.json` |
| V | [Voice cast](/stages/voice-cast/) | `lnvox voice cast <id>` | `04_voice_assignments.json` |
| S | [Scenario direction](/stages/scenario/) | `lnvox scenario <id>` | `03_directed/` (one chapter per scene) |
| 4–5 | TTS, mix | `lnvox s4`, `lnvox s5 --inter-chapter 0.75 --lead-in 0.35` | audio, `.m4b` |
| Y | [Scenario sync](/stages/scenario-sync/) | `lnvox scenario-sync <id>` | `07_sync/play.json`, `<scene>.json`, `play.srt` |

## Outside the per-book flow

- [Building the voicebank](/stages/voicebank/) — `lnvox voice seed-cv`, `lnvox voice list`: once per language.
- [Utilities](/stages/utilities/) — `lnvox sample` (cut a small, committable demo from a finished book) and `lnvox audio purge` (delete one speaker's renders and cache entries).

## Dependencies between stages

```text
00_text ─► 01_characters ─► 02_scenes ─┬─► 04_voice_assignments ─┐
                                       └──────────────────────────┴─► 03_directed ─► 05_audio ─► 06_final
                                                                                        │
                                              epubs/<book>.epub ────────────────────────┴─► 07_sync
```

Change something upstream and everything to its right is stale. Because each stage caches its own work, re-running from that point redoes only what depends on the change — see [Artifacts](/reference/artifacts/) for exactly what to delete to force each stage.

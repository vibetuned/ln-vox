---
title: Narration mode (novels)
description: Produce a multi-voice, acted audiobook from a novel — preparing the input, choosing models, running, checking quality and re-running parts.
sidebar:
  order: 1
---

Narration is the default mode: a full cast, scene-aware delivery, one voice per character plus a narrator. The stages are ingest → [s1](/stages/s1-characters/) → [s2](/stages/s2-scenes/) → [voice cast](/stages/voice-cast/) → [audit](/stages/voice-audit/) → [s3](/stages/s3-director/) → [s4](/stages/s4-tts/) → [s5](/stages/s5-mix/) → [s6](/stages/s6-sync/).

## Prepare the input

- **EPUB** (recommended — it also gives you cover art, illustrations and the text-sync layer): keep the original at `epubs/<series>/<volume>.epub` and extract with [`lnvox ingest-epub`](/stages/ingest-epub/). Front matter, TOC, copyright and image-only pages are dropped; multi-part chapters (`chapter1.xhtml`, `chapter1_1.xhtml`) are merged.
- **Plain text**: `novels/<series>/<volume>/NN-title.txt`, one file per chapter, first line = title, paragraphs separated by blank lines.

Check the chapter split before spending GPU hours: `ls novels/<book>/` and open a couple of files. A book packed into a few XHTML files needs `--split-chapters`.

## Choose the models

| Knob | Default | When to change |
| --- | --- | --- |
| `--llm-backend` | `llama` (Gemma 4 12B QAT GGUF, 64k context) | `vllm` on a CUDA box with vLLM installed; `mlx` on a Mac |
| `--llm-model` | per backend | A bigger model (e.g. a 31B) for better casts and speaker attribution on hard books — much slower; watch the context length ([environment](/reference/environment/)) |
| `--tts-backend` | `dramabox`, staged | `vibevoice` for multi-speaker *session* rendering (smoother dialogue flow, no per-beat sync — s6 is skipped) |
| Render recipe | bf16 encoder, 22 s chunks, fp32 vocoder | Smaller GPU or disk: see [render quality](/guides/render-quality/) |

## Run

```bash
./scripts/run_pipeline.sh <series>/volume-01 \
    --narrator-clip cv_… --book-title "Title — Volume 1"
```

For a first book, do the [hand-casting checkpoint](/guides/casting/) (`--stop-after-audit`) before the full run: a casting change is free before s3, costs a re-direction after s3 and a re-render after s4.

## Check quality early

Listening beats reading, but the JSON tells you where to listen:

- **The cast** — `01_characters.json`: fused or duplicated characters are the root of most wrong-voice problems. See [debugging a bad cast](/stages/s1-characters/).
- **Speaker attribution** — `02_scenes/<chapter>.json`: dialogue beats with the wrong `speaker` will be voiced by the wrong actor. Fix the JSON and re-run from s3.
- **Directions** — `03_directed/<chapter>.json`: each beat's `prompt` is exactly what Dramabox performs.
- **A few beats** — render a slice with `lnvox s4 <book> --staged --limit 40` and listen to `05_audio/<chapter>/*.wav` (or use the ComfyUI [Load Audio](/guides/comfyui/) node).

## Re-running parts

| You changed… | Re-run |
| --- | --- |
| A speaker in `02_scenes/` | delete that chapter's `03_directed/<ch>.json`, then re-run the launcher without `--skip-llm`: s1/s2 and the other chapters are cached, s3 redoes that one, s4 re-renders only the changed beats |
| A character's clip | lock the new clip and re-run the launcher: s3 regenerates that character's profile and re-voices their chapters, s4 re-renders only their beats ([casting](/guides/casting/)) |
| A `prompt` in `03_directed/` | s4 → s5 → s6: `./scripts/run_pipeline.sh <book> --skip-llm` |
| Silences or loudness | only s5, then s6 — s6 follows the new mix's timings sidecar on its own |

The long form of each dependency is on [Artifacts](/reference/artifacts/).

:::note[Beat length]
Dramabox sounds best on short beats. s3's merge pass caps a merged beat at 375 characters (~30 s, `MAX_MERGED_BEAT_CHARS` in `s3_director.py`) and splits longer narration paragraphs at sentence boundaries; the staged renderer further cuts each beat into ≤ 22 s denoise chunks.
:::

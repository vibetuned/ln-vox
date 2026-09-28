---
title: Stage 5 — mix
description: Mix rendered beats into a chaptered, loudness-normalised .m4b with embedded pictures, and record the layout s6, scenario-sync and sample read back.
sidebar:
  order: 12
  label: 5 · mix
---

Stage 5 turns the per-beat WAVs from Stage 4 into the deliverable: one `.m4b` audiobook with chapter markers, the cover and illustrations embedded, and loudness normalised to −18 LUFS. The same mix serves every mode: narration, lecture, scenario, and VibeVoice session renders with `--v2`. It is also where the timeline layout gets fixed. Both sync emitters must reproduce that layout exactly, so s5 records it in a `.timings.json` sidecar that they read back.

## Command

```bash
uv run lnvox s5 <book_id> [options]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | Book id, for example `level99/volume-01`. Artifacts come from `artifacts/<book_id>/`. |
| `--chapters` | all | Comma-separated chapter ids to include, matched against `05_audio/<chapter>/` directory names. |
| `--title` | `book_id` | Title metadata of the m4b, and its filename (`/` and `\` become `_`). |
| `--intra` | `0.25` | Silence in seconds between consecutive beats of one scene. |
| `--inter-scene` | `1.0` | Silence between beats whose `scene_id` differs. In scenario mode this is the staging pause. |
| `--inter-chapter` | `2.0` | Silence between chapters. The launcher passes `0.75` in scenario mode. |
| `--lead-in` | `0.0` | Extra silence before **every** beat. The launcher passes `0.35` in scenario mode. Books keep `0`. |
| `--lufs` | `-18.0` | Target integrated loudness. |
| `--kbps` | `96` | AAC bitrate. |
| `--cover` | auto | Cover image to embed. Auto-detected from `cover_image` in `00_book_meta.json`. |
| `--images-dir` | auto | Directory of extra images. Auto-detected as `<novels-root>/<book>/images/`. Each image becomes an `attached_pic` stream with `title=<stem>`. |
| `--novels-root` | `novels` | Where `<book>/images/` and `<book>/.epub_meta.json` live. |
| `--v2` | off | Mix from `05_audio_v2/` (VibeVoice session renders, DESIGN.md §16) instead of `05_audio/`. |
| `--keep-work` | off | Keep `06_final/_work/` after a successful mix, for debugging. It is always kept when the mix fails. |

## In the launcher

```bash title="scripts/run_pipeline.sh"
if [ "$SKIP_MIX" -eq 1 ]; then
    banner "Skipping mix phase (--skip-mix)"
else
    banner "Stage 5: mix → m4b"
    MIX_ARGS=(--title "$BOOK_TITLE")
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        MIX_ARGS+=(--v2)
    fi
    if [ "$MODE" = "scenario" ]; then
        # Play pacing (user-tested, DESIGN.md §17.4): 0.75 s between scenes
        # (the 2 s book pad drags), plus a 0.35 s lead-in before every line so
        # the 2reply reader app's ~0.3 s playback poll lands in silence.
        # MUST equal scenario-sync's SCENARIO_INTER_SCENE / SCENARIO_LEAD_IN
        # so m4b and sync agree.
        MIX_ARGS+=(--inter-chapter 0.75 --lead-in 0.35)
    fi
    run_step - "Stage 5 (mix)" uv run lnvox s5 "$BOOK_ID" "${MIX_ARGS[@]}"
fi
```

- The stage runs in every mode unless `--skip-mix` is given. `$BOOK_TITLE` comes from `--book-title` and defaults to the book id.
- `--tts-backend vibevoice` adds `--v2`.
- Scenario mode adds the play pacing. Narration and lecture use the defaults (0.25 / 1.0 / 2.0, no lead-in).
- Whatever pacing is used, it is recorded in `<title>.timings.json`, and the sync stage that runs next reads it from there. See [the layout contract](#the-layout-contract).
- The step goes through `run_step -`: it is retried up to `$STEP_RETRIES` times (default 3) with no pre-retry hook. See [launcher functions](/launcher/functions/).

:::caution
The launcher does not forward `--novels-root` to s5, although it passes it to s6. With a non-default `--novels-root`, s5 looks for pictures under `novels/<book>/` and may embed none, or the wrong ones. Stage 6 follows the picture list s5 recorded, so the manifest `images` still match whatever was embedded, but the m4b itself may lack its illustrations. Run `lnvox s5 … --novels-root <dir>` by hand in that case.
:::

## Inputs and outputs

**Reads**

| Path | Used for |
|---|---|
| `artifacts/<book>/05_audio/<chapter>/manifest.json`, or `05_audio_v2/` with `--v2` | Beat order, `scene_id`, `wav_path`. The directories are read in sorted order, and any without a `manifest.json` (such as `_staged/`) are ignored. |
| `artifacts/<book>/05_audio/<chapter>/*.wav` | The beat audio. |
| `artifacts/<book>/00_text.jsonl` | Chapter titles for the markers. In scenario mode these are the scene titles. A missing file means the chapter ids are used as titles. |
| `artifacts/<book>/00_book_meta.json` | `cover_image`, propagated from the EPUB by `lnvox ingest`. |
| `novels/<book>/.epub_meta.json`, `novels/<book>/images/` | Spine-ordered illustration list and files. |

**Writes**

| Path | Content |
|---|---|
| `artifacts/<book>/06_final/<title>.m4b` | The audiobook: AAC in an MP4 container, chapter markers, `attached_pic` streams, `+faststart`. |
| `artifacts/<book>/06_final/<title>.timings.json` | The layout of this mix: the m4b's file name, the render directory, the four pad values, the pictures in embed order, and each mixed chapter's start and end. Also the LUFS target and the total duration. Written only after the m4b is fully encoded. See the [sidecar format](/library/stages/s5-mix/#timingsjson-sidecar). |
| `artifacts/<book>/06_final/_work/` | Silence pads, per-chapter WAVs, `book_raw.wav`, `book_normalized.wav`, concat lists and `ffmetadata.txt`. Removed after a successful mix unless `--keep-work`. Kept when the mix fails. |

## How it works

`cli.stage5` → [`s5_mix.resolve_book_images`](/library/stages/s5-mix/#resolve_book_images) → [`s5_mix.mix`](/library/stages/s5-mix/#mix) → `_make_silence` → `_build_chapter_concat_items` / `_concat_to_wav` → `_loudnorm` → `_build_ffmetadata` → `_encode_m4b`.

1. The CLI loads every chapter's [`ChapterAudio`](/library/tts/schema/) manifest, filtered by `--chapters`, and the chapter titles.
2. `resolve_book_images` picks the cover and the ordered extra pictures. Stage 6 and `lnvox sample` use the same function, so the picture order is identical everywhere.
3. `mix` generates the silence pads. For each chapter it concatenates `[lead-in] beat`, `pad [lead-in] beat`, … into a chapter WAV and probes its duration to place the chapter marker. It then joins all chapters with the inter-chapter pad and runs single-pass `loudnorm=I=-18:LRA=11:TP=-2`. Finally it encodes AAC and muxes chapters and pictures in one ffmpeg call.
4. It writes the `.timings.json` sidecar and removes `_work/`.

The full ffmpeg command lines are listed in [the ffmpeg pipeline](/library/stages/s5-mix/#the-ffmpeg-pipeline).

## The layout contract

The sync emitters never probe audio. They replay the pads from the manifests' `duration_seconds` values, so they must use exactly the layout s5 used, or each timestamp after the first mismatched pad drifts by the difference.

s5 records that layout in `06_final/<title>.timings.json`, and `lnvox s6`, `lnvox scenario-sync` and `lnvox sample` read it back by default. They follow `--title <title>` when given, and otherwise the newest sidecar in `06_final/`, which is the last successful mix. What they take from it:

| Recorded | Used by |
|---|---|
| the four pad values | s6 and scenario-sync, as the defaults of their pad flags |
| the chapter list actually mixed (`--chapters`), with each chapter's measured start and end | s6 times only those chapters and starts each one where the m4b's chapter marker says. scenario-sync emits only those scenes, in that order. |
| the pictures in embed order (`--cover`, `--images-dir` included) | s6, for the manifest `images` slots |
| the render directory (`05_audio` or `05_audio_v2`) | s6 and scenario-sync refuse a `--v2` mix: sessions have no per-beat timing |
| the m4b's file name | s6 writes it into the manifest as `m4b`, and `lnvox sample` slices that file |

You no longer need to repeat pad flags on the sync stage. A pad flag given there overrides the recorded value, and s6 prints a `[WARN]` when the two differ. The flag names still differ between the stages:

| Pad | `lnvox s5` | `lnvox s6` | `lnvox scenario-sync` | Book value | Scenario value (launcher) |
|---|---|---|---|---|---|
| between beats of one scene / run | `--intra` | `--intra` | `--intra` | `0.25` | `0.25` |
| scene change / staging pause | `--inter-scene` | `--inter-scene` | `--staging-pause` | `1.0` | `1.0` |
| between chapters / script scenes | `--inter-chapter` | `--inter-chapter` | `--inter-scene` | `2.0` | `0.75` |
| before every beat | `--lead-in` | `--lead-in` | `--lead-in` | `0.0` | `0.35` |

With **no** sidecar (a book mixed before sidecars existed, or never mixed), each sync stage falls back to its own defaults: s6 to the book values and every chapter in `05_audio/`, scenario-sync to the scenario values and every script scene. A sidecar that predates the picture list, the render directory and the m4b name still supplies the pads and chapters. Only the pictures are then re-resolved from the book, without the s5 overrides.

:::caution[Scenario mode by hand]
`lnvox s5` still defaults to `--inter-chapter 2.0 --lead-in 0.0`. If you re-mix a play by hand, pass `--inter-chapter 0.75 --lead-in 0.35` to s5 to get the play pacing. scenario-sync now follows whatever the mix used, so the sync file matches the audio either way. The scenario-sync `--inter-scene` flag is the scene-to-scene pad, not s5's `--inter-scene`.
:::

## Re-running and caching

- s5 is **not incremental**. Every run re-concatenates, re-normalises and re-encodes the whole book. A full novel takes minutes, dominated by loudnorm and AAC.
- Nothing is reused between runs. The silence pads take milliseconds to regenerate, so `_work/` is removed after every successful mix. It holds about three full-length 48 kHz stereo WAVs, roughly 690 MB per hour of audio each, plus the chapter WAVs.
- Re-running overwrites `<title>.m4b` and `<title>.timings.json`. A different `--title` leaves the old pair in `06_final/`. The downstream stages pick the newest sidecar, so the stale m4b is ignored unless you name its title. Delete it to reclaim the space.
- Re-run s6 or scenario-sync after every re-mix. They read the sidecar at the time they run.

## Failure modes

| Symptom | Cause |
|---|---|
| `Missing …/05_audio. Run lnvox s4.` (exit 1) | Stage 4 has not run. With `--v2` the hint is `lnvox s4 --tts-backend vibevoice`. |
| `No rendered chapter manifests found under …` (exit 1) | No `<chapter>/manifest.json`, or `--chapters` matched nothing. |
| `FileNotFoundError: Beat WAV not found at … (or …)` | A beat WAV was deleted, typically by [`lnvox audio purge`](/stages/utilities/#lnvox-audio-purge), and s4 has not re-rendered it yet. |
| `RuntimeError: ffmpeg failed (exit N)` with a stderr tail | ffmpeg rejected an input (a corrupt WAV, an unsupported picture). The last 40 stderr lines are included. |
| `Cover image referenced by 00_book_meta.json is missing … — skipping` | The EPUB was re-extracted elsewhere. The m4b is built without that cover. |

A failed mix writes no sidecar and leaves `06_final/_work/` in place, so the intermediate WAVs and concat lists can be inspected. The next successful run removes it.

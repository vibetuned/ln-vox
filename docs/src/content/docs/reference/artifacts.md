---
title: Artifacts and on-disk layout
description: Every file and directory ln-vox reads or writes — inputs, artifacts/<book>/, caches, voicebanks, vendored engines — and what to delete to re-run a stage.
sidebar:
  order: 3
  label: Artifacts
---

This page lists every file and directory on disk that each stage reads or writes, which stage produces it, which stages consume it, and where its format is defined. Use it to find out where an output lives, what a stage will reuse on a re-run, and what to delete to force a redo. File names below were checked against `src/lnvox`.

## Top-level layout

```text
<repo>/
├── epubs/                   source EPUBs (input)
├── novels/<book>/           chapter .txt + images/ + .epub_meta.json   (ingest-epub → ingest)
├── scenarios/               theater scripts (input for ingest-scenario; private, gitignored)
├── artifacts/<book>/        everything the stages produce, one tree per book
├── cache/
│   ├── tts/                 content-addressed TTS renders (all engines)
│   └── scenario/            ingest-scenario LLM result cache
├── voicebank/               reference clips + manifest ($LNVOX_VOICEBANK)
├── voicebank-fr/ …          other banks, same layout
├── data/                    raw Common Voice corpora (voicebank seeding)
├── external/                DramaBox/ and VibeVoice/ clones
├── models/VibeVoice-Large/  optional local VibeVoice weights
└── samples/<book>/          `lnvox sample` output
```

Everything above except the source code is gitignored.

:::caution[Run from the repo root]
Only `artifacts/` and the voicebank are configurable. `artifacts/` is `Settings().artifacts_dir`, set with `LNVOX_ARTIFACTS_DIR` (see [config](/library/config/)) and honoured by the CLI, the launcher, `s4_retry.sh`, the Studio and the ComfyUI pack; the two shell scripts read it from the process environment only, so export it rather than keeping it in `.env`. The voicebank comes from `LNVOX_VOICEBANK` (default `voicebank`). `cache/tts`, `cache/scenario`, `novels/` and `epubs/` are literal paths relative to the current directory (`--novels-root` / `--epub` aside). Moving them away from the repo root breaks the launcher.
:::

Book ids are `<series>/<volume-NN>` (flat ids work too), and the slash is kept in paths: `artifacts/toaru/volume-02/`. Sibling directories with the same prefix and a smaller trailing number (compared numerically, so `volume-9` precedes `volume-10`) count as prior volumes. [series](/library/series/) finds them; voice cast uses them so recurring characters keep their voices. s1 doesn't merge their casts.

## Inputs

### `epubs/`

Source EPUBs. [`lnvox ingest-epub`](/stages/ingest-epub/) accepts any path. [s6](/stages/s6-sync/) defaults to `epubs/<book_id>.epub` (e.g. `epubs/level99/volume-01.epub`), as does the launcher's sync step, and `--epub` overrides it. With no EPUB at that path, the launcher skips s6.

### `novels/<book>/`

Written by [ingest-epub](/stages/ingest-epub/) (or by hand for a `.txt`-only book), read by [ingest](/stages/ingest/). s5, s6 and `sample` read it again for pictures and metadata. The root is `--novels-root` (default `novels`).

| Path | Content |
|---|---|
| `NN-<slug>.txt` | One chapter per file. The first line is the title and the rest is the body. `ingest` takes `NN` (the text before the first `-`) as `chapter_id` and sorts by filename |
| `images/` | Every image in the EPUB manifest, extracted as-is. In lecture mode it also holds rendered visual elements named `code_<sha12>.png`, `table_<sha12>.png`, `figure_<sha12>.png` (needs the `render` extra) |
| `.epub_meta.json` | Sidecar: `title`, `authors`, `publisher`, `language`, `cover_image`, `images` (spine-ordered `images/…` paths), `chapters` (`file`, `title`, `source_parts`), `visual_elements`. Format: [ingest.epub](/library/ingest/epub/) |

The spine-ordered `images` list sets the order of the pictures embedded in the m4b, and s5 records that order in the timings sidecar (`embedded_images`), which s6 reads so its manifest images line up one-for-one ([s5_mix](/library/stages/s5-mix/), DESIGN.md §23).

## `artifacts/<book>/`

```text
artifacts/<book>/
├── 00_book_meta.json
├── 00_text.jsonl
├── 00_script.json                      scenario mode only
├── 01_characters.json
├── 01_characters_per_chapter/<ch>.json
├── 01_characters_merge_log.json
├── 02_scenes/<ch>.json
├── 03_voice_profiles.json
├── 03_directed/<ch>.json
├── 04_voice_assignments.json
├── 05_audio/
│   ├── <ch>/<scene_id>_b<NNNN>.wav
│   ├── <ch>/manifest.json
│   └── _staged/                        staged s4 intermediates (transient)
├── 05_audio_v2/<ch>/{<scene_id>_v<NNN>.wav, manifest.json}   vibevoice only
├── _s4_full.log                        s4_retry.sh / ComfyUI TTS node
├── 06_final/
│   ├── <title>.m4b
│   ├── <title>.timings.json
│   └── _work/                          s5 intermediates (removed after a successful mix)
└── 07_sync/
    ├── <book_id>.epub, sync_manifest.json, unmatched.json          s6
    └── <scene_id>.json, play.json, play.srt                        scenario-sync
```

### Ingest (`00_*`)

| File | Written by | Read by | Format |
|---|---|---|---|
| `00_book_meta.json` | [ingest](/stages/ingest/), only when the source folder has `.epub_meta.json`. It is a copy with `cover_image` made absolute | [s5](/stages/s5-mix/) (cover), and s6 / `sample` through `resolve_book_images` | Same keys as `.epub_meta.json` ([ingest.epub](/library/ingest/epub/)) |
| `00_text.jsonl` | [ingest](/stages/ingest/). [ingest-scenario](/stages/ingest-scenario/) writes one line per script scene | [s1](/stages/s1-characters/), [s2](/stages/s2-scenes/), [lecture](/stages/lecture/), [s5](/stages/s5-mix/) (chapter titles for the m4b markers) | One `Chapter` (`chapter_id`, `title`, `text`) per line ([ingest.text](/library/ingest/text/)) |
| `00_script.json` | [ingest-scenario](/stages/ingest-scenario/) | [scenario](/stages/scenario/), [scenario-sync](/stages/scenario-sync/). Without `--scenario-file`, the launcher skips ingest-scenario when this and `01_characters.json` both exist, and refuses to start otherwise | `ScenarioScript` ([llm.schemas](/library/llm/schemas/)) |

### Characters and scenes (`01_*`, `02_*`)

| File | Written by | Read by | Format |
|---|---|---|---|
| `01_characters.json` | [s1](/stages/s1-characters/) writes the merged cast and rewrites it on every run. `ingest --mode lecture` writes a `{"characters": []}` stub if the file is missing. [ingest-scenario](/stages/ingest-scenario/) writes the script's roster | s2, [voice cast](/stages/voice-cast/), [s3](/stages/s3-director/), [scenario](/stages/scenario/). Later volumes' voice cast (aliases for matching recurring characters); their s1 loads it but doesn't merge it. The [Studio](/guides/voicebank-studio/) Casting tab | `CharacterList` ([llm.schemas](/library/llm/schemas/)) |
| `01_characters_per_chapter/<ch>.json` | s1, one per chapter | s1 (resume cache) | `CharacterList` |
| `01_characters_merge_log.json` | s1, only when there is more than one chapter or a prior volume | People debugging the merge | `counts`, `lone_dropped`, `lone_kept`, `new_in_final_unmatched`, `clusters` ([stages.s1_characters](/library/stages/s1-characters/)) |
| `02_scenes/<ch>.json` | [s2](/stages/s2-scenes/) | s3, [voice audit](/stages/voice-audit/) (when there is no `03_directed/`), the Studio's `#lines` column | `ChapterScenes` ([llm.schemas](/library/llm/schemas/)) |

### Casting and direction (`03_*`, `04_*`)

| File | Written by | Read by | Format |
|---|---|---|---|
| `03_voice_profiles.json` | [s3](/stages/s3-director/), [scenario](/stages/scenario/) | s3 / scenario (reused if present). [voice cast](/stages/voice-cast/) uses it as matching hints on re-runs | `VoiceProfileList` ([llm.schemas](/library/llm/schemas/)) |
| `03_directed/<ch>.json` | s3, [lecture](/stages/lecture/), scenario (one file per script scene) | [s4](/stages/s4-tts/), [s6](/stages/s6-sync/), voice audit (preferred over `02_scenes/`), [scenario-sync](/stages/scenario-sync/) | `ChapterDirected` ([llm.schemas](/library/llm/schemas/)) |
| `04_voice_assignments.json` | [voice cast](/stages/voice-cast/). Rewritten by `voice audit --fix` and by the Studio's Casting tab | s3, lecture, scenario, s4, voice audit. Voice cast of this volume on re-runs and of later volumes. The Studio | `BookCasting` ([voices.schema](/library/voices/schema/)) |

### Audio (`05_*`)

| Path | Written by | Read by | Format |
|---|---|---|---|
| `05_audio/<ch>/<beat_id>.wav` | [s4](/stages/s4-tts/), Dramabox (monolithic or `--staged`). `beat_id` = `<scene_id>_b<NNNN>`, e.g. `02_s1_b0000` | [s5](/stages/s5-mix/), `lnvox audio purge` | WAV **copied from `cache/tts/`** and edge-trimmed |
| `05_audio/<ch>/manifest.json` | s4 | s5, [s6](/stages/s6-sync/) (beat timings), [scenario-sync](/stages/scenario-sync/), `audio purge` | `ChapterAudio` / `RenderedBeat` ([tts.schema](/library/tts/schema/)). `wav_path` is relative to `artifacts/<book>` |
| `05_audio/_staged/` | `s4 --staged` | The staged phases | See [below](#05_audio_staged) |
| `05_audio_v2/<ch>/<scene_id>_v<NNN>.wav` + `manifest.json` | `s4 --tts-backend vibevoice` | `s5 --v2` only. s6 and scenario-sync skip v2 audio, which has no per-beat timing | `ChapterAudio`, one `RenderedBeat` per **session** ([stages.s4_vibevoice](/library/stages/s4-vibevoice/)) |
| `_s4_full.log` | `scripts/s4_retry.sh` (and the ComfyUI TTS node), appended across attempts | People | Text, `==== Attempt N @ … ====` banners ([helper scripts](/launcher/helper-scripts/#s4_retrysh)) |

s5 and s6 only treat a directory as a chapter when it contains `manifest.json`, so `_staged/` never ends up in the mix.

#### `05_audio/_staged/`

Crash-resumable intermediates for the staged Dramabox path (DESIGN.md §15, [staged](/library/tts/staged/)). "Done" means the file exists (written to `*.tmp`, then `os.replace`).

```text
05_audio/_staged/
├── plan.json                     StagedPlan, rebuilt on every run (cheap, deterministic)
├── refs/<ref_hash>.safetensors   phase refs: VAE-encoded (RE-USE-denoised) voice clip
├── ctx/<item_id>.safetensors     phase ctx: Gemma prompt encoding per chunk
├── ctx/neg.safetensors           the negative-prompt encoding (only when cfg_scale > 1)
├── latents/<item_id>.safetensors phase denoise: DiT output per chunk
├── inflight.json                 item currently being attempted (crash attribution only)
└── attempts.json                 restarts per phase:item (diagnostics; persists across runs)
```

`item_id` = `{cache_key}_c{NN}-{sha256(chunk text)[:8]}`. The decode phase deletes each chunk's `ctx` and `latents` files once its WAV has landed in `cache/tts/`. After a successful run the whole directory is removed, unless you pass `--keep-staged`. A failed run leaves it in place so the next run resumes. DESIGN.md §15.5 estimates the peak for a ~5,000-item novel at roughly 50 MB of refs, 5–10 GB of ctx and 1 GB of latents.

### Mix and sync (`06_*`, `07_*`)

| Path | Written by | Read by | Format |
|---|---|---|---|
| `06_final/<title>.m4b` | [s5](/stages/s5-mix/). `<title>` is `--title` (default: the book id) with `/` and `\` replaced by `_` | Players, `lnvox sample` | AAC (96 kbps default) at −18 LUFS, with chapter markers and embedded cover and illustrations |
| `06_final/<title>.timings.json` | s5 | s6, scenario-sync, `lnvox sample`, the ComfyUI nodes | The mix's **layout contract**: `book_title`, `m4b`, `audio_dir`, `total_duration_seconds`, `target_lufs`, the four pads and lead-in, `embedded_images` (embed order, cover first) and `chapters[]` (`chapter_id`, `title`, `start_seconds`, `end_seconds`, `duration_seconds`) ([stages.s5_mix](/library/stages/s5-mix/)) |
| `06_final/_work/` | s5 | s5 | `sil_*ms.wav` pads, `chapter_<id>.wav`, `book_raw.wav`, `book_normalized.wav`, `*_list.txt` concat lists, `ffmetadata.txt`. Removed after a successful mix (kept on failure, or with `--keep-work`) |
| `07_sync/<book_id>.epub` | [s6](/stages/s6-sync/), with `/` in the book id replaced by `_` | The reader app, `lnvox sample` | Source EPUB with `data-beat-id` spans |
| `07_sync/sync_manifest.json` | s6 | The reader app, `lnvox sample` | Beat times, images aligned to the m4b's embedded pictures, visual elements, match stats ([stages.s6_sync](/library/stages/s6-sync/)) |
| `07_sync/unmatched.json` | s6 | People | `{"beats": [...]}`, the beats that could not be located in the EPUB |
| `07_sync/<scene_id>.json`, `play.json`, `play.srt` | [scenario-sync](/stages/scenario-sync/) | The reader app | `SceneSync` / `PlaySync` ([stages.scenario_sync](/library/stages/scenario-sync/)) |

`06_final/_work/` holds about three uncompressed 48 kHz stereo copies of the book while a mix runs (the chapter WAVs, `book_raw.wav`, `book_normalized.wav`), roughly 0.7 GB each per hour of audio. s5 deletes it once the m4b is written; nothing reuses it. For comparison, `05_audio/` holds about 600 MB of WAVs per hour of audio, and the m4b about 50 MB.

## Caches

### `cache/tts/`

The content-addressed render cache shared by every book and engine: `cache/tts/<key>.wav`, where `<key>` is the **first 16 hex characters** of a SHA-256 (`_content_hash` in [stages.s4_tts](/library/stages/s4-tts/)). It survives book deletion.

| Path | Key parts |
|---|---|
| Dramabox, per beat | beat `prompt` + reference clip **file name** (`no-ref` if none) + `DramaboxClient.MODEL_VERSION` + recipe tag |
| VibeVoice, per session | `Speaker N:` session script + `|`-joined reference file names + `VibeVoiceClient.MODEL_VERSION` |

The recipe tag records how a render differs from the original v1 recipe. Default staged runs are tagged `+enc-bf16+cap22+dec-fp32`. The monolithic path (CUDA/CPU only — it refuses MPS) defaults to the bnb4 encoder and gets no tag. On MPS the tag is always the effective recipe, so the revert knobs, which can't change the audio there, don't re-key it. **Switching between staged and monolithic s4, or changing any `LNVOX_S4_*` render knob, re-renders the whole book** (see [render quality](/guides/render-quality/)). Dramabox beats are edge-trimmed when s4 places them, and a legacy untrimmed cache entry is trimmed in place the next time it's used. VibeVoice trims inside its client before writing. `--no-trim` (`LNVOX_S4_NO_TRIM=1`) disables both.

### `cache/scenario/`

[ingest-scenario](/stages/ingest-scenario/)'s LLM cache: `cache/scenario/<key>.json`, where the key is the first 16 hex characters of SHA-256(schema name, system prompt, rendered user prompt, model name). Editing one scene re-keys only that scene, and a prompt-template or model change re-keys everything. `--no-cache` skips the reads and overwrites the entries, so the re-roll sticks. Format: [ingest.scenario](/library/ingest/scenario/).

## Voicebanks

```text
voicebank/                          $LNVOX_VOICEBANK (default: voicebank)
├── manifest.json                   Voicebank {version, clips[]}
├── clips/
│   ├── cv_<client_id[:12]>.wav     voice seed-cv, Studio "Promote" (mono 24 kHz)
│   └── <id>.wav                    Studio manual add (manual_<stem> by default)
└── acoustics.json                  voice audit's per-clip index (pitch + recording quality)
```

| Path | Written by | Read by | Format |
|---|---|---|---|
| `manifest.json` | [`voice seed-cv`](/stages/voicebank/) (merges with existing clips), the [Studio](/guides/voicebank-studio/) | voice cast, s3, scenario, s4, voice audit, `voice list`, the Studio | `Voicebank` / `VoiceClip` ([voices.schema](/library/voices/schema/), [voices.manifest](/library/voices/manifest/)). `clip_path` is relative to the bank root |
| `clips/*.wav` | seed-cv, the Studio | s4 (voice references), voice audit, the Studio | Mono 24 kHz PCM-16 WAV, about 10–20 s |
| `acoustics.json` | [voice audit](/stages/voice-audit/): clips not yet indexed are measured and added | voice audit | `{clip_id: {…quality metrics, "pitch": Hz}}` ([voices.acoustics](/library/voices/acoustics/)). Delete it to re-measure |

`voicebank-fr/`, `voicebank-demo/` and any other bank share the same layout. Select one per shell with `LNVOX_VOICEBANK=voicebank-fr` (DESIGN.md §17.5).

`data/<cv-corpus-…>/<locale>/` is the raw Common Voice extraction (`clips/*.mp3` plus `validated.tsv`, `train.tsv`, …). Only `voice seed-cv` and the Studio's Import tab read it.

## Vendored engines and weights

| Path | Written by | Read by |
|---|---|---|
| `external/DramaBox/` | `setup_dramabox.sh` (shallow `git clone`) | `DramaboxClient` / the staged phases add it to `sys.path`. The launcher's `install_dramabox_reqs` reads its `requirements.txt` |
| `external/VibeVoice/` | `setup_vibevoice.sh` (pinned commit) | Installed editable. The launcher re-installs it in `prepare_vibevoice_env` |
| `models/VibeVoice-Large/` | `setup_vibevoice.sh --weights` / `--weights-hf` | [VibeVoiceClient](/library/tts/vibevoice-client/) if it has `config.json` (after `$LNVOX_VIBEVOICE_MODEL`) |
| Hugging Face cache (`~/.cache/huggingface`) | First runs | Dramabox weights (~15 GB), the unquantized `unsloth/gemma-3-12b-it` encoder (~23 GB, staged default), GGUF / MLX / vLLM LLM weights, VibeVoice when there's no local copy |

`samples/<book_id with / → _>/` is written by [`lnvox sample`](/stages/utilities/): `<leaf>-sample.m4b`, `<leaf>-sample.epub`, `sync_manifest.json`, `README.md`. It needs `06_final/*.m4b`, `07_sync/*.epub` and `07_sync/sync_manifest.json`.

## Re-running a stage: what to delete

s1 and s2 **cache per chapter by chapter id, not by content**: a chapter file that already exists gets reused even if its input changed, so re-running an upstream stage doesn't invalidate them — after redoing s1, delete `02_scenes/` too. s3, lecture and scenario direction also cache per chapter, but each file records the casting and settings it was written with (voice descriptors, and for lecture the normalize flag), so a recast or a changed flag is picked up without deleting anything.

| To redo | Delete (or pass) | Why (from the code) |
|---|---|---|
| [ingest-epub](/stages/ingest-epub/) | nothing | Re-extracting into the same folder first removes the chapter `.txt` files the previous run listed in `.epub_meta.json` (and nothing else), so an older split doesn't linger |
| [ingest](/stages/ingest/) | nothing | Always rewrites `00_text.jsonl` (and `00_book_meta.json`). The lecture stub `01_characters.json` is only written when missing |
| [ingest-scenario](/stages/ingest-scenario/) | nothing, or pass `--no-cache` to re-roll | Always rewrites `00_text.jsonl`, `01_characters.json` and, last, `00_script.json`, serving unchanged LLM calls from `cache/scenario/`. `--no-cache` skips the cache reads and overwrites the entries |
| [s1](/stages/s1-characters/) | `01_characters_per_chapter/` (or one `<ch>.json`) | Per-chapter extraction is loaded from there when present. The cross-chapter merge and `01_characters.json` are rebuilt every run |
| [s2](/stages/s2-scenes/) | `02_scenes/` (or one `<ch>.json`) | Cached per chapter. A cached file with no scenes or an empty scene is re-segmented automatically |
| [voice cast](/stages/voice-cast/) | `04_voice_assignments.json`, or pass `--fresh` | **Locked** entries are always kept (over prior volumes, `--narrator-clip` and `--fresh`). Unlocked entries are the weakest layer: prior-volume matches override them, and `--fresh` drops them |
| [voice audit](/stages/voice-audit/) | nothing | Stateless. `<bank>/acoustics.json` is only an incremental measurement cache |
| [s3](/stages/s3-director/) | nothing after a recast; `03_directed/<ch>.json` for fresh performance cues | A recast character's profile is regenerated and every cached chapter they speak in is re-voiced (no LLM call) — s4 then re-renders those beats. `--regen-profiles` forces every descriptor, which re-voices every chapter |
| [lecture](/stages/lecture/) | nothing | Each chapter records the normalize flag and narrator descriptor: toggling `--no-normalize` rebuilds it, a new narrator only re-voices it |
| [scenario](/stages/scenario/) | nothing after a recast; `03_directed/<scene>.json` for fresh cues | Same casting-aware caching as s3; `lnvox scenario --regen-profiles` forces every descriptor |
| [s4](/stages/s4-tts/), Dramabox | the beats' `cache/tts/` entries: `lnvox audio purge <book> --speaker <name>` | s4 re-places `05_audio/` from the cache on every run, so **deleting `05_audio/` alone re-renders nothing**. A changed prompt (new s3 output), reference clip or recipe changes the key, and those beats re-render automatically |
| s4, staged intermediates | `05_audio/_staged/` | Always safe. Only present after a failed run or with `--keep-staged` |
| s4, VibeVoice | the `cache/tts/<cache_key>.wav` files listed in `05_audio_v2/<ch>/manifest.json` | `audio purge` only scans `05_audio/`. Session boundaries depend on `--max-session-chars`, so changing it re-keys sessions |
| [s5](/stages/s5-mix/) | nothing | Rebuilds the mix and its timings sidecar every run |
| [s6](/stages/s6-sync/), [scenario-sync](/stages/scenario-sync/) | nothing | Rewrite `07_sync/` every run, laid out from the mix's timings sidecar (`--title` picks the mix, default the newest) |

:::caution[Ways a stale file survives]
- **The reference token is the clip's file name.** If you replace the audio of `clips/cv_x.wav` but keep its name, no cache key changes. Purge the affected speakers' renders.
- **Hand-made chapter folders sort by their numeric prefix.** `ingest` orders `NN-…txt` numerically, but later stages order chapter ids as strings; extracted books are padded to a common width, so only a hand-made folder mixing widths (`9-…`, `10-…`) needs renaming.
:::

Placement is not one of them: the staged decode re-copies `cache/tts/<key>.wav` into `05_audio/<ch>/` whenever the placed file isn't an exact copy of the entry `manifest.json` names, so reverting a casting, direction or recipe to one rendered earlier puts the earlier audio back.

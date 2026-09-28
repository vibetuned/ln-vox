---
title: Voicebank Studio
description: The PySide6 GUI in scripts/voicebank_studio.py — curate the voicebank by ear, import Common Voice speakers, re-cast characters, and audition TTS prompts.
sidebar:
  order: 6
---

`scripts/voicebank_studio.py` is a standalone desktop tool for the jobs the CLI can't do by ear: choosing good reference voices, fixing a casting, and working out how to phrase a direction so the TTS engine performs it. It reads and writes the voicebank (`manifest.json` + `clips/`) and a book's `04_voice_assignments.json`, and never runs a pipeline stage. The design is in DESIGN.md §12.

The window has four tabs: **Voicebank**, **Import from Common Voice**, **Casting** and **TTS Lab**.

## Install and run

PySide6 is deliberately not a project dependency. The pipeline never imports it.

```bash
uv sync --extra voice --extra tts        # NOT --extra serve
uv pip install PySide6                   # one-time, GUI toolkit
uv run python scripts/voicebank_studio.py
```

The first three tabs need only the `voice` extra (librosa, soundfile). The script exits with an install hint if PySide6 or those libraries are missing. The TTS Lab additionally needs a TTS engine installed, and only when you press **Load model**. The engine imports sit inside the load worker, so the Studio starts without torch, Dramabox or VibeVoice present.

- **Dramabox**: `./scripts/setup_dramabox.sh` (see [helper scripts](/launcher/helper-scripts/#setup_dramaboxsh)).
- **VibeVoice**: `./scripts/setup_vibevoice.sh`.

:::danger[Keep the Studio out of the LLM (vLLM) env]
Don't run the Studio in a venv synced with `--extra serve`. vLLM needs a newer torch than Dramabox's pinned `torch==2.8`, and mixing them produces `No module named 'av'` and `mamba-ssm` build failures when Dramabox loads. Use the TTS env above, and run `uv sync --extra serve …` again when you go back to the LLM stages. This is why [run_pipeline.sh](/launcher/run-pipeline/) swaps environments between phases.
:::

## Command-line flags

| Flag | Default | Meaning |
|---|---|---|
| `--voicebank DIR` | `$LNVOX_VOICEBANK` (or its `.env` entry), else `<repo>/voicebank` | Voicebank directory. Priority: flag > env var > `.env` > default |
| `--cv-root DIR` | auto-detected under `--data` | A Common Voice locale directory (contains `clips/` + `*.tsv`) for the Import tab |
| `--data DIR` | `<repo>/data` | Searched for the first `**/validated.tsv` that has a sibling `clips/` directory. Falls back to any `clips/` directory next to a `*.tsv` |
| `--casting FILE` | newest `04_voice_assignments.json` under `--artifacts` | Casting file opened in the Casting tab at startup |
| `--artifacts DIR` | `$LNVOX_ARTIFACTS_DIR` (or its `.env` entry), else `<repo>/artifacts` | Where the casting file is auto-detected, where **Open assignments…** starts, and the root used to find prior volumes for series context |

`<repo>` is the repository root, resolved from the script's own location, not from your current directory. Both env defaults are read through lnvox's own [`Settings`](/library/config/), so the Studio opens the same bank and artifacts tree as every `lnvox` command, `.env` included (read from the current directory, as the CLI reads it). A relative value resolves against the current directory, as it does for the CLI. If `Settings` can't be built (a malformed `LNVOX_*` value), the plain env var still applies. Bank selection matches the rest of the toolchain (DESIGN.md §17.5), so one setting picks the bank for every command:

```bash
LNVOX_VOICEBANK=voicebank-fr uv run python scripts/voicebank_studio.py
uv run python scripts/voicebank_studio.py --voicebank voicebank-demo    # one-off look at another bank
```

If the chosen directory has no `manifest.json`, the Studio prints `WARNING: no manifest.json in <dir> — the Studio will open on an empty bank.` to stderr and opens anyway. A misspelled bank name shows up as an empty table, not an error. The first save (promote or manual add) then creates the manifest in that directory.

## Voicebank tab (listen, add, erase)

A table of every clip in the bank, sorted by id. Columns: `id`, `gender`, `age_band`, `accent`, `dur (s)`, `source`, `sample sentences`. The label above the table shows the clip count and the bank path.

- **▶ Play** (or double-click a row) plays `<bank>/<clip_path>`.
- **✕ Erase** asks for confirmation, then deletes the clip's WAV **and** removes its manifest entry. It can't be undone. Nothing checks whether a casting still references the clip. The Casting tab will show such an assignment as `missing!`.
- **+ Add manual…** opens the manual-add dialog (below).
- **⟳ Reload** re-reads `manifest.json` from disk, for example after `lnvox voice seed-cv` ran in another terminal.

Every change here refreshes the Casting and TTS Lab voice lists too.

### Manual add

Imports any audio file (`*.wav *.mp3 *.flac *.ogg *.m4a`) as a new clip:

| Field | Default | Stored as |
|---|---|---|
| Audio file | — | Decoded with librosa to **mono 24 kHz PCM-16**, written to `clips/<id>.wav` |
| Clip id | `manual_<file stem>` | `id`. Rejected if already in the bank |
| Gender | `male` / `female` | `gender` |
| Age band | `teen`, `young_adult`, `adult`, `elder` | `age_band` |
| Accent | `any` | `accent` (free text) |
| Sample sentences | — | `sample_sentences`, the first 3 non-empty lines |
| License | `unknown` | `license` |
| Notes | `Added manually via Voicebank Studio` | `notes` |

The clip is saved with `source="manual"` and `duration_seconds` taken from the decoded audio. The schema is in [VoiceClip](/library/voices/schema/).

## Import from Common Voice tab

Browse a local Common Voice corpus by **speaker** and promote the good ones into the bank. `validated.tsv` is roughly 600 MB / 2.5 M rows, so a background thread streams it instead of loading it (DESIGN.md §12.3).

1. **CV root / TSV**: pick the locale directory. The TSV list puts `validated.tsv`, `train.tsv`, `dev.tsv`, `test.tsv` first, then any other `*.tsv`.
2. **Filters**: Gender (`any`/`male`/`female`), Age (`any` or an age band), Accent (substring of the normalized accent code, e.g. `us`), **Min utts** (1–50, default 8), **Max speakers** (1–2000, default 100).
3. **Scan** streams the TSV. It applies the seeder's `_eligible()` gate (at least 2 up-votes, 0 down-votes, mappable age and gender) and groups rows by `client_id`. A speaker appears as soon as it has *Min utts* eligible rows and passes the filters. The scan stops at *Max speakers*, so it usually reads only the start of the file. The same button reads **Stop** while a scan runs. Results follow file order, so they won't match the speakers the seeder would pick at the same cap.
4. Select a speaker to list their utterances. Double-click one to play its MP3.
5. **▶ Preview merged** runs `build_speaker_clip()` into a temp directory and plays the result. No manifest is touched.
6. **➜ Promote to voicebank** runs the same `build_speaker_clip()` into the bank. It writes `clips/cv_<first 12 chars of client_id>.wav` and appends the clip to `manifest.json`. A speaker already in the bank is refused.

Promotion reuses the seeder's code exactly ([common_voice](/library/voices/common-voice/)): mono 24 kHz, top-voted utterances joined with 0.25 s gaps, a 12 s target, and up to 3 sample sentences. A speaker with less than 8 s of clean speech yields no clip (`This speaker yielded < 8 s of clean speech.`).

All playback goes through `QMediaPlayer`, but anything that isn't a WAV is first decoded to a temp 24 kHz WAV. Qt is only ever asked to play WAV, which avoids per-platform MP3 codec gaps.

## Casting tab

The by-ear counterpart to [voice cast](/stages/voice-cast/) and [voice audit](/stages/voice-audit/). It opens a `04_voice_assignments.json` ([BookCasting](/library/voices/schema/)) and lets you reassign any character's clip.

```bash
uv run python scripts/voicebank_studio.py \
    --casting artifacts/novel-name/volume-01/04_voice_assignments.json
```

The launcher prints this command when it stops at `--stop-after-audit` (see the [casting guide](/guides/casting/)), prefixed with `LNVOX_VOICEBANK=…` / `LNVOX_ARTIFACTS_DIR=…` only when that run had them set — spelling out a default would override a bank kept in `.env`. Without `--casting`, the Studio opens the most recently modified `04_voice_assignments.json` under `--artifacts`. That may not be the book you want, so check the file label.

**Left, characters.** One row per casting:

| Column | Source |
|---|---|
| `character` | `character_name` |
| `#lines` | Beats per speaker counted from the volume's `02_scenes/*.json`: dialogue beats by normalized speaker name, narration beats go to `Narrator`. Shows `—` if s2 hasn't run |
| `target` | `target.gender/target.age_band` |
| `assigned clip` | `assigned_clip_id` or `(none)` |
| `ok?` | `✓` clip is in the bank · `—` unassigned · `missing!` id not in the current bank |
| `inherited` | `↩ <volume>` when the assignment matches the newest prior volume that cast this character, the same identity lookup [`PriorCastIndex`](/library/voices/matcher/) does for `voice cast` |

`#lines` and character aliases come from `02_scenes/` and `01_characters.json` in the casting file's own directory. The `inherited` column also needs prior volumes, and those are only found when the casting file sits under `--artifacts/<series>/<volume>/`.

**Middle, LLM ranked candidates.** The target line (gender/age, accent/timbre/manner keywords, `voice_descriptor` if set, line count, inheritance, `locked`), then the matcher's `ranked` list with scores. `★` marks the assigned clip. Select a candidate to see the LLM's reason, double-click to play it, and press **➜ Assign this candidate** to use it.

**Right, the whole bank.** A filterable table (id / gender / age / accent substring). Double-click to play, **➜ Cast selected clip to character** to assign. If the clip's gender or age band differs from the target, you're asked to confirm. The Studio never re-runs the matcher.

Assigning a different clip (`recast` in the script) changes three fields of that entry, and the status line says so (`Cast Hero → cv_… (unsaved; locked, so voice cast and audit --fix keep it; old voice descriptor cleared — s3 composes one for the new clip).`):

| Field | After a re-cast |
|---|---|
| `assigned_clip_id` | the new clip |
| `locked` | `true`. `voice cast` keeps a locked entry over prior-volume continuity, `--narrator-clip` and `--fresh`, and `voice audit --fix` never moves it. An unlocked hand edit to a recurring character, or to the Narrator of a run given `--narrator-clip`, would be undone by the next `voice cast`, which the launcher re-runs when you continue. To let the automation manage the entry again, set `"locked": false` in the JSON |
| `voice_descriptor` | if it was set: re-derived from the new clip for the Narrator (what `voice cast --narrator-clip` writes; lecture mode reads the Narrator's descriptor verbatim and never composes one); cleared for anyone else, so s3 composes one for the new clip with the character's personality. A stale descriptor would otherwise keep describing the old voice, because s3 uses a non-empty one verbatim |

Re-assigning the clip an entry already has changes nothing. **Save** (enabled with unsaved changes, shown as `💾 Save *`) writes the whole `BookCasting` back with `model_dump_json(indent=2)`; `target`, `ranked`, `candidates_considered` and `audit_note` are written back as loaded. **Reload** and **Open…** ask before discarding unsaved edits.

:::tip[When to re-cast]
Before s3 is cheapest, which is what `--stop-after-audit` is for. A re-cast after s3 still works when you continue the launcher: s3 regenerates the profiles of recast characters only and re-voices their chapters, and s4 re-renders the affected beats (it caches renders by prompt + reference clip).
:::

## TTS Lab

Audition prompts against any bank clip to hear how a direction or description comes out. Renders go to a temp directory (`gen_NNN.wav`). The lab writes nothing to the bank or to `artifacts/`.

### Loading an engine

| Control | Choices | Notes |
|---|---|---|
| Engine | `dramabox`, `vibevoice` | Switching sets `cfg_scale` to the engine default (2.5 / 1.3) and enables `stg_scale` only for dramabox |
| Device | `auto`, `cuda`, `mps`, `cpu` | `auto` picks `mps` if available, else `cuda`, else `cpu` |
| ⚙ Load model | | Loads **once per session**. Engine and device lock after you press it |

The engine is never loaded on startup or by the other tabs. Each engine takes 15–18 GB, and the first run also downloads the weights. Before loading, the Studio checks the installed `transformers` version and shows a dialog. Dramabox needs `>= 4.52`. VibeVoice's fork pins `== 4.51.3` (DESIGN.md §16.6). If they don't match, the dialog prints the fix and asks whether to try anyway:

```bash
uv pip install -U 'transformers>=4.52'     # for dramabox
uv pip install -e external/VibeVoice        # for vibevoice
```

Which client gets loaded:

| Engine | Resolved device | Client |
|---|---|---|
| `vibevoice` | any | [`VibeVoiceClient`](/library/tts/vibevoice-client/) |
| `dramabox` | `mps` (any `mps…` device) | [`StagedLabEngine`](/library/tts/lab-engine/) (`lnvox.tts.lab_engine`) |
| `dramabox` | `cuda` / `cpu` | [`DramaboxClient`](/library/tts/dramabox-client/) |

On MPS, `DramaboxClient`'s monolithic `TTSServer` can't be used (it refuses any MPS device), because it builds every component in one dtype and no single dtype works there. The Gemma encoder returns NaN in fp16 and the vocoder needs fp32 (MAC_TEST_REPORT.md F9/F10). `StagedLabEngine` keeps the staged-s4 components loaded in memory, each in its own dtype: encoder bf16, DiT fp16, vocoder fp32. It applies the same 22 s chunk cap with word-split fallback and the same edge trim as s4, so the lab sounds like a book render. It holds about 32 GB of memory. Measured on an M4 Max: about 51 s to load, and a 6 s audition renders in about 39 s. See the [Apple Silicon guide](/guides/apple-silicon/).

Load errors come with hints:

- **Missing module, vibevoice**: run `./scripts/setup_vibevoice.sh`.
- **Missing module, dramabox**: the module is one of Dramabox's own requirements, which `uv sync` prunes. The hint gives `uv pip install "<module>"` for that one, or a torch-safe reinstall of all of them: `grep -viE '^(torch|torchaudio|torchvision)' external/DramaBox/requirements.txt | uv pip install -r /dev/stdin`.
- **Other load or generate errors**: `_augment_engine_error` adds the transformers-conflict hint whenever the message contains `has no attribute 'model'` or `Gemma3`. It shows the installed version and both fix commands, then says to restart the Studio.

### Composing and generating

- **Voice**: any bank clip, or `(no reference …)`. Dramabox then uses its default voice. VibeVoice requires a reference and refuses to generate without one. **▶ Play reference** plays the clip.
- **Description**: pre-filled from the clip's metadata via `descriptor_from_clip` (only when empty). **↩ from clip** refills it.
- **Line** plus **Insert as** buttons compose the prompt from description + line. Formats: `comma` `{desc}, "{line}"` · `brackets` `[{desc}] "{line}"` · `brackets+NL` `[{desc}]\n"{line}"` · `period` `{desc}. "{line}"` · `colon` `{desc}: "{line}"` · `dash` `{desc} — "{line}"` · `line only` `"{line}"`. The useful comparison is which shape makes Dramabox perform the description instead of reading it aloud.
- **Prompt**: the text actually sent to the engine, freely editable.
- **Seed** (default 42), **cfg_scale** (2.5 dramabox / 1.3 vibevoice), **stg_scale** (1.5, dramabox only). Keep the seed fixed so only the prompt changes.
- **▶ Generate & play** renders and plays. Each render is added to **History** (`#N [engine · ref · seed N] prompt`). Double-click to replay.

### VibeVoice scripts

With VibeVoice the prompt box holds a `Speaker N:` script and the description, format buttons and `stg_scale` don't apply. The reference clip alone sets the voice. `_to_vibevoice_script` prepares the text:

- If at least one line matches `^\s*Speaker\s+(\d+)\s*:` (case-insensitive), the text passes through with blank lines dropped and each line stripped. The highest `N` sets the speaker count.
- Otherwise the whole prompt is collapsed to one line and sent as `Speaker 1: …`.

The lab **clones every speaker from the same selected reference** (`voice_refs = [ref] * N`). It auditions one voice, so a multi-speaker script here tests turn-taking, not casting. Real multi-voice sessions are produced by [s4 with `--tts-backend vibevoice`](/library/stages/s4-vibevoice/).

```text
Speaker 1: You're late.
Speaker 2: The trains were stopped at the border.
```

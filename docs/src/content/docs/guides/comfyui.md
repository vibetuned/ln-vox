---
title: ComfyUI nodes
description: Drive the pipeline as a ComfyUI graph — install the node pack, how nodes chain and cache, every node and its inputs, and the example workflows.
sidebar:
  order: 9
---

The `comfyui/` folder is a ComfyUI custom-node pack that exposes every stage of the pipeline as a node. A graph gives you what the launcher can't: re-run one stage, inspect the cast and the casting between stages, audition voicebank clips and rendered beats, and hand-cast without leaving the canvas. Under the hood each node runs the same `uv run lnvox …` command the launcher runs, with the same retries and the same LLM ↔ TTS hand-off.

## Install

```bash
ln -s /path/to/ln-vox/comfyui /path/to/ComfyUI/custom_nodes/comfyui-lnvox
# restart ComfyUI
```

On Windows, use a directory junction instead: unlike a symlink it needs no Developer Mode, and the checkout is still found through it (from `cmd`):

```bat
mklink /J C:\path\to\ComfyUI\custom_nodes\comfyui-lnvox C:\path\to\ln-vox\comfyui
```

The pack runs the launcher and serve scripts with Git for Windows' `bash.exe`, located next to `git` or under `Program Files\Git` — never a bare `bash`, which on Windows can be WSL's. Set `LNVOX_BASH` to a `bash.exe` to override (MSYS2, Cygwin, a git reached through a shim).

The pack has no Python dependencies of its own. It only needs, on the ComfyUI machine:

- the ln-vox checkout, installed as in [Installation](/start/installation/) (uv, ffmpeg, llama-server, Dramabox, a voicebank);
- `uv`, `ffmpeg` and `ffprobe` reachable — the pack adds `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` and `~/.cargo/bin` to the `PATH` it hands to subprocesses, for ComfyUI builds launched from a desktop icon.

The checkout is found through the symlink. If you copied the folder instead, set `LNVOX_REPO=/path/to/ln-vox` before starting ComfyUI or fill the Book node's `repo_path`.

After a restart the nodes are under **ln-vox/** in the node library, and five example workflows appear under **Templates → comfyui-lnvox**.

## Why subprocesses

ComfyUI has its own Python and its own torch; ln-vox needs *two* incompatible venv states (vLLM or llama.cpp for the LLM phase, Dramabox's pinned torch for TTS — see [concepts](/start/concepts/#one-venv-swapped-between-phases)). So nothing is imported into ComfyUI: nodes run `uv run lnvox …` inside the checkout, and the LLM Server and TTS nodes call `scripts/run_pipeline.sh --prepare-env llm|tts|vibevoice` to swap the venv exactly as the launcher does. Your ComfyUI environment is never touched, and ComfyUI's own models are unloaded from VRAM before a render starts.

## How the graph works

**The book travels along the wires.** Every node takes and returns an `LNVOX_BOOK`: the book id, mode, title, input paths, the voicebank, the LLM server settings and the TTS backend used. Wiring nodes in a chain is what orders the stages. Later nodes read what earlier ones recorded — the LLM server a stage should (re)launch, or whether audio came from VibeVoice.

**Results are cached; `rerun` forces a stage.** ComfyUI re-executes a node only when its inputs (or an upstream node's) change, so queueing a finished graph again does nothing. Every stage node has a `rerun` counter: change it to run that node — and everything after it — again, e.g. after hand-editing an artifact. The stages themselves are resumable, so a re-run continues from the caches rather than starting over. Inspect and Load Audio always re-read the disk.

**Logs are on the node.** Each stage node shows the live tail of its command's output while it runs (a multi-hour s4 included) and its final log when done. The full output also goes to the ComfyUI console, prefixed `[ln-vox]`. **Cancel** in ComfyUI kills the stage's whole process tree.

**The LLM server follows the launcher's rules.** Put one **LLM Server** node before the first LLM stage. Every LLM stage checks the server before running and relaunches it if it died, the same as the launcher's `ensure_vllm` hook, so an LLM stage re-run after the server was stopped just brings it back up. **Stop LLM Server** (and the TTS node itself) stops it before rendering. The server's log is `logs/comfyui_llm_<backend>.log` in the checkout.

## Example workflows

| Template | What it shows |
| --- | --- |
| **ln-vox narration (stage by stage)** | Book → Ingest EPUB (bypassed; un-bypass with Ctrl+B for EPUB sources) → Ingest → LLM Server → s1 → s2 → Voice Cast → Casting Audit → s3 → Stop → TTS → Mix → EPUB Sync, with an Inspect on the casting, a summary at the end and the finished m4b in a Preview Audio |
| **ln-vox lecture** | Lecture-mode chain with the rules-only EPUB classifier and a narrator clip |
| **ln-vox scenario** | Play chain with a French voicebank, ingest-scenario → scenario direction → Scenario Sync |
| **ln-vox hand casting** | Voicebank browser + clip audition + Set Voice + re-audit — the `--stop-after-audit` loop |
| **ln-vox one-shot launcher** | Book → Run Pipeline (the launcher as one node) → Inspect + Preview |

The workflows are generated from the nodes' definitions by `comfyui/tools/build_workflows.py`; run it after changing a node's inputs.

## Nodes

All stage nodes output `book` and `log` unless noted, and have a `rerun` counter. Defaults match the CLI and the launcher.

### Book

Starts a chain. `book_id` (`<series>/<volume-NN>`), `mode` (narration / lecture / scenario), and optionally `book_title` (m4b title, default the id), `novels_root` (`novels`), `voicebank` (default `$LNVOX_VOICEBANK`, else `voicebank`), `epub` (Stage 0a input and s6 target, default `epubs/<book_id>.epub`), `scenario_file`, `repo_path`. Its log is the book's current stage status. The artifacts root follows `$LNVOX_ARTIFACTS_DIR` like the CLI.

### Ingest EPUB (0a)

[`lnvox ingest-epub`](/stages/ingest-epub/) into `novels/<book_id>/`. Inputs: `epub_path`, `ingest_classifier` (`fallback` / `none`, lecture only — `fallback` needs the LLM Server upstream), `render`, `split_chapters`. Narration and lecture only.

### Ingest (0)

[`lnvox ingest`](/stages/ingest/) with the book's mode.

### Ingest Scenario (0)

[`lnvox ingest-scenario`](/stages/ingest-scenario/). Inputs: `scenario_file` (default the Book's), `language` (`fr`), `no_cache`. With no file and an existing `00_script.json`, it reuses it — the launcher's behaviour. Needs the LLM.

### LLM Server

Configures and (by default) starts the OpenAI-compatible server. Inputs: `backend` (llama / vllm / mlx), `model` (blank = the [launcher's default](/launcher/run-pipeline/#options) for that backend), `external_url` (use a running server; nothing is launched or stopped — the launcher's `--vllm-url`), `max_model_len` (exported as both `LNVOX_LLM_MAX_LEN` for the serve script and `LNVOX_LLM_MAX_MODEL_LEN` for the client), `prepare_env` (swap the venv to the LLM state first), `start_now` (off: start lazily at the first LLM stage).

### Characters (s1), Scenes (s2)

[`lnvox s1`](/stages/s1-characters/) and [`lnvox s2`](/stages/s2-scenes/). Input: `chapters` (comma-separated ids, blank = all). Narration only.

### Voice Cast (V)

[`lnvox voice cast`](/stages/voice-cast/). Inputs: `narrator_clip`, `top_n` (3), `fresh`. A lecture book with a narrator clip makes no LLM call, so it doesn't require the server — the launcher's `NEED_LLM` rule.

### Casting Audit (A)

[`lnvox voice audit`](/stages/voice-audit/). Inputs: `action` (report / fix / off), `min_semitones` (2.5), `min_shared` (3). Never fails the chain on collisions, like the launcher's `|| true`; skipped in scenario mode.

### Set Voice

Hand-casting. `assignments` is one `Character Name = clip_id` per line (names are case-insensitive, `#` starts a comment). Checks each clip exists in the book's voicebank and each character exists in the casting, then sets `assigned_clip_id`, `locked` (default on) and `audit_note`. When the clip changes, a stale `voice_descriptor` is re-derived from the new clip for the Narrator and cleared for anyone else (s3 composes one) — the same rule as the Studio. Place it after Voice Cast / Audit and before the Director. Locked castings come through every later `voice cast` unchanged (over prior volumes, `--narrator-clip` and `--fresh`), and because the node sits downstream, re-running Voice Cast also re-applies your edits.

### Director (s3)

[`lnvox s3`](/stages/s3-director/). Inputs: `chapters`, `regen_profiles` (off — recast characters' profiles refresh on their own and their chapters are re-voiced; forcing it re-voices every chapter, so s4 re-renders the book), `fresh_directions` — delete the selected chapters' `03_directed/` files first to get fresh performance cues.

### Lecture (L), Scenario Direction (S)

[`lnvox lecture`](/stages/lecture/) (inputs `normalize`, `chapters`; with `normalize` off no LLM is needed) and [`lnvox scenario`](/stages/scenario/) (input `regen_profiles`, off by default for the same reason as the Director's).

### Stop LLM Server

Stops a server this pack started (never an external one) and waits 5 s for VRAM to be released.

### TTS (s4)

[`lnvox s4`](/stages/s4-tts/). Inputs: `backend` (dramabox / vibevoice), `staged` (on — the launcher default; Dramabox only), `device` (auto / cuda / mps / cpu), `chapters`, `limit` (smoke test: first N beats), `no_trim`, `max_retries` (30, monolithic and VibeVoice restart budget), `max_session_chars` (3000, VibeVoice), `keep_staged`, `prepare_env` (swap to the TTS / VibeVoice venv first), `free_comfy_vram`. It stops a still-running managed LLM server first. The staged path runs once (its driver retries internally); the others restart until clean like `s4_retry.sh`, appending to `artifacts/<book>/_s4_full.log`. On macOS it runs under `caffeinate`.

### Mix m4b (s5)

[`lnvox s5`](/stages/s5-mix/). Outputs `book`, `m4b_path`, `log`. `pacing`: `auto` uses 0.25 / 1 / 2 s and no lead-in for books, or the play pacing (inter-chapter 0.75 s, lead-in 0.35 s) in scenario mode; `custom` uses `intra`, `inter_scene`, `inter_chapter`, `lead_in`. Also `lufs` (−18), `kbps` (96), `cover`, `images_dir`, `chapters`. Mixes `05_audio_v2/` automatically after a VibeVoice render.

### EPUB Sync (s6)

[`lnvox s6`](/stages/s6-sync/) `--title <book title>`: s6 reads that mix's timings sidecar (pads, lead-in, the chapters mixed, the embedded pictures), so the sync matches the m4b the Mix node wrote. Input: `epub`. Skipped (with the reason on the node) for VibeVoice audio, a missing EPUB or missing audio — the launcher's rules.

### Scenario Sync (Y)

[`lnvox scenario-sync`](/stages/scenario-sync/) `--title <book title>`: laid out from the same timings sidecar as EPUB Sync, so the play timeline totals the m4b's duration.

### Run Pipeline (launcher)

`scripts/run_pipeline.sh` as one node — every launcher flag as an input (`llm_backend`, `tts_backend`, `llm_model`, `vllm_url`, `max_model_len`, `narrator_clip`, `audit_cast`, `stop_after_audit`, `no_normalize`, `staged_tts`, the `skip_*` flags, `max_retries`, `step_retries`); title, novels root, EPUB and scenario file come from the Book node. Use it when you want exactly the CLI's behaviour.

### Inspect

Renders a book's artifacts as text. `view`: `summary` (which stages are done), `characters`, `casting` (clip, its gender/age/accent, lock, runner-up candidates), `scenes`, `directed` (the exact prompts), `audio` (rendered vs directed beats per chapter), `final` (m4b, loudness, chapter table), `sync` (match rate and confidence histogram, or the play timeline), `voicebank`. `filter` narrows it: a chapter id, speaker or character name, or voicebank terms that must all match (`female adult england`). Outputs `text`.

### Load Audio

Decodes audio to ComfyUI's `AUDIO` for **Preview Audio** / **Save Audio**. `source`: `final_m4b` (optionally from `chapter_id`'s start, using the timings sidecar), `beat` (a beat id such as `02_s1_b0003`, or `chapter_id` + an index), `voicebank_clip` (a clip id — audition before casting), `file` (any path). Always windowed — `start_seconds`, `duration_seconds` (60; 0 = to the end, careful with whole books) — so a nine-hour m4b never lands in memory. Outputs `audio` and `path`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Could not find the ln-vox checkout" | Symlink (don't copy) the folder, or set `LNVOX_REPO` / the Book node's `repo_path` |
| `uv: command not found` in a node log | Put uv on the `PATH` ComfyUI starts with, or in one of the directories listed under [Install](#install) |
| "… calls the LLM. Put an 'ln-vox LLM Server' node before it" | Wire an LLM Server node upstream of that stage |
| LLM Server times out after 600 s | First start downloads the model; read `logs/comfyui_llm_<backend>.log`, then bump `rerun` |
| A stage "did nothing" after you edited a file | Cached: bump its `rerun` |
| A stage fails at once with "refused (exit 2)" | A deterministic refusal (bad option or knob, monolithic Dramabox on MPS, mistyped narrator clip) — retrying can't help; fix what the log names |
| s4 fails right away with an import error | Leave `prepare_env` on; if it persists, run `./scripts/setup_dramabox.sh` and see [troubleshooting](/guides/troubleshooting/) |
| Out of memory at s4 | Something else holds VRAM: another ComfyUI workflow's models (leave `free_comfy_vram` on) or an external LLM server |

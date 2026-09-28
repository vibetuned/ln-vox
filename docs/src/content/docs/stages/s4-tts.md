---
title: Stage 4 — TTS
description: Render directed beats to WAVs with Dramabox (staged or monolithic) or VibeVoice sessions - options, launcher routing, outputs, caching and failure modes.
sidebar:
  order: 11
  label: 4 · TTS
---

Stage 4 turns the directed beats from Stage 3 (or the lecture and scenario
stages) into audio. It writes one WAV per beat (Dramabox) or per multi-speaker
session (VibeVoice), plus a per-chapter `manifest.json` that Stage 5 mixes into
the m4b. It is the only GPU-heavy stage after the LLM phase and can take hours.
A content-hash cache makes re-runs after a single-line edit cheap, and each
render path has its own crash recovery.

There are three render paths:

| Path | Invocation | Engine | Crash recovery | Output |
|---|---|---|---|---|
| **Staged Dramabox** (launcher default) | `lnvox s4 <book> --staged` | Four single-model phases in subprocesses ([staged](/library/tts/staged/)) | Built-in per-phase relaunch with a stall limit ([driver](/library/tts/staged-driver/)) | `05_audio/` |
| **Monolithic Dramabox** | `lnvox s4 <book>` | One warm `TTSServer` ([DramaboxClient](/library/tts/dramabox-client/)) | External: `scripts/s4_retry.sh` | `05_audio/` |
| **VibeVoice sessions** | `lnvox s4 <book> --tts-backend vibevoice` | VibeVoice-Large ([client](/library/tts/vibevoice-client/)) | External: `scripts/s4_retry.sh` | `05_audio_v2/` |

## Command

```bash
uv run lnvox s4 <book_id> [--staged] [--tts-backend dramabox|vibevoice] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | Book id, e.g. `alice/volume-01`. Artifacts are read from `artifacts/<book_id>/`. |
| `--chapters TEXT` | all | Comma-separated chapter ids to render. |
| `--limit INTEGER` | none | Render at most N beats **overall**, across chapters (smoke-test mode). For VibeVoice it still counts beats, not sessions. |
| `--device TEXT` | auto | Torch device. Auto means `mps` when `torch.backends.mps.is_available()`, otherwise `cuda`. Auto never picks CPU; pass `cpu` to force it. |
| `--staged` | off | Use the crash-isolated staged pipeline (DESIGN.md §15). **Dramabox only**, and **required on MPS** for Dramabox. |
| `--keep-staged / --no-keep-staged` | off | Keep `05_audio/_staged/` intermediates after a successful staged run, for debugging. |
| `--tts-backend TEXT` | `$LNVOX_TTS_BACKEND`, else `dramabox` | `dramabox` (per beat, into `05_audio/`) or `vibevoice` (sessions, into `05_audio_v2/`). Case-insensitive. The flag wins over the env var. |
| `--max-session-chars INTEGER` | `3000` | VibeVoice only. Character cap per generation session. It trades cache granularity against prosody continuity (DESIGN.md §16.3). |
| `--no-trim` | off | Keep engine-emitted leading and trailing silence. It sets `LNVOX_S4_NO_TRIM=1` in the process environment, which the staged-phase subprocesses inherit, so one flag covers all three paths. |

Checks happen in this order and exit early. Exit 2 marks a deterministic
refusal, which `s4_retry.sh` and the launcher do not retry:

1. Unknown backend: exit 2.
2. `--staged` with vibevoice: exit 2, with the message
   `--staged is Dramabox-only (DESIGN.md §16.2)`.
3. Dramabox only: an invalid `LNVOX_S4_ENCODER_PRECISION`,
   `LNVOX_S4_CHUNK_CAP` or `LNVOX_S4_DECODE_DTYPE` value: exit 2, printing the
   reader's message.
4. Monolithic path only: the [env-knob refusal](#the-env-knob-refusal): exit 2.
5. `03_directed/` missing (run `lnvox s3`): exit 1.
6. `04_voice_assignments.json` missing (run `lnvox voice cast`): exit 1.
7. Empty voicebank (run `lnvox voice seed-cv`): exit 1.
8. No directed chapter matched `--chapters`: exit 1.
9. Monolithic path only, after the device is resolved: an MPS device is
   refused with exit 2 ([why](#monolithic-dramabox-on-mps)).

### Environment

| Variable | Default | Applies to | Effect |
|---|---|---|---|
| `LNVOX_TTS_BACKEND` | `dramabox` | all | Backend when `--tts-backend` is not given. |
| `LNVOX_S4_ENCODER_PRECISION` | `bf16` staged, `bnb4` monolithic | Dramabox | Gemma encoder checkpoint. Always `bf16` on MPS. See [render quality](/guides/render-quality/). |
| `LNVOX_S4_CHUNK_CAP` | `22` | staged | Denoise chunk cap in seconds. `0`, `none` or `off` restores the 45 s chunker on CUDA/CPU; MPS keeps 22 s. |
| `LNVOX_S4_DECODE_DTYPE` | `fp32` | staged | Vocoder dtype. `auto` uses the per-device dtype; MPS stays fp32. |
| `LNVOX_S4_STALL_LIMIT` | `10` | staged | Consecutive zero-progress phase restarts before the driver aborts. |
| `LNVOX_S4_NO_TRIM` | unset | all | `1` disables the edge-silence trim (same as `--no-trim`). |
| `LNVOX_VOICEBANK` | `voicebank` | all | Voicebank directory. |
| `LNVOX_VIBEVOICE_MODEL` | see [client](/library/tts/vibevoice-client/#_resolve_model_path) | VibeVoice | Checkpoint path or HF id. |

For the three `LNVOX_S4_ENCODER_PRECISION` / `CHUNK_CAP` / `DECODE_DTYPE` knobs, an empty value is the same as unset. The full list is in [environment](/reference/environment/).

### Hidden `s4-phase`

```bash
uv run lnvox s4-phase <refs|ctx|denoise|decode> <book_id> [--device DEVICE]
```

This is an internal command. The staged driver spawns it as
`python -m lnvox.cli s4-phase <phase> <book_id> --device <device>`. It runs one
phase sweep with [`staged.run_phase`](/library/tts/staged/#run_phase) against
the existing `05_audio/_staged/plan.json`, using `cache/tts`. An unknown phase
exits 2. `--device` defaults to auto. You can run it by hand to debug a single
phase, but only while a plan exists. `_staged/` is deleted after a successful
run, so that means after an aborted run or one made with `--keep-staged`.

### The env-knob refusal

The monolithic path delegates chunking and decoding to `TTSServer`, so
`LNVOX_S4_CHUNK_CAP` and `LNVOX_S4_DECODE_DTYPE` cannot apply there. Silently
ignoring them would render v1 audio under the key you asked for and poison any
A/B comparison. The code refuses **explicit** values instead:

```python title="src/lnvox/cli.py"
explicit = [
    name
    for name, val in (
        ("LNVOX_S4_CHUNK_CAP", chunk_cap),
        ("LNVOX_S4_DECODE_DTYPE", decode_dtype),
    )
    if os.environ.get(name, "").strip() and val is not None
]
if explicit and not staged:
    console.print(
        f"[red]{' / '.join(explicit)} need the staged path — re-run "
        "with --staged (LNVOX_S4_ENCODER_PRECISION works on both).[/]"
    )
    raise typer.Exit(2)
```

The rule has three consequences:

- Unset, empty or blank is fine.
- The revert values `LNVOX_S4_CHUNK_CAP=none`, `0` or `off`, and
  `LNVOX_S4_DECODE_DTYPE=auto`, are accepted, because they describe what the
  monolithic path does anyway.
- Any other valid value is refused with exit 2. Invalid values were already
  refused with exit 2 by the validation step before it.

`LNVOX_S4_ENCODER_PRECISION` works on both paths. The VibeVoice path skips both
checks and ignores all three knobs.

### Monolithic Dramabox on MPS

`TTSServer` builds every component in one dtype, and no single dtype works on
Metal: the fp16 Gemma encoder returns all-NaN encodings, which renders silent
audio with exit code 0 (MAC_TEST_REPORT.md F10), and the vocoder needs fp32
(F9). So `lnvox s4` without `--staged` on an MPS device prints

```text
Monolithic Dramabox can't render on MPS (…). Re-run with --staged — the launcher default; LNVOX_S4_STAGED=0 is what selects this path.
```

and exits 2 before placing or loading anything. `DramaboxClient` raises the
same way if anything else constructs it on MPS. CUDA and CPU are unaffected,
and so is VibeVoice on MPS.

## In the launcher

`scripts/run_pipeline.sh` runs Stage 4 in every mode (narration, lecture,
scenario) after it has stopped the LLM server. It waits 5 s first, unless you
passed `--vllm-url`, so VRAM is released. `--skip-tts` skips the stage
entirely. Routing is decided from three variables:

```bash title="scripts/run_pipeline.sh"
STAGED_TTS="${LNVOX_S4_STAGED:-1}"
# Records whether staging was *asked for* (env var set or flag passed), as
# opposed to inherited from the default above — vibevoice ignores the
# default silently but rejects an explicit request (DESIGN.md §16.2).
STAGED_TTS_EXPLICIT="${LNVOX_S4_STAGED+1}"
TTS_BACKEND="${LNVOX_TTS_BACKEND:-dramabox}"
# …
        --staged-tts)    STAGED_TTS=1; STAGED_TTS_EXPLICIT=1; shift ;;
        --tts-backend)   TTS_BACKEND="$2";  shift 2 ;;
# …
if [ "$TTS_BACKEND" = "vibevoice" ] && [ -n "${STAGED_TTS_EXPLICIT:-}" ] && [ "$STAGED_TTS" = "1" ]; then
    echo "ERROR: --staged-tts is Dramabox-only (DESIGN.md §16.2); vibevoice uses the monolithic path + retry loop." >&2
    usage 2
fi
```

Then the TTS phase:

```bash title="scripts/run_pipeline.sh"
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        prepare_vibevoice_env
        banner "Stage 4: TTS (VibeVoice sessions → 05_audio_v2, with auto-retry)"
        MAX_ATTEMPTS="$MAX_RETRIES" LNVOX_TTS_BACKEND=vibevoice ./scripts/s4_retry.sh "$BOOK_ID"
        check_s4_retry $?
    elif [ "$STAGED_TTS" -eq 1 ]; then
        prepare_tts_env
        banner "Stage 4: TTS (Dramabox, staged phases)"
        uv run lnvox s4 "$BOOK_ID" --staged
        s4_rc=$?
        if [ "$s4_rc" -eq 2 ]; then
            echo "ERROR: staged s4 refused its input (exit 2) — fix the arguments / configuration above and re-run." >&2
            exit 2
        elif [ "$s4_rc" -ne 0 ]; then
            echo "ERROR: staged s4 failed — see the stall diagnostics above." >&2
            exit 1
        fi
    else
        prepare_tts_env
        banner "Stage 4: TTS (Dramabox, with auto-retry)"
        MAX_ATTEMPTS="$MAX_RETRIES" ./scripts/s4_retry.sh "$BOOK_ID"
        check_s4_retry $?
    fi
```

`check_s4_retry` exits the launcher with `s4_retry.sh`'s status when it is not
0 (1 after the retry budget, 2 on a refusal, or the signal status), printing
`s4 failed (s4_retry.sh exit N) — not mixing a partial render`, so a partial
render is never mixed. The staged branch does the same with its own status:
2 for a refusal, 1 for anything else.

| Backend | `LNVOX_S4_STAGED` / `--staged-tts` | Result |
|---|---|---|
| `dramabox` | unset (defaults to `1`) | **Staged**: `uv run lnvox s4 "$BOOK_ID" --staged`, with no outer retry. A refusal exits the launcher with code 2, any other failure with code 1. |
| `dramabox` | `--staged-tts`, or `LNVOX_S4_STAGED=1` | Staged, same as above. |
| `dramabox` | `LNVOX_S4_STAGED=0` | **Monolithic** under `s4_retry.sh` (`MAX_ATTEMPTS=$MAX_RETRIES`, default 30, set with `--max-retries`). On a Mac this is refused with exit 2 and the launcher stops. |
| `vibevoice` | unset, or `LNVOX_S4_STAGED=0` | **VibeVoice** under `s4_retry.sh` with `LNVOX_TTS_BACKEND=vibevoice`. The staged default is silently ignored. |
| `vibevoice` | `--staged-tts`, or `LNVOX_S4_STAGED=1` | **Error** at argument parsing (usage, exit 2), before any stage runs. |

Stage 4 is not wrapped in `run_step` (neither `run_step ensure_vllm …` nor
`run_step - …`). The staged path owns its retries, and the other two paths use
`s4_retry.sh`. There is no `--no-staged-tts` flag; `LNVOX_S4_STAGED=0` is the
only way to select the monolithic path from the launcher. The launcher never
passes `--device`, `--limit`, `--chapters` or `--no-trim`. Export
`LNVOX_S4_NO_TRIM=1` (or the other `LNVOX_S4_*` knobs) to affect a launcher run.
Stage 5 gets `--v2` automatically for VibeVoice, and Stage 6 and scenario sync
are skipped for VibeVoice audio.

### `scripts/s4_retry.sh`

```bash title="scripts/s4_retry.sh"
    uv run lnvox s4 "$BOOK" 2>&1 | tee -a "$LOG"
    ec="${PIPESTATUS[0]}"
    if [ "$ec" -eq 0 ]; then
        # … Completed
        exit 0
    elif [ "$ec" -eq 2 ]; then
        # … deterministic refusal, not retrying
        exit 2
    # … anything else: retry after RETRY_DELAY
```

The script reruns `uv run lnvox s4 "$BOOK"`, with no other flags, until it
exits 0. Exit 2 (a deterministic refusal, such as a bad knob or the MPS
refusal) stops it at once with status 2. Any other failure is retried after
`RETRY_DELAY` seconds (default 5), up to `MAX_ATTEMPTS` (100 when standalone,
`$MAX_RETRIES` under the launcher), and then it exits 1. Every attempt is
appended to `$LNVOX_ARTIFACTS_DIR/<book>/_s4_full.log` (default
`artifacts/`). Cached beats skip immediately, so each restart resumes, but
each restart also pays the full model boot. Options can only reach it through
the environment (`LNVOX_TTS_BACKEND`, `LNVOX_S4_*`). See also
[helper scripts](/launcher/helper-scripts/).

### Environment preparation

The LLM and TTS phases need incompatible torch builds. Each TTS path therefore
re-prepares the venv first (see [launcher functions](/launcher/functions/)):

- **`prepare_tts_env`** (Dramabox):
  1. `uv sync --extra voice --extra tts`.
  2. `install_dramabox_reqs`, which is platform-aware:
     - Darwin strips torch and bitsandbytes and installs `torch>=2.10,<2.12`.
     - x86_64 keeps Dramabox's torch 2.8 pin and installs prebuilt
       `mamba-ssm` / `causal-conv1d` wheels (`cu12torch2.8`).
     - aarch64 installs `torch==2.10.*` from the cu130 index plus
       `cu13torch2.10` kernel wheels.
  3. `uv pip install "transformers>=4.52"`, which undoes a VibeVoice pin.
  4. A sanity probe: MPS on Darwin; elsewhere CUDA, plus imports of
     `causal_conv1d_cuda` and `selective_scan_cuda`.
- **`prepare_vibevoice_env`**:
  1. `uv sync --extra voice --extra tts`.
  2. Requires `external/VibeVoice` (run `scripts/setup_vibevoice.sh`).
  3. `uv pip install -e external/VibeVoice`, which pins `transformers==4.51.3`.
  4. The same MPS or CUDA probe.

:::tip[Running s4 by hand]
Prepare the right venv before calling `lnvox s4` directly. The launcher does it
for you, and `./scripts/run_pipeline.sh --prepare-env tts` (or `vibevoice`) runs
only that preparation step and exits, with no book id needed.
:::

## Inputs and outputs

**Reads**

- `artifacts/<book>/03_directed/*.json`: directed beats (`ChapterDirected`).
  Each beat's `prompt` is what Dramabox renders; VibeVoice uses `text`.
- `artifacts/<book>/04_voice_assignments.json`: `BookCasting`, character to
  clip id.
- `$LNVOX_VOICEBANK/manifest.json` and the clip WAVs it references.

**Writes**

```text
artifacts/<book>/05_audio/<chapter_id>/<beat_id>.wav        # Dramabox (both paths), trimmed
artifacts/<book>/05_audio/<chapter_id>/manifest.json        # ChapterAudio
artifacts/<book>/05_audio/_staged/                          # staged only; removed on success unless --keep-staged
artifacts/<book>/05_audio_v2/<chapter_id>/<session_id>.wav  # VibeVoice, 48 kHz stereo, trimmed
artifacts/<book>/05_audio_v2/<chapter_id>/manifest.json
artifacts/<book>/_s4_full.log                               # s4_retry.sh only
cache/tts/<cache_key>.wav                                   # content cache, relative to the working directory
```

`beat_id` is `<scene_id>_b<NNNN>`, and `session_id` is `<scene_id>_v<NNN>`.
The manifest format is described in [schema](/library/tts/schema/#manifestjson)
and the full artifact tree in [artifacts](/reference/artifacts/).

**Downloads, one-time, into the HF cache**

- Dramabox DiT and audio components.
- `unsloth/gemma-3-12b-it-bnb-4bit`.
- `nvidia/RE-USE`.
- With the default staged recipe, the unquantized `unsloth/gemma-3-12b-it`
  (about 23 to 24 GB).
- VibeVoice-Large (about 18 GB), which `setup_vibevoice.sh` puts in
  `models/VibeVoice-Large/`.

## How it works

Everything starts in `cli.stage4`. It validates the options, loads the
voicebank, casting and directed chapters, resolves the device, and dispatches:

- **VibeVoice:** `cli.stage4` →
  [`s4_vibevoice.run`](/library/stages/s4-vibevoice/#run) →
  [`plan_chapter`](/library/stages/s4-vibevoice/#plan_chapter) →
  [`render_chapter`](/library/stages/s4-vibevoice/#render_chapter) →
  [`VibeVoiceClient.generate_session`](/library/tts/vibevoice-client/#generate_session)
  → `_save_normalized` (trim, resample to 48 kHz, stereo).
- **Staged:** `cli.stage4` →
  [`staged_driver.run_staged`](/library/tts/staged-driver/#run_staged) →
  [`build_plan`](/library/tts/staged/#build_plan), then for each phase a
  subprocess running `lnvox s4-phase` → `cli.stage4_phase` →
  [`staged.run_phase`](/library/tts/staged/#run_phase) → `_phase_refs` /
  `_phase_ctx` / `_phase_denoise` / `_phase_decode` →
  [`_finalize`](/library/tts/staged/#_finalize) (place, trim, manifest).
- **Monolithic:** `cli.stage4` → env-knob refusal → MPS refusal →
  [`s4_tts.run`](/library/stages/s4-tts/#run) →
  [`render_chapter`](/library/stages/s4-tts/#render_chapter) →
  [`DramaboxClient.generate`](/library/tts/dramabox-client/#generate) →
  `TTSServer.generate_to_file`, then [`trim_file`](/library/tts/trim/#trim_file).

The Dramabox steps, per beat:

1. Map the speaker to their assigned voicebank clip, or to no ref at all
   (descriptor-only).
2. Compute `cache_key = _content_hash(prompt, clip filename or "no-ref", MODEL_VERSION + variant tag)`,
   a `\x1f`-separated SHA-256 truncated to 16 hex chars
   ([details](/library/stages/s4-tts/#_content_hash)).
3. On a cache hit, copy the cached WAV.
4. Otherwise render. The staged path does this as four sweeps across the whole
   book:
   - ref VAE latents, once per unique clip;
   - Gemma encodings, once per chunk, plus the negative prompt once per book;
   - the 30-step DiT denoise per chunk;
   - decode, crossfading chunks back into beats.
5. Trim the edge silence, measure the duration, and write the chapter manifest.

The staged path splits long beats into chunks of at most 22 s by default. The
monolithic path uses `TTSServer`'s internal 45 s chunking.

Per-device behaviour:

- **CUDA:** the full staged recipe. The monolithic path uses `TTSServer`
  defaults (bf16, bnb-4bit encoder, `torch.compile`).
- **MPS:** staged only; the monolithic path exits 2. It uses a
  per-component dtype map (encoder bf16, DiT fp16 eager, vocoder fp32) and a
  forced 22 s / 18 s chunk cap
  ([details](/library/tts/staged/#per-component-dtype-map)). The revert knobs
  cannot take effect there, so they do not change the cache key either. See
  [Apple Silicon](/guides/apple-silicon/).
- **CPU:** treated like CUDA by the knob tables. It works in principle but is
  impractically slow, and it is untested for Dramabox.

Throughput rules of thumb: a warm 4090 runs at roughly real-time to 2x
real-time (DESIGN.md §2.6). An M4 Max on the staged path needs about 4 h of
render per hour of audio (MAC_TEST_REPORT.md).

## Re-running and caching

- **Resume is simply re-running.** All paths skip beats whose
  `cache/tts/<key>.wav` exists. The staged path also reuses per-chunk
  intermediates under `_staged/`, because their names are content-keyed.
- **What re-renders:**
  - an edited beat prompt re-renders that beat (or its VibeVoice session);
  - a recast character re-renders that character's beats (the clip filename is
    in the key);
  - a changed render recipe or `MODEL_VERSION` re-renders **everything**;
  - switching between the staged and monolithic defaults re-renders everything,
    because their variant tags differ. On MPS the tag describes the
    effective recipe, so the revert knobs (which MPS cannot honour) re-render
    nothing.

  Keep one recipe per book ([render quality](/guides/render-quality/)).
- **What does not re-render:** editing a clip file in place under the same
  name, and toggling `--no-trim`. To force a speaker's beats to re-render, use
  `lnvox audio purge <book> --speaker <name>` ([utilities](/stages/utilities/)).
- **Fully cached runs never load a model.** The monolithic and VibeVoice paths
  construct their client on the first cache miss. The staged driver still runs
  decode once, which places the beats, trims them and writes the manifests
  without loading any model.
- The staged plan line
  `Staged s4 plan: N beats → M unique renders (K already cached), …` is the
  quickest check that a re-run is reusing the cache. `K = 0` on a book that was
  already rendered means the cache key changed. The next line,
  `Render recipe: <model_version>`, shows the key's recipe.
- **Placement follows the manifest.** The staged path re-copies every placed
  WAV that is not an exact copy of the cache entry its manifest names, so a
  re-render, a switch back to an earlier recipe or casting, or a monolithic run
  in between never leaves other audio in `05_audio/`.
- `--limit` and `--chapters` runs rewrite the manifests of the chapters they
  touch, with only the beats they rendered. Run a full pass before mixing.
- `cache/tts/` grows without bound. Entries left from old recipes are never
  reused but are not deleted either.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Staged s4 phase 'X' made no progress across 10 consecutive restarts; stuck on item …` | A deterministic crasher, for example MPS F11 on an over-long chunk, an OOM, or a bad clip. | Investigate the named item (`_staged/attempts.json` and `inflight.json`), then re-run `lnvox s4 <book> --staged`. Raise `LNVOX_S4_STALL_LIMIT` only for transient faults. |
| Exit 0, valid m4b, all silence (file far too small, `volumedetect` near -91 dB) | NaN conditioning, e.g. an fp16 Gemma on MPS (F10). The monolithic path, which had this exposure on MPS, now refuses MPS. | Always check the output level after a device or dtype change; exit code 0 is not proof of audio. |
| `Monolithic Dramabox can't render on MPS` (exit 2) | `lnvox s4` without `--staged` on a Mac, e.g. `LNVOX_S4_STAGED=0` in the launcher. | Use `--staged` (the launcher default). |
| `N cast character(s) reference clip ids that don't exist in the voicebank …` | The casting was made against another voicebank. | Point `LNVOX_VOICEBANK` at the right bank. |
| `LNVOX_S4_CHUNK_CAP … need the staged path` (exit 2) | Explicit staged-only knobs on the monolithic path. | Add `--staged`, or unset the knobs. `s4_retry.sh` does not retry it. |
| `LNVOX_S4_… must be …` (exit 2) | An invalid knob value. | Fix or unset the variable. |
| `--staged is Dramabox-only` | `--staged` combined with vibevoice. | Drop `--staged`. |
| OOM while loading the monolithic path | `LNVOX_S4_ENCODER_PRECISION=bf16` puts about 23 GB of encoder next to the DiT and vocoder. | Use `--staged`, or `bnb4`. |
| `Dramabox not found at …/external/DramaBox` | Setup was skipped. | Run `./scripts/setup_dramabox.sh`. |
| `The vibevoice package is not installed in this venv` | VibeVoice env not prepared. | Run `./scripts/setup_vibevoice.sh`, or let the launcher's `prepare_vibevoice_env` do it. |
| Dramabox Gemma errors right after a VibeVoice run | `transformers==4.51.3` is still pinned in the venv. | Run `prepare_tts_env` (the launcher does), or `uv pip install "transformers>=4.52"`. |
| `stuck on item placement <chapter>/…` | Placing beats or writing a manifest keeps failing, e.g. a full or read-only disk. The driver re-runs decode until the placements verify. | Fix the disk problem, then re-run `lnvox s4 <book> --staged`. |

When `s4_retry.sh` gives up or refuses, the launcher stops with its exit
status (`check_s4_retry`) instead of mixing a partial render; the staged path
already exits the launcher on failure.

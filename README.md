# ln-vox

A self-hosted pipeline that turns a text novel into a multi-voice audiobook
(`.m4b`) with character-appropriate voices, emotionally-acted delivery, and
chapter-marker navigation. Runs on a single workstation with one consumer
GPU (24 GB+ VRAM recommended).

See [DESIGN.md](DESIGN.md) for the full architecture; this README focuses on
how to actually run the pipeline.

---

## Pipeline at a glance

```
ingest → s1 cast → s2 scenes → voice cast → s3 director → s4 tts → s5 mix → .m4b
            ↑                       ↑                        ↑          ↑
       prior volume's          prior volume's          Dramabox    ffmpeg
       cast (merged)         clips (reused) +           (GPU)     loudnorm
                              --narrator-clip                       + AAC
```

Two GPU phases that **cannot run simultaneously** (they each want most of
the VRAM):
- **vLLM phase** — Gemma 4 serves s1, s2, voice cast, s3.
- **Dramabox phase** — local TTS serves s4.

Stage 5 runs on CPU/ffmpeg only.

---

## One-time setup

```bash
# 1. Python deps
uv sync --extra serve --extra voice --extra tts

# 2. Clone & install Dramabox
./scripts/setup_dramabox.sh

# 3. Download Mozilla Common Voice (the EN tarball, ~96 GB)
#    https://commonvoicedata.mozilla.org/ → download → extract to ./data/

# 4. Seed the voicebank (~30 min for 400 speakers, CPU/ffmpeg work)
uv run lnvox voice seed-cv \
    data/<cv-corpus-NN.N-YYYY-MM-DD>/en/ \
    --max-speakers 400

# 5. Browse the seeded voicebank
uv run lnvox voice list
```

Notes:
- Python is pinned to 3.13 via `.python-version`. uv will install it if you
  don't already have it.
- `serve` pulls `vllm>=0.19` + `torch` from PyTorch's cu130 wheel index.
  Replace with the appropriate cu* version for your driver if needed.
- Dramabox auto-downloads ~15 GB of weights from HuggingFace on first run.
- The voicebank seed only needs to be done once per language. Re-running it
  with a higher `--max-speakers` adds more speakers to the existing bank.

---

## Project layout for novels

Books live under `novels/<series>/volume-NN/`, one `.txt` per chapter
ordered by filename prefix. Example:

```
novels/
└── novel-name/
    ├── volume-01/
    │   ├── 01-prologue.txt
    │   ├── 02-chapter-1.txt
    │   ├── …
    │   └── 07-afterword.txt
    └── volume-02/
        ├── 01-prologue.txt
        ├── …
```

The first non-empty line of each file is taken as the chapter title.

---

## Running the pipeline

### One-shot launcher

```bash
# Volume 1 of a series — narrator clip required (or auto-cast acceptable):
./scripts/run_pipeline.sh novel-name/volume-01 \
    --narrator-clip cv_051e865815e5 \
    --book-title "A Certain Magical Index — Volume 1"

# Volume 2 — narrator is inherited from volume-01, no flag needed:
./scripts/run_pipeline.sh novel-name/volume-02 \
    --book-title "A Certain Magical Index — Volume 2"
```

The launcher manages vLLM and Dramabox transparently:
1. **Starts vLLM** in the background (waits for the `/v1/models` endpoint).
2. Runs ingest → s1 → s2 → voice cast → s3 against the local vLLM.
3. **Stops vLLM** to free the GPU.
4. Runs s4 (Dramabox, with auto-retry) → s5 (mix to .m4b).

Set `LNVOX_NO_PROMPT=1` for fully non-interactive runs.

If you already have a vLLM serving (e.g. on a separate GPU), pass
`--vllm-url http://host:8000/v1` to skip auto-start..gitignore
often OOMs.** The 31B path is best for:
- DGX-class machines (DGX Spark / H100 / B100) with ≥48 GB VRAM, or
- Headless servers where no other process touches the GPU.

On a roomy machine you can also bump `--max-model-len` higher:

```bash
# DGX Spark or similar with abundant VRAM
./scripts/run_pipeline.sh novel-name/volume-01 \
    --llm-model "nvidia/Gemma-4-31B-IT-NVFP4" \
    --max-model-len 65536 \
    --narrator-clip cv_051e865815e5
```

### Picking the narrator

Run `lnvox voice list` after seeding to see the per-bucket distribution,
pick a `cv_<id>` whose gender / age / accent fits the narrator style you
want, and pass it via `--narrator-clip` on the FIRST volume only.

Subsequent volumes auto-reuse the prior volume's narrator clip — you can
omit `--narrator-clip` entirely. Pass it again only when you intentionally
want to change the narrator mid-series.

### Stage-by-stage (advanced / re-runs)

Every stage is an idempotent CLI; re-running with the same inputs reproduces
outputs.

| Stage | Command | Inputs | Outputs |
|---|---|---|---|
| 0 | `lnvox ingest novels/novel-name/volume-01` | `.txt` files | `00_text.jsonl` |
| 1 | `lnvox s1 novel-name/volume-01` | `00_text.jsonl` (+ prior `01_characters.json`) | `01_characters.json` |
| 2 | `lnvox s2 novel-name/volume-01` | `00_text.jsonl` + `01_characters.json` | `02_scenes/*.json` |
| V | `lnvox voice cast novel-name/volume-01 --narrator-clip cv_…` | `01_characters.json` + voicebank | `04_voice_assignments.json` |
| 3 | `lnvox s3 novel-name/volume-01 --regen-profiles` | `02_scenes/*.json` + `04_voice_assignments.json` | `03_directed/*.json` + `03_voice_profiles.json` |
| 4 | `./scripts/s4_retry.sh novel-name/volume-01` | `03_directed/*.json` + `04_voice_assignments.json` | `05_audio/<ch>/*.wav` |
| 5 | `lnvox s5 novel-name/volume-01 --title "…"` | `05_audio/<ch>/*.wav` | `06_final/<title>.m4b` |

---

## Cross-volume continuity

Drop volume-02 next to volume-01 and re-run the launcher:

```bash
./scripts/run_pipeline.sh novel-name/volume-02 \
    --book-title "A Certain Magical Index — Volume 2"
```

The pipeline auto-detects `artifacts/novel-name/volume-01/` and:
- Adds volume-01's `01_characters.json` to s1's merge step so recurring
  characters keep their established affiliation / origin / personality.
- Loads volume-01's `04_voice_assignments.json` and skips voice-casting for
  any character whose canonical name matches a prior assignment.
- If `--narrator-clip` is omitted, the prior volume's narrator clip is
  reused (so the listener hears the same narrator across the series).

To intentionally change a recurring voice (e.g. a character's casting was
wrong in volume-01), edit the new volume's `04_voice_assignments.json`
manually after voice cast runs, then re-run from s3.

---

## Operational notes

### GPU handoff

Both Gemma (vLLM) and Dramabox want most of the GPU's VRAM. Always:
- **Stop vLLM before starting Dramabox**, and vice versa.
- `nvidia-smi` to confirm the GPU is idle before starting the next phase.
- `set -o pipefail` if you write your own bash wrappers — `tee` masks
  upstream failures otherwise.

### s4 stability

Long Dramabox runs (10+ min continuous denoising) sometimes SIGKILL. Cause
appears to be CUDA memory fragmentation on RTX 50-series. **Use
`scripts/s4_retry.sh`**, never `lnvox s4` directly. The content-hash cache
ensures every restart is a near-zero-cost resume from the last successful
beat.

### Beat length

Empirically Dramabox sounds best on 20–60 s beats (~250–700 chars). The
Director's merge pass caps at ~500 chars. If a source narration paragraph
is itself longer than that, it's auto-split at sentence boundaries before
TTS.

### Disk

Plan for:
- ~96 GB for the Common Voice tarball
- ~500 MB per book for the voicebank's selected clips
- ~600 MB of WAVs per hour of rendered audio in `artifacts/<book>/05_audio/`
- ~50 MB per hour of audio in the final `.m4b`

Cache (`cache/tts/`) accumulates indefinitely — clear it periodically if
disk gets tight, but understand that every entry is a re-render saver.

### Personal-use disclaimer

Common Voice itself is CC-0 so the seeded voicebank is fine to publish.
**Cloning identifiable real people** (e.g. via the planned YouTube ref-clip
pipeline) is for personal use only — never distribute audiobooks rendered
on cloned-from-living-people references without explicit consent.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Dramabox not found at external/DramaBox` | Setup step skipped | `./scripts/setup_dramabox.sh` |
| `FileNotFoundError: ltx-2.3-22b-dev.safetensors` | Old Dramabox config path baked in | Update `DramaboxClient` to use `model_downloader.get_all_paths()` (already done in `src/lnvox/tts/dramabox_client.py`) |
| `Python.h not found` during JIT compile | System Python lacks dev headers | `sudo apt install python3.13-dev` OR use uv-managed Python |
| `BadRequestError: maximum context length` from vLLM | `LNVOX_LLM_MAX_LEN` too low | `LNVOX_LLM_MAX_LEN=131072 ./scripts/serve_vllm.sh` |
| `httpx.ReadTimeout` mid-s2 with a slow model | Default timeout assumes >20 tok/s; 31B on DGX Spark is ~6 tok/s, so a 28k-token s2 chunk runs ~80 min and the connection drops | Bump `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN` — default 0.25 gives ~7000s for a 28k-token call. For very slow hosts use 0.35–0.5. (Per-call timeout = `timeout_base_seconds + max_tokens × timeout_seconds_per_token`.) |
| `AssertionError: Torch not compiled with CUDA enabled` on DGX Spark / Jetson | aarch64 platform; Dramabox's pinned `torch==2.8.0` only has CPU-only aarch64 wheels | Re-run `./scripts/setup_dramabox.sh` — it auto-detects aarch64 and pulls `torch>=2.10+cu130` (which **does** ship aarch64+sbsa CUDA wheels) instead. x86_64 is unaffected. |
| s4 crashes after 10–15 min | CUDA fragmentation | Use `scripts/s4_retry.sh` (auto-resumes from cache) |
| Narrator voice doesn't match descriptor | Stage order pre-dates the v2 fix | Re-run s3 with `--regen-profiles` AFTER voice cast |
| Empty `accent` distribution in voicebank | TSV had pipe-separated accents | Fix `_normalize_accent` (already in `voices/common_voice.py`); re-seed |
| Dramabox renders sound rushed | Long beats (>60 s) | Lower `MAX_MERGED_BEAT_CHARS` in `s3_director.py`, re-run s3 |

---

## File map cheat sheet

```
artifacts/<series>/<volume-NN>/
├── 00_text.jsonl                        Ingested chapters (one JSON line each)
├── 01_characters.json                   Merged book cast
├── 01_characters_per_chapter/*.json     Pre-merge per-chapter casts
├── 02_scenes/*.json                     Scene/beat segmentation
├── 03_voice_profiles.json               Per-character voice descriptors
├── 03_directed/*.json                   Dramabox-ready beat prompts
├── 04_voice_assignments.json            Character → ref clip mapping
├── 05_audio/<chapter>/                  Rendered beat WAVs + manifest.json
└── 06_final/<title>.m4b                 Final audiobook + timings.json

voicebank/
├── manifest.json                        Indexed voice clips
└── clips/cv_<id>.wav                    Reference clips (10–20 s each)

cache/tts/<sha256>.wav                   Content-addressed TTS cache (survives book deletions)

external/DramaBox/                       Cloned Dramabox repo (sys.path-injected)
data/<cv-corpus-…>/                      Raw Common Voice extraction
```

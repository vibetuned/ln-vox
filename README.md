# Lectures and Narrations

Lectures and Narrations (LN) This project was born out of a small AI experiment in book understanding and knowledge presentation. By using language models to analyze narrative structures, identify character traits, and map scene dynamics, it quickly became clear that this deep-text comprehension could be repurposed. The experiment naturally evolved into a complete, end-to-end pipeline for creating highly immersive, multi-voice audiobooks.

Today, the Lectures and Narrations ecosystem consists of two halves:

> ln-vox (The Creator): An offline, self-hosted AI pipeline that ingests raw text or EPUBs. It orchestrates LLMs to cast characters and direct emotional delivery, then uses local TTS to generate a fully acted, multi-voice .m4b audiobook complete with text-to-audio sync markers.

> ln-reader (The Consumer): A custom Android application built specifically to play these enhanced audiobooks. It features chapter-relative scrubbing, sleep timers, and an EPUB companion reader that automatically highlights the text in sync with the audio.

## ln-vox in a nutshell

A self-hosted pipeline that turns a text novel into a multi-voice audiobook
(`.m4b`) with character-appropriate voices, emotionally-acted delivery, and
chapter-marker navigation. Runs on a single workstation with one consumer
GPU (24 GB+ VRAM recommended).

See [DESIGN.md](DESIGN.md) for the full architecture; this README focuses on
how to actually run the pipeline. The in-depth reference — every stage, flag,
environment variable and artifact — is the documentation site in `docs/`
(Astro Starlight): `cd docs && npm install && npm run dev`. To drive the
stages as a node graph instead of the launcher, there's a ComfyUI node pack in
`comfyui/` (docs site: *Guides → ComfyUI nodes*).

---

## Pipeline at a glance

```
ingest → s1 cast → s2 scenes → voice cast → s3 director → s4 tts → s5 mix → .m4b
                                    ↑                        ↑          ↑
                               prior volumes'          Dramabox    ffmpeg
                             clips (reused) +           (GPU)     loudnorm
                              --narrator-clip                       + AAC
```

Two GPU phases that **cannot run simultaneously** (they each want most of
the VRAM):
- **LLM phase** — Gemma 4 serves s1, s2, voice cast, s3. Default backend is
  **llama.cpp** (`google/gemma-4-12B-it-qat-q4_0-gguf`, 64k context);
  vLLM and mlx are one `--llm-backend` flag away.
- **Dramabox phase** — local TTS serves s4.

Stage 5 runs on CPU/ffmpeg only.

> **Non-fiction?** For technical / reference books there's a single-voice
> **lecture mode** that reads the text as written (no dramatization) and shows
> code/tables/figures as images instead of narrating them. Jump to
> [Lecture mode](#lecture-mode-non-fiction--technical-books).
>
> **Theater scripts?** For plays there's a **scenario mode** that keeps every
> line verbatim, renders a full-cast performance, and emits a timed **sync
> file** per scene (who says what, when, with which emotion) — plus the
> sound/light cues on the same timeline. Jump to
> [Scenario mode](#scenario-mode-theater-scripts--timed-sync--full-cast-audio).

---

## One-time setup

```bash
# 1. Python deps
uv sync --extra voice --extra tts

# 2. Install llama.cpp (the default LLM backend — a native binary)
#    macOS:  brew install llama.cpp
#    Linux:  build from https://github.com/ggerganov/llama.cpp (CUDA build)
#    The default model (google/gemma-4-12B-it-qat-q4_0-gguf, ~8 GB) is
#    auto-downloaded by llama-server on first run.

# 3. Clone & install Dramabox
./scripts/setup_dramabox.sh

# 4. Download Mozilla Common Voice (the EN tarball, ~96 GB)
#    https://commonvoicedata.mozilla.org/ → download → extract to ./data/

# 5. Seed the voicebank (~30 min for 400 speakers, CPU/ffmpeg work)
uv run lnvox voice seed-cv \
    data/<cv-corpus-NN.N-YYYY-MM-DD>/en/ \
    --max-speakers 400

# 6. Browse the seeded voicebank
uv run lnvox voice list
```

Notes:
- Python is pinned to 3.13 via `.python-version`. uv will install it if you
  don't already have it.
- **LLM backend default is llama.cpp** with
  `google/gemma-4-12B-it-qat-q4_0-gguf` at a 65,536-token context — validated
  across narration, lecture and scenario runs. `--llm-backend vllm` (Linux,
  needs `uv sync --extra serve`, pulls `vllm>=0.19` + cu130 torch) and
  `--llm-backend mlx` (Apple Silicon, `--extra mlx`) remain available; pick a
  model with `--llm-model`.
- Dramabox auto-downloads ~15 GB of weights from HuggingFace on first run,
  plus a ~23 GB unquantized Gemma encoder on the first s4 render (the
  default high-quality recipe — see
  [Render quality](#render-quality-defaults-and-the-lighter-fallback)).
- The voicebank seed only needs to be done once per language. Re-running it
  with a higher `--max-speakers` adds more speakers to the existing bank.

---

## macOS (Apple Silicon) setup

The full pipeline runs on an M-series Mac — validated end-to-end on an
M4 Max / 64 GB (`MAC_TEST_REPORT.md` has the whole story; DESIGN.md §11 is
the contract for what differs). Linux + CUDA remains the primary target.

```bash
# 1. Tooling
brew install ffmpeg          # s5 mix
brew install llama.cpp       # default LLM backend, same as Linux

# 2. Python deps (the mlx extra adds the Apple-native LLM backend)
uv sync --extra mlx --extra voice --extra tts

# 3. Dramabox — the Darwin branch installs MPS-capable torch and drops
#    bitsandbytes (CUDA-only)
./scripts/setup_dramabox.sh

# 4. Voicebank: copy it from your main machine instead of re-seeding
#    (seeding needs the ~96 GB Common Voice tarball)
rsync -a <linux-box>:projects/ln-vox/voicebank/ voicebank/
```

Then run the launcher exactly as on Linux — llama.cpp is the default LLM
backend on every platform. To serve the LLM stages Apple-natively instead,
add `--llm-backend mlx`:

```bash
./scripts/run_pipeline.sh novel-name/volume-01 \
    --llm-backend mlx \
    --narrator-clip cv_… --book-title "…"
```

What the Mac path does automatically:

- **s4 auto-detects MPS** and, on the staged path (the launcher default),
  applies the working per-phase dtype map: Gemma prompt-encoder in bf16
  (fp16 overflows to NaN → silent audio), DiT in fp16, vocoder in fp32. The
  first run downloads the **unquantized** encoder checkpoint (~23 GB) because
  the bnb-4bit one is CUDA-only. Monolithic s4 (`lnvox s4` without
  `--staged`, or `LNVOX_S4_STAGED=0` in the launcher) can't apply that map, so
  it refuses MPS with exit 2 instead of rendering silence.
- **Render chunks are capped at 22 s** — originally an MPS necessity (a
  boolean-indexing bug breaks the DiT mask on longer beats), now the
  default everywhere after blind-testing *better* than long chunks; see
  [Render quality](#render-quality-defaults-and-the-lighter-fallback).
- **The launcher re-execs under `caffeinate -dis`** so multi-hour renders
  survive macOS power management.

mlx backend notes:

- Default model: `mlx-community/gemma-4-12B-it-qat-4bit`, served by a
  **git-pinned mlx-lm** (in `pyproject.toml`; drop the pin at the first
  mlx-lm release > 0.31.3 — released versions can't load Gemma 4 12B
  conversions). `gemma-4-E4B-it-4bit` fits 16 GB Macs but is structurally
  unreliable on s1/s2; the 31B QAT works but is far too slow for real books.
- `mlx_lm server` ignores per-request sampling and replays identical
  completions — `serve_mlx.sh` serves with `--temp` (`LNVOX_LLM_TEMP`,
  default 0.2) and the client feeds validation errors back into retries.
- `mlx_lm server` has no context-window flag (the window comes from the
  model's config), but the launcher's `--max-model-len` still tells the
  client, so its output budgets fit.

Sizing: 36 GB+ unified memory recommended (the bf16 encoder alone is
~24 GB). Measured on an M4 Max: ~3.6× real-time denoise ≈ **4 h of render
per hour of audio** — plan accordingly, the s4 cache makes interrupted runs
resumable.

---

## Importing books from EPUB

If your source is a publisher-issued EPUB rather
than a folder of `.txt` files, use the bundled extractor:

```bash
uv run lnvox ingest-epub epubs/<series>/volume-01.epub novels/<series>/volume-01
```

This produces:

- `novels/<series>/volume-01/NN-<slug>.txt` — one file per *narrative* chapter,
  ordered by EPUB spine. Front matter, signup pages, copyright, TOC images, and
  image-only spine items (inserts / bonuses / color plates / cover page) are
  dropped; the images they reference are still extracted (see next bullet).
- `novels/<series>/volume-01/images/` — every illustration in the EPUB, including
  the cover. Filenames match the original (`Cover.jpg`, `Insert3.jpg`, …).
- `novels/<series>/volume-01/.epub_meta.json` — title / authors / publisher /
  language / cover-image path / image list / chapter map (with `source_parts`
  for every contributing XHTML stem so Stage 6 can re-align to the original
  markup).

Multi-part chapters (`chapter1.xhtml` + `chapter1_1.xhtml` + `chapter1_2.xhtml`)
are merged into a single `.txt` keyed off the base `chapterN` stem. Chapter
title comes from the first `<h1>`/`<h2>` in the merged group; falls back to a
humanised slug.

Run `lnvox ingest <output_dir>` afterwards as usual — Stage 0 reads
`.epub_meta.json` and copies the cover-image path into `00_book_meta.json` so
Stage 5 can embed it in the final m4b.

EPUB sources for all volumes of a series live under `epubs/<series>/`:

```bash
for v in epubs/<series>/volume-*.epub; do
    name=$(basename "$v" .epub)            # → volume-01
    uv run lnvox ingest-epub "$v" "novels/<series>/$name"
done
```

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

The first non-empty line of each file is taken as the chapter title. For a
hand-made folder, pad the prefixes to one width (`01-…` … `10-…`): Stage 0
orders files numerically, but later stages sort chapter ids as strings, so
`9-…` next to `10-…` would come out of order. (`ingest-epub` pads for you.)

---

## Running the pipeline

### One-shot launcher

```bash
# Volume 1 of a series — narrator clip required (or auto-cast acceptable):
./scripts/run_pipeline.sh novel-name/volume-01 \
    --narrator-clip cv_051e865815e5 \
    --book-title "My Novel — Volume 1"

# Volume 2 — narrator is inherited from volume-01, no flag needed:
./scripts/run_pipeline.sh novel-name/volume-02 \
    --book-title "My Novel — Volume 2"
```

The launcher manages the LLM server and Dramabox transparently:
1. **Starts the LLM server** in the background — llama-server by default,
   vLLM or mlx with `--llm-backend` — and waits for its `/v1/models` endpoint.
2. Runs ingest → s1 → s2 → voice cast → s3 against it.
3. **Stops the LLM server** to free the GPU.
4. Runs s4 (Dramabox, staged by default: crash-isolated phases that resume on
   their own) → s5 (mix to .m4b).
5. Runs s6 (sync layer) when the source EPUB is present — produces the synced
   EPUB + `sync_manifest.json`. Skipped automatically for a `.txt`-only book;
   pass `--skip-sync` to opt out, or `--epub <path>` to point at a non-default
   EPUB location.

A step that fails is retried (`--step-retries`, default 3) — except on exit 2,
which is lnvox's "deterministic refusal" status (a mistyped `--narrator-clip`,
a bad option or knob value): the launcher stops at once so you can fix the
arguments. It reads only exported variables, never `.env`, and exports the
model, endpoint and context length it picked to every stage.

If you already have an LLM server running (e.g. on a separate GPU), pass
`--vllm-url http://host:8000/v1` to skip auto-start (the flag name is
historical; it works for any backend). Without it the server URL is
`$LNVOX_LLM__ENDPOINT`, else `$LNVOX_LLM_ENDPOINT`, else
`http://localhost:${LNVOX_LLM_PORT:-8000}/v1` — the probe and the stages
always use the same one.

**Bigger models (Gemma 4 31B on vLLM).** On a 32 GB desktop card the NVFP4
31B only fits at a 20,480-token window — `serve_vllm.sh`'s default for
31B/27B models, which the launcher also hands to the client so its
output budgets fit — and a desktop session sharing the GPU often OOMs it.
The 31B path is best for:
- DGX-class machines (DGX Spark / H100 / B100) with ≥48 GB VRAM, or
- Headless servers where no other process touches the GPU.

On a roomy machine you can also bump `--max-model-len` higher. It sizes the
server **and** the client's output budgets (the launcher exports it as
`LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN`):

```bash
# DGX Spark or similar with abundant VRAM
./scripts/run_pipeline.sh novel-name/volume-01 \
    --llm-backend vllm \
    --llm-model "nvidia/Gemma-4-31B-IT-NVFP4" \
    --max-model-len 65536 \
    --narrator-clip cv_051e865815e5
```

### Casting by hand (the `--stop-after-audit` loop)

The matcher casts each character independently on demographics, so it can put
two people who talk to each other on the same voice — and it declines to cast
anyone Stage 1 left `gender: unknown` (talking animals, mostly), leaving them
to render descriptor-only. Both are worth fixing by ear.

Stop the pipeline at the one point where fixing is free:

```bash
./scripts/run_pipeline.sh novel-name/volume-01 \
    --narrator-clip cv_… --stop-after-audit
```

That runs ingest → s1 → s2 → voice cast → **the casting audit (report only)**
and stops. Nothing has been directed or rendered yet, so a fix costs nothing.
Later is still fine, just not free: after s3, the next run regenerates the
recast characters' voice descriptors and re-voices their chapters (no new
direction calls); after s4, their beats re-render.

Fix what it reported, in the Studio's Casting tab:

```bash
uv run python scripts/voicebank_studio.py \
    --casting artifacts/novel-name/volume-01/04_voice_assignments.json
uv run lnvox voice audit novel-name/volume-01        # re-check by the numbers
```

(Prefix both with `LNVOX_VOICEBANK=…` for a non-default bank that isn't set
in your `.env` — the launcher prints the exact commands for your run.)

Then re-run the same command **without** `--stop-after-audit` to continue.
s1/s2 are cached per chapter and voice cast keeps the existing casting, so it
resumes rather than repeats.

Three things to know while casting:

- **Locks are what survive.** The Studio saves a re-cast as `"locked": true`.
  A locked entry is kept by every later `voice cast` — over prior volumes,
  `--fresh`, even over `--narrator-clip` for a locked Narrator (it warns and
  ignores the flag) — and `--audit-cast fix` leaves it alone. For an edit made
  in the JSON itself, set `"locked": true` yourself: an unlocked edit to a
  character a prior volume already knows is undone by the next voice cast
  (series continuity wins).
- Locking is also how you mark deliberate sharing (a narrator who is also a
  character), which the audit otherwise can't tell from a mistake.
- The audit only flags pairs that **share scenes**. Two characters who never
  speak together can sound identical without anyone noticing.

### Picking the narrator

Run `lnvox voice list` after seeding to see the per-bucket distribution,
pick a `cv_<id>` whose gender / age / accent fits the narrator style you
want, and pass it via `--narrator-clip` on the FIRST volume only.

Subsequent volumes auto-reuse the prior volume's narrator clip — you can
omit `--narrator-clip` entirely. Pass it again only when you intentionally
want to change the narrator mid-series. (A Narrator locked in this volume's
casting wins over the flag — unlock it first. A clip id that isn't in the
voicebank stops voice cast with exit 2 and a "Did you mean" hint, before any
LLM call.)

### Stage-by-stage (advanced / re-runs)

Every stage is an idempotent CLI; re-running with the same inputs reproduces
outputs.

| Stage | Command | Inputs | Outputs |
|---|---|---|---|
| 0a | `lnvox ingest-epub epubs/<series>/<vol>.epub novels/<series>/<vol>` | `.epub` | `novels/<series>/<vol>/*.txt` + `images/` + `.epub_meta.json` |
| 0 | `lnvox ingest novels/novel-name/volume-01` | `.txt` files | `00_text.jsonl` |
| 1 | `lnvox s1 novel-name/volume-01` | `00_text.jsonl` | `01_characters.json` |
| 2 | `lnvox s2 novel-name/volume-01` | `00_text.jsonl` + `01_characters.json` | `02_scenes/*.json` |
| V | `lnvox voice cast novel-name/volume-01 --narrator-clip cv_…` | `01_characters.json` + voicebank (+ prior volumes' `04_voice_assignments.json`) | `04_voice_assignments.json` |
| A | `lnvox voice audit novel-name/volume-01 [--fix]` | `02_scenes/*.json` + `04_voice_assignments.json` | flags characters who share scenes but not distinguishable voices |
| 3 | `lnvox s3 novel-name/volume-01` | `02_scenes/*.json` + `04_voice_assignments.json` | `03_directed/*.json` + `03_voice_profiles.json` |
| 4 | `lnvox s4 novel-name/volume-01 --staged` | `03_directed/*.json` + `04_voice_assignments.json` | `05_audio/<ch>/*.wav` + `manifest.json` |
| 5 | `lnvox s5 novel-name/volume-01 --title "…"` | `05_audio/<ch>/*.wav` | `06_final/<title>.m4b` + `<title>.timings.json` |
| 6 | `lnvox s6 <book>` | `03_directed/*.json` + `05_audio/<ch>/manifest.json` + the mix's `.timings.json` + original EPUB | `07_sync/<book>.epub` + `sync_manifest.json` |

s3's caches are casting-aware: after a recast it regenerates only the recast
characters' voice profiles and re-voices the cached chapters they speak in,
without new direction calls. `--regen-profiles` forces *every* descriptor
(and so a whole-book re-render in s4) — only for when you changed the
descriptor prompt itself. Stage 4 without `--staged` is the monolithic path;
run it under `./scripts/s4_retry.sh <book>` (see [s4 stability](#s4-stability)).

---

## Lecture mode (non-fiction / technical books)

Narration mode (above) is built for novels: multi-voice, character casting,
emotionally-acted delivery. **Lecture mode** is the opposite — a single steady
narrator reading the text *as written*, for non-fiction and technical books.
No characters, no scene segmentation, no dramatization. See
[DESIGN.md §13](DESIGN.md) for the full design.

What it does differently:

- **Collapses s1 + s2 + s3 into one `lnvox lecture` stage.** No character or
  scene LLM passes. The text is split into TTS-sized narration beats; each
  beat's `source_span` stays verbatim (so Stage-6 sync is near-perfect).
- **Speech-normalizes** each beat for the ear (`1973` → "nineteen
  seventy-three", `Fig. 3` → "Figure three", `42%` → "forty-two percent")
  without rewriting or dramatizing. `--no-normalize` reads the text byte-literal
  and skips the LLM entirely.
- **Never narrates code / tables / figures.** During EPUB import they're
  classified out of the prose, **rendered to PNG** (code is syntax-highlighted),
  and surfaced by Stage 6 as *visual elements* the reader displays at the right
  timestamp — exactly like illustrations. Boilerplate (TOC, copyright, index)
  is dropped.

### Launch the whole pipeline in lecture mode

One command, same launcher as narration — just add `--mode lecture`:

```bash
# 1. Import the EPUB in lecture mode (classify blocks, drop boilerplate,
#    render code/tables to images). Needs the `render` extra (see below).
uv run lnvox ingest-epub epubs/nonfiction/book.epub novels/nonfiction/book --mode lecture

# 2. Run the full pipeline: ingest → voice cast → lecture → s4 → s5 → s6.
#    s1/s2/s3 are skipped automatically. Pick a narrator clip by ear (lecture
#    mode is 100% narrator, so this matters most here).
./scripts/run_pipeline.sh nonfiction/book \
    --mode lecture \
    --narrator-clip cv_051e865815e5 \
    --book-title "Book Title — Subtitle"
```

Flags specific to lecture mode:

- `--mode lecture` — required; routes ingest → voice cast → `lnvox lecture`,
  skipping s1/s2/s3.
- `--no-normalize` — read the text verbatim (no speech-normalize LLM pass).
  Combined with `--narrator-clip`, the launcher makes **zero** LLM calls and
  **skips the LLM server entirely** — the run is ingest → cast → split → TTS →
  mix.

### The `render` extra (code/table images)

Rendering code and tables to PNG uses Pygments + headless Chromium, kept out of
the core install:

```bash
uv sync --extra render
uv run playwright install chromium   # one-time browser download (~115 MB)
```

Without it, lecture ingest still works — code/table blocks are recorded as
HTML-only visual elements (no PNG), and the pipeline never fails on a missing
renderer. Pass `--no-render` to `ingest-epub` to skip rasterization explicitly.

### Lecture-mode stages (advanced / re-runs)

| Stage | Command | Notes |
|---|---|---|
| 0a | `lnvox ingest-epub <epub> <out> --mode lecture [--no-render] [--ingest-classifier none]` | Classifies blocks, drops boilerplate, renders code/tables → `images/`, records `visual_elements` in `.epub_meta.json`. |
| 0 | `lnvox ingest novels/<book> --book-id <book> --mode lecture` | Writes `00_text.jsonl` + a Narrator-only `01_characters.json` stub (no s1). |
| V | `lnvox voice cast <book> --narrator-clip cv_…` | Casts only the Narrator. |
| L | `lnvox lecture <book> [--no-normalize]` | Split + speech-normalize → `03_directed/*.json`. **Replaces s1/s2/s3.** |
| 4–6 | (unchanged) | `lnvox s4 --staged`, `lnvox s5`, `lnvox s6` run exactly as in narration mode. |

`--ingest-classifier` controls the block classifier: `fallback` (default — the
LLM is consulted *only* for untagged/ambiguous blocks) or `none` (rules-only,
no LLM in ingest at all).

---

## Scenario mode (theater scripts → timed sync + full-cast audio)

The third mode. The input is a **theater script** (a troupe's working
markdown, not a book), and the primary deliverable is not the audiobook but a
**sync file** — one timed entry per line: `{start, end, speaker, text,
direction, emotion}` — plus the full-cast audio that timeline is measured
against. Use it to learn lines, rehearse against cast voices, or drive the
régie: staging gets a timed pause, sound/light cues become zero-duration
markers on the same timeline. See [DESIGN.md §17](DESIGN.md) for the design.

What it does differently:

- **The LLM structures, it never rewrites.** Ingest classifies every line
  into dialogue / staging (didascalies) / cue, and every dialogue line must
  be an **exact substring of the source** — lines that fail validation are
  kept as staging with a loud warning, never paraphrased or dropped.
  Validated on real scripts: 2 demotions across ~700 lines.
- **Formats are tolerated, not required.** Speaker labels as `**Nom** - ligne`,
  `NAME: line`, or bold CAPS on their own line; cast sections titled
  `PERSONNAGES` / `CHARACTERS` / `CAST`; scene headers like `Séquence 3`,
  `SCENE 1`, `ACT II` (a headerless one-act works too). Label variants and
  typos are canonicalized into one character.
- **Per-line acting direction.** The director adds an `emotion` (calm, joy,
  sadness, anger, fear, surprise, disgust) and a short cue *in the script's
  language*; parenthetical cues printed inside a line (`(whispering) …`) are
  kept in the script/sync but stripped from what the TTS speaks.
- **Languages:** validated in French and English. For a French cast, seed a
  separate voicebank from the French Common Voice tarball and point the
  pipeline at it with `LNVOX_VOICEBANK=voicebank-fr` (all `voice` commands,
  `s4` and the Studio honor it; a casting made against one bank refuses to
  render against another).
- **Privacy:** `scenarios/` and `data/` are gitignored; script text goes only
  to the locally-served LLM and the local TTS — it never leaves the machine.

### Script format

Anything close to this works (invented example — real scripts are messier and
that's fine):

```markdown
CHARACTERS

- GUARD
- TRAVELLER

SCENE 1

[A city gate, at night. A lantern swings in the wind.]

GUARD: Who goes there?

TRAVELLER: (whispering) It's me. Open up.
```

### Launch the whole pipeline in scenario mode

```bash
./scripts/run_pipeline.sh myplay \
    --mode scenario \
    --scenario-file "scenarios/My Play.md" \
    --book-title "My Play"
# French cast? prefix with:  LNVOX_VOICEBANK=voicebank-fr
```

The launcher runs: ingest-scenario → voice cast → scenario direction (all on
the LLM server it manages) → s4 TTS → s5 mix → **scenario-sync**. Stage 6
(EPUB sync) is skipped — scenario mode has its own sync emitter. Without
`--scenario-file` it reuses the previous ingest, but only a complete one
(`00_script.json` **and** `01_characters.json`); after editing the script,
pass the file again — only the changed scenes cost LLM calls.

### Scenario-mode stages (advanced / re-runs)

| Stage | Command | Notes |
|---|---|---|
| 0 | `lnvox ingest-scenario scenarios/<play>.md --id <id> [--no-cache]` | LLM structure + roster + characters → `00_script.json`, `01_characters.json`. Content-cached under `cache/scenario/` — re-runs only re-pay scenes whose text changed; `--no-cache` re-rolls and overwrites the cached entries, so the re-roll sticks. |
| V | `lnvox voice cast <id>` | Same casting as narration mode. **Check it by ear in the Studio** — the matcher may assign the same clip to two characters who share scenes; recast by hand where it matters. |
| S | `lnvox scenario <id>` | Direction pass (emotion + cue per line) → `03_directed/*.json`. Idempotent per scene. |
| 4–5 | `lnvox s4 <id> --staged` / `lnvox s5 <id> --inter-chapter 0.75 --lead-in 0.35` | Unchanged Dramabox rendering; the launcher mixes with play pacing. VibeVoice renders audio too, but session WAVs carry no per-line timing, so scenario-sync requires the per-beat Dramabox path. |
| Y | `lnvox scenario-sync <id>` | Timing plan → `07_sync/<scene>.json` + `play.json` + `play.srt`. Follows the mix's `06_final/<title>.timings.json` (pads, lead-in, scene list); `--title` picks the mix, default the newest. |

### Outputs

- `artifacts/<id>/06_final/<title>.m4b` — the full-cast performance, one
  chapter marker per scene.
- `artifacts/<id>/07_sync/play.json` — every line/staging/cue with absolute
  timestamps. Computed from the same plan as the mix, so **the sync total and
  the m4b duration match exactly**; the timing survives edits because the
  content-hash cache re-renders only changed lines.
- `artifacts/<id>/07_sync/play.srt` — the same timeline as subtitles
  (dialogue as `SPEAKER: text`, staging bracketed, cues double-bracketed) for
  any player that takes SRT.

Group lines (`Tous :` / `All:`) render with the narrator's voice; the sync
file keeps the group label.

---

## Stage 6 — sync layer

The launcher runs this automatically as its final step whenever the source
EPUB is present (skip it with `--skip-sync`, or run it standalone with
`lnvox s6 <book>`). For players that highlight the current beat in sync with
audio playback (WebKit-based reader apps, Plex audiobooks, Audiobookshelf, a
custom front-end) it re-aligns the Stage-3 beats back onto the original EPUB's
XHTML. The output is a copy of the EPUB with
`<span class="lnvox-beat" data-beat-id="<beat_id>">` wrappers around each
beat's text plus a sidecar `sync_manifest.json` mapping each beat to its start
and end time in the m4b. In lecture mode the same manifest also carries the
`visual_elements` (rendered code/tables/figures) with their `trigger_seconds`.

Stage 6 follows the Stage-5 mix it syncs against — its pads, its lead-in,
the chapters it contained and the order it embedded pictures in, all read
from `06_final/<title>.timings.json`. No s5 flags need repeating; `--title`
picks the mix (default: the newest).

### Algorithm

1. **Build a shadow string + DOM map.** For each chapter, parse the original
   XHTML with BeautifulSoup. Concatenate every text node into one massive
   normalised string while maintaining a `[char_index → (DOM_node, offset)]`
   table. Record positions of non-text elements too (images, `<hr>` scene
   breaks) so the player can trigger visual cues at the right offset.

2. **Normalize both sides** (search-side only — preserve original casing in
   the DOM map so the wrapped output keeps its capitalisation):
   - Lowercase.
   - Smart quotes (`"` `"` `'` `'`) → straight (`"` `'`).
   - Em / en dashes (`—`, `–`) → ASCII `-`.
   - Collapse runs of whitespace / newlines → a single space.

   Ligatures (`ﬁ`, `ﬂ`) and the `…` ellipsis are deliberately **not**
   expanded: that would change the character-count mapping and make
   re-aligning the wrap back to the original DOM offsets fragile.

3. **Anchor-based sequential matching.** Naive `indexOf(beat.text)` fails on
   real data — s3 strips attribution tags so `"…", she said, "…"` collapses
   into one merged beat whose text doesn't appear verbatim in the source.
   First try the beat's verbatim `source_span` (Stage 2's grounding — the
   common path, `span-exact`); otherwise use the first 30 and last 30 chars of
   the beat's text as anchors, find them in sequence, and treat the inclusive
   span as the matched range. **Always start beat N+1's search at beat N's
   end-index** to keep repeated dialogue (`"Yes."` twice in a row) from
   collapsing onto the same span.

4. **DOM wrapping.** For each matched `[start, end)` shadow-string range, use
   the index map to identify which DOM nodes contain those characters. Wrap
   them in `<span class="lnvox-beat" data-beat-id="<beat_id>">`. When a beat
   straddles multiple nodes (very common — narration with nested `<em>`/`<a>`
   tags), each node gets its own span sharing the same `data-beat-id`. Save
   the modified XHTML into a new EPUB that's otherwise byte-identical to the
   original.

The matcher runs in **two passes per chapter** (all of a chapter's
`source_parts` XHTML are concatenated into one "master shadow" first):

- **Pass 1 — strict, forward-only.** Accept only `span-exact`, `exact`
  (short beats) and `anchored` (head + tail both found) matches, advancing a
  cursor. Lenient fallbacks are *off* here because a loose match can
  false-positive on a recurring phrase and jump the cursor, stranding
  everything after it. A
  per-match **forward-jump cap** (`_MAX_FORWARD_JUMP = 8000` chars) is the
  single most important guard — without it, one bad anchored match leaping to
  the chapter's end drops the whole tail of the chapter.
- **Pass 2 — lenient gap-fill.** For each beat Pass 1 missed, search only the
  gap between its bracketing matches (with ~2 KB of backward slack, since
  Stage 2 sometimes reorders dialogue attribution). After the strict rungs it
  falls back to `head-only`, `tail-only`, then fuzzy `SequenceMatcher`.
  Repeated up to 3 rounds, each round shrinking the remaining gaps. (The old
  `backtrack` rung is gone — the backward slack covers its window; its
  histogram key stays, always 0.)

5. **Outputs.** Three artifacts under `artifacts/<book>/07_sync/`:
   - `<book>.epub` — same structure as the original EPUB, with
     `<span class="lnvox-beat" data-beat-id="…">` wrappers added.
   - `sync_manifest.json` — `beats[]` (per matched beat:
     `{beat_id, data_beat_id, chapter_id, xhtml, type, speaker,
       start_seconds, end_seconds, match_confidence}`) + `images[]` +
     `visual_elements[]` + a top-level `match_confidence` histogram, plus the
     `silences` it used (lead-in included) and the `m4b` they describe. Beat
     times are rebuilt from the Stage-4 manifests with the mix's recorded
     layout, and each chapter starts at the offset s5 measured for it; a
     chapter re-rendered after the mix gets a `[WARN]` (re-run s5, then s6).
   - `images[]` in the same manifest — one entry per picture **embedded in
     the m4b**, in embed order (cover first), so `images[i]` is the i-th
     embedded picture: `{src, xhtml, chapter_id, trigger_seconds,
       after_beat_id, before_beat_id}`. The player flips to `src` when
     playback reaches `trigger_seconds` (= the start of `before_beat_id`).
     A picture on a front-matter page triggers at the first beat (≈ 0 s);
     `trigger_seconds: null` is for end-matter plates after the last chapter
     and pictures no page references (often the cover image itself). Placement
     is per-XHTML-part, so an insert between `chapterN.xhtml` and
     `chapterN_1.xhtml` triggers at the correct mid-chapter beat. Every
     individual occurrence is also listed in `visual_elements[]` as
     `kind: "image"` (image-only spine pages add `spine_page`,
     `after_chapter` and `before_chapter`).
   - `unmatched.json` — beats the matcher couldn't anchor (usually genuine
     Stage-2 paraphrases/hallucinations).

### Player integration

```js
// On audio timeupdate:
const active = manifest.beats.filter(
    b => b.start_seconds <= t && t < b.end_seconds
);
active.forEach(b =>
    document.querySelectorAll(`[data-beat-id="${b.beat_id}"]`)
        .forEach(el => el.classList.add("active"))
);
```
Pair with a `.lnvox-beat.active { background: … }` rule.

### Why this is non-trivial

Real-data failure modes the algorithm handles:

- **Dropped attribution tags.** Anchor matching tolerates the gap between head
  and tail (the stripped `"…", she said, "…"` lives in the span but needn't
  match the beat text).
- **Paraphrased head OR tail.** `head-only` / `tail-only` fallbacks wrap just
  the verified anchor rather than over-claiming.
- **Reordered attribution.** Source `"Patrick admitted, '…'"` becomes a
  dialogue beat then a `"Patrick admitted"` narration beat — *earlier* in the
  source than the dialogue. Pass 2's backward slack recovers these.
- **Sentence-split narration** (`_split_long_text`) and **same-speaker merge**
  (`_merge_same_speaker`) — consecutive beats stay ordered via the cursor.

### Measured results

| Book | Match rate | Notes |
|---|---|---|
| title/volume-01 | 98.9% | mostly exact + anchored |
| title/volume-02 | 95.7% | more tail-only/fuzzy (heavier Stage-2 paraphrasing) |

The unmatched remainder are genuine Stage-2 hallucinations (independent
per-beat ceiling measured at 92–94%; two-pass + fuzzy recovers reordered
attribution to push past it). `match_confidence` in the manifest lets your QA
flag the low-confidence (`fuzzy`, `tail-only`) wraps.

### Tuning knobs (`src/lnvox/stages/s6_sync.py`)

| Constant | Default | Effect |
|---|---|---|
| `_MAX_FORWARD_JUMP` | 8000 | Max chars a Pass-1 match may sit ahead of the cursor. Lower if you still see a chapter's tail dropping; raise if a chapter legitimately has very long unmatched runs. |
| `HEAD_ANCHOR_LEN` / `TAIL_ANCHOR_LEN` | 30 / 30 | Anchor length. Longer = fewer false positives but more misses when the head/tail is lightly paraphrased. |
| `_FUZZY_MIN_RATIO` | 0.20 | Min fraction of the beat the fuzzy longest-common substring must cover. Lower to match more aggressively (risks false positives). |
| `_PASS2_BACKWARD_SLACK` | 2000 | How far before a gap's start Pass 2 searches (for reordered attribution). A local constant inside `run`. |

---

## Cross-volume continuity

Drop volume-02 next to volume-01 and re-run the launcher:

```bash
./scripts/run_pipeline.sh novel-name/volume-02 \
    --book-title "A Certain Novel — Volume 2"
```

The pipeline auto-detects the earlier volumes under `artifacts/novel-name/`
— sibling folders with the same prefix and a smaller number, compared
numerically (`volume-9` comes before `volume-10`, padded or not) — and, at
voice cast:
- Folds **every** prior volume's `04_voice_assignments.json` into one index,
  newest volume winning, and reuses the clip of any character it recognises —
  by canonical name, token-order variant or alias (alias matches also need
  the same gender) — without an LLM call. A character who skips a volume
  still gets their voice back.
- If `--narrator-clip` is omitted, the prior volumes' narrator clip is
  reused (so the listener hears the same narrator across the series).

s1 builds each volume's cast on its own; continuity is voice cast's job.

To intentionally change a recurring voice in this volume only (e.g. a
character's casting was wrong in volume-01), re-cast it in the Studio — which
saves it as `"locked": true` — or edit the new volume's
`04_voice_assignments.json` and set `"locked": true` on the entry yourself.
Without the lock, the next voice cast restores the prior volume's voice. Then
re-run the launcher: s3 re-voices that character's chapters on its own, and
s4 re-renders just their beats. To change the voice for the rest of the
series instead, change it in the newest earlier volume that has the
character; later volumes inherit it.

---

## Operational notes

### GPU handoff

Both Gemma (the LLM server — llama-server, vLLM or mlx) and Dramabox want
most of the GPU's VRAM. The launcher handles this; by hand, always:
- **Stop the LLM server before starting Dramabox**, and vice versa.
- `nvidia-smi` to confirm the GPU is idle before starting the next phase.
- `set -o pipefail` if you write your own bash wrappers — `tee` masks
  upstream failures otherwise.

### s4 stability

Long Dramabox runs (10+ min continuous denoising) sometimes SIGKILL. Cause
appears to be CUDA memory fragmentation on RTX 50-series. The launcher's
default, the **staged** path (`lnvox s4 <book> --staged`), is built for it:
four single-model phases, each in its own subprocess, with checkpoints the
driver resumes from after a crash — no outer retry loop needed. Use it when
running s4 by hand too.

The monolithic path (`lnvox s4` without `--staged`, or `LNVOX_S4_STAGED=0`
in the launcher) holds everything in one process — run it under
**`scripts/s4_retry.sh <book>`**, never bare. The script restarts it until
it exits cleanly (`MAX_ATTEMPTS`, default 100; the launcher passes
`--max-retries`, default 30), `RETRY_DELAY` seconds apart (default 5), and
logs every attempt to `$LNVOX_ARTIFACTS_DIR/<book>/_s4_full.log`. It stops
at once on exit 2, a deterministic refusal a retry would only replay (a bad
knob value, monolithic on MPS). Either way the content-hash cache makes
every restart a near-zero-cost resume from the last finished beat.

### Beat length

Empirically Dramabox sounds best on shorter beats — longer text raises its
noise floor and can slur into unintelligible speech. The Director's merge pass
caps at 375 chars (~30 s; `MAX_MERGED_BEAT_CHARS` in `s3_director.py`); a source
narration paragraph longer than that is auto-split at sentence boundaries before
TTS.

### Render quality (defaults, and the lighter fallback)

Since 2026-08-04 the staged s4 path (the launcher default) renders with the
**high-quality recipe** on every platform — it **won a blind A/B** against
the original recipe (which had audible artifacts — `MAC_TEST_REPORT.md`)
and is even **~20% faster** in denoise, because short chunks beat long ones
(DiT attention is quadratic in chunk length):

- **Unquantized bf16 Gemma prompt-encoder** instead of bnb-4bit. Costs a
  one-time ~23 GB download; the staged ctx phase loads it with the GPU to
  itself, so it fits a 32 GB card.
- **Denoise chunks capped at 22 s** (target 18 s), word-boundary fallback
  split, crossfaded seams.
- **Vocoder in fp32.**

No flags needed — this is what `run_pipeline.sh` does now. Each knob can be
reverted individually (all of them cost quality; useful for smaller GPUs,
tight disk, or reproducing an old render byte-for-byte):

```bash
LNVOX_S4_ENCODER_PRECISION=bnb4  # quantized encoder: no 23 GB download,
                                 # less VRAM — audibly worse conditioning
LNVOX_S4_CHUNK_CAP=none          # restore the 45 s chunker (the artifact
                                 # source in the blind test; also slower)
LNVOX_S4_DECODE_DTYPE=auto       # vocoder back to per-device dtype (bf16
                                 # on CUDA)
```

The effective recipe is part of the TTS cache key (default runs are tagged
`+enc-bf16+cap22+dec-fp32`; the old recipe is untagged), so renders from
different recipes never collide in `cache/tts/` — which also means changing
the recipe mid-book re-renders everything. Pick a recipe per book and keep
it. Scope notes: the cap and decode knobs exist only on the staged path
(`lnvox s4` without `--staged` refuses explicit values with exit 2, as it
does any invalid knob value); the monolithic path keeps the old `bnb4`
encoder default because the bf16 encoder doesn't fit alongside the DiT +
vocoder in one process; and on a Mac the reverts can't take effect (MPS
always runs bf16 / 22 s / fp32), so its cache key follows the effective
recipe and the reverts re-key nothing (an explicit numeric cap still does). Switching recipes back and forth is
safe for the placed files: the staged path re-copies any
`05_audio/<ch>/<beat>.wav` that isn't an exact copy of the cache entry its
manifest names.

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
| `BadRequestError: maximum context length` | The server's context is smaller than the client assumes (65,536 by default), so its output budgets overflow the window | Pass `--max-model-len N` to the launcher — it sizes the serve script **and** reaches the client. By hand, export `LNVOX_LLM_MAX_LEN=N` for both the serve script and the stages (the client reads it too). |
| `openai.APITimeoutError` mid-s2 with a slow model | Default timeout assumes >20 tok/s; 31B on DGX Spark is ~6 tok/s, so a 28k-token s2 chunk runs ~80 min. A timed-out request is **not** re-sent (the SDK's own retries are off, so one call never waits a multiple of its timeout): the stage fails and the launcher retries the step, resuming from cache | Bump `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN` — default 0.25 gives ~7000s for a 28k-token call. For very slow hosts use 0.35–0.5. (Per-call timeout = `timeout_base_seconds + max_tokens × timeout_seconds_per_token`.) |
| `AssertionError: Torch not compiled with CUDA enabled` on DGX Spark / Jetson | aarch64 platform; Dramabox's pinned `torch==2.8.0` only has CPU-only aarch64 wheels | Re-run `./scripts/setup_dramabox.sh` — it auto-detects aarch64 and pulls `torch>=2.10+cu130` (which **does** ship aarch64+sbsa CUDA wheels) instead. x86_64 is unaffected. |
| s4 crashes after 10–15 min | CUDA fragmentation | The staged path (launcher default) relaunches the crashed phase itself; for the monolithic path use `scripts/s4_retry.sh` (auto-resumes from cache) |
| `… refused its input (exit 2) — not retrying` | A deterministic refusal: mistyped `--narrator-clip`, invalid `LNVOX_S4_*` value, staged-only knob on the monolithic path, monolithic s4 on MPS | Fix the argument / variable the message above names and re-run; a retry would only replay it |
| Narrator (or any) voice doesn't match its descriptor | The casting changed after s3 wrote the descriptor | Re-run the launcher (or `lnvox s3`) after voice cast: s3's profile cache is casting-aware, so it regenerates the recast characters' descriptors and re-voices their chapters on its own. `--regen-profiles` rewrites *every* descriptor (whole-book re-render) — only for a changed descriptor prompt |
| Empty `accent` distribution in voicebank | TSV had pipe-separated accents | Fix `_normalize_accent` (already in `voices/common_voice.py`); re-seed |
| Dramabox renders sound rushed | Long beats (>60 s) | Lower `MAX_MERGED_BEAT_CHARS` in `s3_director.py`, re-run s3 |
| `No module named 'av'` / `requires accelerate` / `mamba-ssm` build fails when loading Dramabox | Dramabox env mixed with `--extra serve` (torch 2.8 vs 2.11 conflict), or `uv sync` pruned Dramabox's deps | Use a TTS-only env: `./scripts/run_pipeline.sh --prepare-env tts` (or `uv sync --extra voice --extra tts && ./scripts/setup_dramabox.sh`; see [Voicebank Studio & TTS Lab](#voicebank-studio--tts-lab)) |

---

## Voicebank Studio & TTS Lab

`scripts/voicebank_studio.py` is a standalone PySide6 GUI for curating the
voicebank by ear (listen, import from Common Voice, add/erase clips, cast
characters) and, in its **TTS Lab** tab, auditioning Dramabox prompts — handy
for finding how to phrase a direction so Dramabox *performs* it instead of
reading it aloud. See [DESIGN.md §12](DESIGN.md).

The first three tabs need only the `voice` extra:

```bash
uv pip install PySide6                       # GUI toolkit (dev-only, not a project dep)
uv run python scripts/voicebank_studio.py
```

### TTS Lab — set up the Dramabox env

The **TTS Lab** tab loads the Dramabox model (only when you click its
**"⚙ Load model"** button — never on startup or when using the other tabs), so
it needs Dramabox's runtime installed. Set it up in the **TTS environment**:

```bash
uv sync --extra voice --extra tts            # NOT --extra serve
./scripts/setup_dramabox.sh                  # installs Dramabox + its deps
```

**Don't combine it with `--extra serve`.** vLLM (the `serve` extra) needs
`torch>=2.11`, Dramabox pins `torch==2.8`, and they can't share a venv — which
is exactly why the pipeline runs the LLM and TTS phases as separate stages with
separate environments (`run_pipeline.sh` swaps between them). Mixing them is
what causes `No module named 'av'` and the `mamba-ssm` build failures. Use the
TTS env above for the Studio; run `uv sync --extra serve …` again when you go
back to the LLM stages.

The launcher's own swap is available on its own:
`./scripts/run_pipeline.sh --prepare-env tts` (or `llm` — per
`--llm-backend` — or `vibevoice`) prepares that phase's venv exactly as a
full run would, then exits without running any stage; no book id needed.
It's what the ComfyUI nodes call between LLM and TTS nodes, and the easy way
to get the right env before running a stage by hand.

---

## File map cheat sheet

```
epubs/<series>/<vol>.epub                Original publisher EPUB (input for Stage 0a)

novels/<series>/<volume-NN>/             Stage 0a output → Stage 0 input
├── NN-<slug>.txt                        One per narrative chapter (first line = title)
├── images/                              Cover + every illustration
└── .epub_meta.json                      Title / authors / publisher / chapter map

artifacts/<series>/<volume-NN>/          ($LNVOX_ARTIFACTS_DIR, default artifacts/)
├── 00_book_meta.json                    EPUB metadata propagated from .epub_meta.json
├── 00_text.jsonl                        Ingested chapters (one JSON line each)
├── 01_characters.json                   Merged book cast
├── 01_characters_per_chapter/*.json     Pre-merge per-chapter casts
├── 02_scenes/*.json                     Scene/beat segmentation
├── 03_voice_profiles.json               Per-character voice descriptors
├── 03_directed/*.json                   Dramabox-ready beat prompts
├── 04_voice_assignments.json            Character → ref clip mapping
├── 05_audio/<chapter>/                  Rendered beat WAVs + manifest.json
├── 05_audio_v2/<chapter>/               VibeVoice session renders (s4 --tts-backend vibevoice)
├── 06_final/<title>.m4b                 Final audiobook
├── 06_final/<title>.timings.json        The mix's layout contract (s6 / scenario-sync / sample follow it)
└── 07_sync/                             Synced EPUB + sync_manifest.json + unmatched.json

artifacts/<scenario-id>/                 Scenario mode (theater scripts)
├── 00_script.json                       Structured verbatim script (scenes → dialogue/staging/cue)
├── 01_characters.json                   Roster (script-given + LLM gap-fill)
├── 03_directed/*.json                   Directed beats (emotion + cue per line)
├── 05_audio/ · 06_final/                Same as narration mode
└── 07_sync/                             play.json + <scene>.json + play.srt (timed lines & cues)

scenarios/*.md                           Theater scripts (gitignored — private)

voicebank/                               English bank (default)
voicebank-fr/                            French bank (select with LNVOX_VOICEBANK=voicebank-fr)
├── manifest.json                        Indexed voice clips
└── clips/cv_<id>.wav                    Reference clips (10–20 s each)

cache/tts/<sha256>.wav                   Content-addressed TTS cache (survives book deletions)
cache/scenario/<sha256>.json             Scenario-ingest LLM cache (keyed on schema + system + rendered prompt + model)

external/DramaBox/                       Cloned Dramabox repo (sys.path-injected)
external/VibeVoice/                      Community fork (pip -e; scripts/setup_vibevoice.sh)
data/<cv-corpus-…>/                      Raw Common Voice extraction (EN + FR)
```

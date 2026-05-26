# ln-vox — Novel-to-Audiobook Pipeline

## 0. Goals & non-goals

**Goal.** Turn a text novel (plain `.txt` / `.epub` / `.md`) into a multi-voice
audiobook with character-appropriate timbre and emotionally-acted delivery,
runnable on a single workstation with one or two consumer GPUs.

**Non-goals (v1).**
- Real-time streaming. The pipeline is offline/batch.
- Music or SFX. Voice only.
- Multi-language mixing within one book.
- Cloning identifiable real people for distribution. (Personal use only — see §6.)

## 1. Pipeline overview

```
   ┌──────────────┐
   │ 0a. ingest-  │           ┌────────────┐
   │   epub (opt.)│           │ Voicebank  │ (seeded once from Common Voice 25)
   └──────┬───────┘           └──────┬─────┘
          │ .txt + images          │ ref clips with gender / age / accent
          ▼                        ▼
  ┌────────┐  ┌───────────────┐  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────┐  ┌──────┐
  │ Ingest │→ │ 1. Characters │→ │ 2. Scenes & │→ │ V. Voice cast│→ │ 3. Director│→ │ 4. TTS   │→ │ 5.   │→ .m4b
  │ (text) │  │  (Gemma 4)    │  │  speakers   │  │  (Gemma 4    │  │  (stage    │  │ (Drama-  │  │ Mix  │
  │        │  │  + merge w/   │  │  (Gemma 4)  │  │   match)     │  │ directions)│  │  box)    │  │      │
  │        │  │  prev volume) │  │             │  │              │  │            │  │          │  │      │
  └────────┘  └───────────────┘  └─────────────┘  └──────────────┘  └────────────┘  └──────────┘  └──┬───┘
                     │                                  ▲                                            │
                     │                                  │ optional --narrator-clip override          ▼
                     │                                  │ + auto-reuse of prior volume's casting    ┌─────────────┐
                     ▼                                                                              │ 6. Sync     │
              (cumulative cast)                                                                     │  epub→spans │
                                                                                                    └─────────────┘
```

**Critical ordering**: voice casting is **stage V**, after scene segmentation
and **before** the Director. The Director composes voice descriptors that
*match the assigned reference clip*, so a clip's actual gender / age / accent
drives the bracket-prefix Dramabox sees.

**Series & volumes**. Book IDs are hierarchical, `<series>/volume-NN`
(e.g. `toaru/volume-02`). When processing a non-first volume, the pipeline
auto-detects prior volume(s) in the same series and:
- Merges per-chapter character lists with the previous volume's cast (s1)
- Reuses prior-volume voice clip assignments for recurring characters (V)

Every stage reads & writes JSON to `artifacts/<book-id>/`. Each stage is an
idempotent CLI: re-running with the same inputs reproduces outputs. This makes
debugging / partial re-runs trivial.

## 2. Stage contracts

### 2.0 Stage 0a — EPUB extraction (optional pre-ingest)

When the source is a publisher EPUB rather than a `novels/<series>/<vol>/`
folder of `.txt` files, `lnvox ingest-epub <epub> <output_dir>` converts it
to the layout Stage 0 expects.

Implementation: [`src/lnvox/ingest/epub.py`](src/lnvox/ingest/epub.py).

Pipeline inside the extractor:

1. Read `META-INF/container.xml` → locate the OPF rootfile.
2. Parse the OPF for `<dc:title>`, `<dc:creator>`, `<dc:publisher>`,
   `<dc:language>`, the `<manifest>` (id → href / media-type / properties),
   and the `<spine>` (ordered itemrefs).
3. Resolve every `image/*` manifest entry → copy to
   `<output_dir>/images/<basename>`. Cover is identified by
   `properties="cover-image"` (EPUB 3) OR `<meta name="cover"…>` (EPUB 2) OR
   filename stem `cover`.
4. Walk the spine in order. Skip front/back-matter stems matching
   `cover/toc/tocimg/copyright/signup/insert\d+/bonus\d+/color\d+`. For each
   remaining XHTML, extract `<h1>`/`<h2>` as title and concatenate `<p>`
   text. Group multi-part chapters (`chapter1.xhtml` + `chapter1_1.xhtml` +
   `chapter1_2.xhtml`) by stripping the trailing `_N` suffix.
5. Write `NN-<slug>.txt` per chapter group (NN = group order in the spine,
   first line = title, blank-line-separated paragraphs as body).
6. Write `.epub_meta.json` capturing title / authors / publisher / language /
   cover-image / images list / chapter map (with `source_parts` for each
   chapter so Stage 6 can re-align back to the original XHTML).

Stage 0 (`lnvox ingest`) detects `.epub_meta.json` and propagates the cover
image path into `00_book_meta.json` for Stage 5 to embed in the final m4b.

### 2.1 Ingest

- Input: a folder of `.txt` files (typically the output of Stage 0a, or
  manually-prepared).
- Output: `artifacts/<book>/00_text.jsonl` — one record per chapter:
  `{chapter_id, title, text}`.
- Parsers: `.txt` (filename-prefix ordering, first line = title), `.epub`
  (deferred to Stage 0a), `.md` (split on H1/H2).
- If `.epub_meta.json` is present, the EPUB cover path is copied into
  `00_book_meta.json` so Stage 5 can embed it.

### 2.2 Stage 1 — Character extraction (Gemma 4)

Run **per chapter** then **merge globally**. Per-chapter keeps prompts short
and avoids the 256K context being mostly wasted; the merge step deduplicates
aliases ("Lord Vex" / "Vex" / "the duke").

**Cross-volume merge.** When the book ID is `<series>/volume-NN` with NN > 01,
the pipeline auto-detects all prior volumes in the same series
(`artifacts/<series>/volume-*`) and feeds their `01_characters.json` files into
the global merge step *in addition* to the current volume's per-chapter lists.
The merge prompt is instructed to PRESERVE every trait from prior volumes
(affiliation, origin, established personality) while integrating new traits
from the current volume. A character who appeared as "Anglican Church nun" in
volume 01 stays an Anglican nun in volume 02 even if the new chapters only
mention her grimoire memory.

**Per-chapter prompt** asks Gemma 4 to return strict JSON:

```json
{
  "characters": [
    {
      "name": "canonical name as it appears",
      "aliases": ["other names/titles used"],
      "first_mention_chapter": 3,
      "gender": "male|female|nonbinary|unknown",
      "approx_age": "child|teen|young_adult|adult|elder|unknown",
      "description": "physical + speech + manner, 2-3 sentences pulled from text",
      "evidence": ["short verbatim quotes that justify the description"]
    }
  ]
}
```

**Global merge** is a second Gemma 4 call that receives the concatenated
per-chapter lists and outputs a single deduplicated cast. Output:
`01_characters.json`.

> **Model choice.** Use **Gemma 4 E4B** for dev/iteration (fits in ~10 GB
> VRAM, ~2× faster) and **Gemma 4 31B Dense** for production runs. Both via
> the same vLLM endpoint — only `--model` changes.

### 2.3 Stage 2 — Scene & speaker segmentation (Gemma 4)

Input: chapter text + global cast list. Output per chapter:
`02_scenes/<chapter_id>.json`:

```json
{
  "chapter_id": "ch03",
  "scenes": [
    {
      "scene_id": "ch03_s1",
      "location_hint": "Vex's study at dusk",
      "beats": [
        {"type": "narration", "text": "The duke turned from the window."},
        {"type": "dialogue", "speaker": "Lord Vex", "text": "You're late."},
        {"type": "dialogue", "speaker": "Mira", "text": "I came as soon as I could."}
      ]
    }
  ]
}
```

`narration` is voiced by a fixed Narrator character (auto-added to cast).
Quoted speech inside narration is attributed when the LLM can infer it,
otherwise stays as narration.

**Failure mode to watch.** Long passages with `"…," she said` style — the LLM
must split the dialogue from the tag. Prompt explicitly instructs to drop the
`"she said"` tag once `speaker` is assigned.

### 2.4 Stage V — Voice cast (Gemma 4)

**Runs between s2 and s3.** This is the only ordering change from the v1
sketch and it matters: by casting voices *before* writing stage directions,
the Director (§2.5) can write descriptors that physically match the assigned
reference clip (e.g. "elder Scottish male" rather than a freelancing
"middle-aged man" that fights the clip during Dramabox's cloning).

Input: `01_characters.json` + voicebank/manifest.json.
Output: `04_voice_assignments.json` mapping each character (and the Narrator)
to a specific voicebank clip.

Two LLM calls per character:
1. **Target inference**: from the character's description + voice descriptor
   (passed through from any prior voice profile), Gemma emits search metadata:
   `{ gender, age_band, accent_keywords, timbre_keywords, manner_keywords }`.
2. **Ranked match**: hard-filter the voicebank by gender + age_band, soft-
   filter by accent, then ask Gemma to rank the remaining candidates and pick
   a top-3.

**Narrator handling.** The Narrator is synthesised (gender + age inferred from
the prior s3 voice descriptor if one exists, else 'male/adult') and cast like
any other character. The pipeline launcher's `--narrator-clip <clip_id>`
option overrides the LLM pick — the Narrator voice is the single most
audible voice in the finished audiobook and is usually best chosen by hand.

**Cross-volume reuse.** For non-first volumes, the previous volume's
`04_voice_assignments.json` is loaded; any character whose canonical name
matches a prior assignment keeps the prior clip and skips both LLM calls.
This guarantees voice continuity across a series.

### 2.5 Stage 3 — Director (stage directions)

This is the stage that **aligns the pipeline to Dramabox's input format**.
Dramabox expects screenplay-style prompts:

```
[Lord Vex, mid-50s, baritone, weary disappointment]
"You're late."
[Mira, breathless, defensive]
"I came as soon as I could."
```

The Director runs **after** voice cast (§2.4), so it knows each speaker's
assigned reference clip and can generate a voice descriptor that's
**consistent with that clip's actual gender / age / accent**. For each beat:

1. Speaker descriptor — derived from BOTH the character's personality (s1)
   AND the assigned clip's metadata (Stage V). A character cast on a
   `female/adult/england` clip gets a descriptor that says "adult British
   female", never "young adult male".
2. Emotional state inferred from local context (prior 2 beats + current).
3. Performance cues (`whispered`, `interrupted`, `laughs softly`).

A merge pass fuses consecutive same-speaker beats (capped at ~500 chars per
beat, the empirically-validated sweet spot for Dramabox quality — see §2.6).

Output: `03_directed/<chapter_id>.json` — same shape as scenes, but each
dialogue beat gains a `direction` string and a `prompt` field with the
fully-formatted Dramabox input.

LLM call is **per scene**, not per beat — context matters and per-beat would
hammer the model with redundant prompts.

### 2.6 Stage 4 — TTS (Dramabox)

For each beat in scene order:
- Load Dramabox once (it's 3.3B + Gemma 3 12B conditioner — sticky in VRAM).
- Look up the assigned `ref_clip` for the speaker from `04_voice_assignments.json`.
- Render `prompt` → `<beat_id>.wav` (48 kHz stereo).
- Cache by content hash of `(prompt, ref_clip_filename, model_version)` so
  re-runs after editing one line don't re-render the whole book.

Output: `05_audio/<chapter_id>/<beat_id>.wav` + `manifest.json` per chapter.

**Beat length matters.** Empirically Dramabox renders best between 20–60 s of
audio (~250–700 chars of English text). The Director's merge pass caps
fused beats at ~500 chars and the prompt pipeline splits any longer source
narration at sentence boundaries before this stage runs.

**Auto-retry.** Long Dramabox runs can SIGKILL after 10–15 min of continuous
denoise (likely CUDA fragmentation on RTX 50-series). The bundled
`scripts/s4_retry.sh` re-invokes the stage until it exits clean; the
content-hash cache ensures every restart is a cheap resume.

**Throughput note.** Dramabox is diffusion-based; expect ~real-time-to-2×-RT
on a 4090. A 100k-word novel ≈ 9 h audio ≈ 2-3 h render (warm). Plan for an
overnight run for a full novel.

### 2.7 Stage 5 — Mix

Concatenate beats with silence padding (configurable, defaults: 250 ms intra-
scene, 1 s inter-scene, 2 s inter-chapter). All audio plumbing is delegated to
system `ffmpeg` — no in-process audio decoding.

Pipeline per chapter, then per book:
1. Generate three silence WAVs at 48 kHz stereo.
2. Concat each chapter's beats interleaved with the silences → chapter WAV.
3. Concat all chapter WAVs with inter-chapter silence → book WAV.
4. Single-pass `loudnorm` to target −18 LUFS / −2 dB TP.
5. AAC encode and mux into `.m4b` (mp4 container) with chapter markers via
   ffmetadata. `+faststart` flag makes the file streamable.

Output: `06_final/<title>.m4b` with chapter markers + a
`<title>.timings.json` sidecar (chapter offsets for debugging / re-encodes).

### 2.8 Stage 6 — Sync layer (optional)

Players that highlight the current beat in sync with playback (WebKit reader,
Audiobookshelf, Plex Audiobooks, a custom front-end) need a mapping from
audio time → highlighted text span in the source. Stage 6 produces that
mapping by re-aligning the Stage-3 beats onto the original EPUB's XHTML.

Implementation: [`src/lnvox/stages/s6_sync.py`](src/lnvox/stages/s6_sync.py),
CLI `lnvox s6 <book> [--epub PATH]`.

Inputs: `03_directed/*.json` (beat texts) + `05_audio/<chapter>/manifest.json`
(per-beat durations) + original EPUB from `epubs/<series>/<vol>.epub` (the
`source_parts` field in `.epub_meta.json` maps each `chapter_id` to the
originating XHTML stems).

Outputs under `artifacts/<book>/07_sync/`:

- `<book>.epub` — a copy of the original EPUB with every matched beat's text
  wrapped in `<span class="lnvox-beat" data-beat-id="<beat_id>">…</span>`. A
  beat that straddles multiple text nodes gets one span per node, all sharing
  the same `data-beat-id`. Structure / styling / metadata otherwise
  byte-identical (mimetype stays STORED, etc.).
- `sync_manifest.json` — `beats[]` (per matched beat `{beat_id,
  data_beat_id, chapter_id, xhtml, type, speaker, start_seconds, end_seconds,
  match_confidence}`), `images[]`, and a top-level `match_confidence`
  histogram. Timings are cumulative through the Stage-5 silence layout
  (intra/inter-scene/inter-chapter), so the silence flags passed to
  `lnvox s6` MUST match Stage 5's.
- `images[]` (in the same manifest) — one entry per embedded illustration:
  `{src, xhtml, spine_page, after_chapter, before_chapter, after_beat_id,
  before_beat_id, trigger_seconds}`. Light-novel inserts/color/bonus pages
  are their own image-only spine items (skipped from text by Stage 0a); we
  walk the OPF **spine order** to place each between the preceding XHTML
  part's last beat and the following part's first beat — per-PART, so an
  insert between `chapterN.xhtml` and `chapterN_1.xhtml` triggers at the
  right mid-chapter beat. `before_beat_id: null` ⇒ end-matter shown after
  the final beat; `after_beat_id: null` ⇒ front matter (cover/TOC) at 0 s.
- `unmatched.json` — beats the matcher couldn't anchor (usually genuine
  Stage-2 paraphrases / hallucinations).

**Algorithm.** Per chapter, all of its `source_parts` XHTML are concatenated
into one normalized "master shadow" string with a parallel
`[char_index → (text_node, original_offset)]` map. Matching is then **two
passes**:

1. **Shadow + DOM index.** Walk text nodes; build the normalized shadow and
   the offset map. Normalization (search-side only — original casing kept in
   the map): lowercase, smart→straight quotes, em/en dash→`-`, collapse
   whitespace runs to a single space. (Ligatures / `…` deliberately left as-is
   so the normalized↔original char-offset mapping stays 1:1.)

2. **Pass 1 — strict, forward.** Only `exact` (short beats) and `anchored`
   (head + tail both found) matches, advancing a cursor. A per-match
   **forward-jump cap** (`_MAX_FORWARD_JUMP = 8000`) bounds how far ahead a
   match may start — without it, one false-positive anchored match leaping to
   the chapter's end strands every later beat (observed dropping volume-02
   chapters from ~95% → ~35%). Lenient fallbacks are off in Pass 1 precisely
   because they false-positive on recurring phrases and poison the cursor.

3. **Pass 2 — lenient gap-fill.** For each unmatched beat, search only the
   gap between its bracketing matches (plus `_PASS2_BACKWARD_SLACK = 2000`
   chars of backward slack, because Stage 2 sometimes reorders dialogue
   attribution — source `"Patrick admitted, '…'"` becomes a dialogue beat then
   a `"Patrick admitted"` narration beat that's *earlier* in the source).
   Fallback ladder: `head-only`, `tail-only`, `backtrack`, then fuzzy
   `SequenceMatcher` (≥`_FUZZY_MIN_RATIO=0.20` of the beat). Lenient matches
   claim ONLY the verified anchor/substring, never `cursor→anchor`, so an
   imprecise match can't swallow text the next beat needs. Repeated up to 3
   rounds.

4. **DOM wrapping.** Group matches by node; split each node into
   text/span/text/… parts in one pass so multiple beats per node work.
   Repack into a new EPUB, overlaying only the modified XHTML.

**Real-data failure modes handled**: dropped attribution tags (anchor gap
tolerance); paraphrased head OR tail (`head-only`/`tail-only`); reordered
attribution (Pass 2 backward slack); sentence-split narration
(`_split_long_text`) and same-speaker merge (`_merge_same_speaker`) staying
ordered via the cursor.

**Measured**: level99/volume-01 → 98.9%, level99/volume-02 → 95.7%. The
unmatched remainder are genuine Stage-2 hallucinations (independent per-beat
ceiling 92–94%; two-pass + fuzzy recovers reordered attribution to exceed it).

**Tuning knobs** (top of `s6_sync.py`): `_MAX_FORWARD_JUMP` (8000),
`HEAD_ANCHOR_LEN`/`TAIL_ANCHOR_LEN` (30/30), `_FUZZY_MIN_RATIO` (0.20),
`_BACKTRACK_WINDOW` (1500), `_PASS2_BACKWARD_SLACK` (2000).

## 3. Module layout

```
ln-vox/
├── DESIGN.md                 ← this file
├── pyproject.toml
├── src/lnvox/
│   ├── ingest/               ← txt/epub/md parsers
│   ├── llm/
│   │   ├── client.py         ← vLLM OpenAI-compatible client
│   │   ├── prompts/          ← jinja templates, one per stage
│   │   └── schemas.py        ← pydantic models for stage outputs
│   ├── ingest/
│   │   ├── text.py           ← folder-of-.txt parser (Stage 0)
│   │   └── epub.py           ← EPUB → novels/ layout (Stage 0a)
│   ├── stages/
│   │   ├── s1_characters.py
│   │   ├── s2_scenes.py
│   │   ├── s3_director.py
│   │   ├── s4_tts.py
│   │   ├── s5_mix.py
│   │   └── s6_sync.py        ← EPUB beat-span re-alignment (Stage 6)
│   ├── voices/               ← voice casting subsystem (§6)
│   ├── series.py             ← hierarchical book-id / prior-volume helpers
│   └── cli.py                ← per-stage subcommands (ingest, ingest-epub, s1…s6, voice, audio)
├── artifacts/                ← gitignored; per-book working dir
└── voicebank/                ← gitignored; ref clips + metadata
```

Each `stages/sN_*.py` exposes `run(book_id, config) -> Path` and is callable
in isolation. The orchestrator (`cli.py`) is a thin wrapper that chains them
and skips stages whose outputs are newer than inputs.

## 4. Serving topology

- **vLLM server** (one process) hosts Gemma 4 with `--enable-prefix-caching`.
  All four LLM stages reuse it. Use the OpenAI-compatible endpoint so we can
  swap to Claude/Gemini for evals by changing one env var.
- **Dramabox** runs as a separate Python process — keeps the audio model
  isolated from the LLM's VRAM and lets us schedule it after all LLM stages
  finish (or on a second GPU in parallel).
- **No queue/broker in v1.** Stages are local function calls. If we later need
  parallel chapter rendering, drop in `concurrent.futures.ProcessPoolExecutor`
  for stage 4.

## 5. Storage & idempotency

- Every stage output is content-hashed; the orchestrator skips work whose
  inputs+config+model-version haven't changed.
- TTS cache lives at `cache/tts/<sha256>.wav` — survives book deletions so
  you can re-run the same beat with a different ref clip cheaply.
- All JSON is pretty-printed for human debugging.

## 6. Voice casting subsystem

### 6.1 Voicebank

`voicebank/manifest.json` indexes every reference clip with normalised
attributes. `voicebank/clips/<id>.wav` holds the audio (mono 24 kHz, 8-20 s).

```json
{
  "id": "cv_051e865815e5",
  "source": "common_voice",
  "clip_path": "clips/cv_051e865815e5.wav",
  "duration_seconds": 13.39,
  "gender": "female",
  "age_band": "adult",
  "accent": "england",
  "sample_sentences": ["It has been called the center of Cherokee culture."],
  "license": "CC0",
  "notes": "Common Voice (validated.tsv); speaker 051e865815e5…"
}
```

**Seed.** v1 uses **Mozilla Common Voice 25** exclusively. The dataset
manifests `gender`, `age` (which we map to `teen / young_adult / adult /
elder`), and `accents` (kept as the 17 official short codes: `us`, `england`,
`indian`, `canada`, `australia`, `scotland`, `african`, `newzealand`,
`ireland`, `philippines`, `hongkong`, `singapore`, `malaysia`, `wales`,
`bermuda`, `southatlandtic`, `other`).

Common Voice clips are 3-8 s individually, too short for stable cloning.
The loader (`scripts: lnvox voice seed-cv`) groups clips by `client_id`
(speaker) and concatenates the top-voted utterances into ~12 s ref clips.

Common Voice 23+ is distributed only via Mozilla Data Collective, so the
tarball is downloaded by the user and the loader reads from the local
extraction.

**Additional sources** (planned, share the same manifest schema):
- Artie Bias Corpus
- Meta Fair-Speech
- Speech Accent Archive

### 6.2 Matching (Stage V)

Two LLM calls per character:

1. **Target inference** (`voice_target.jinja`) — reads the character's
   description + voice descriptor and emits `gender`, `age_band`,
   `accent_keywords`, `timbre_keywords`, `manner_keywords`.

2. **Ranked match** (`voice_match.jinja`) — given the candidates surviving
   the hard filter on gender + age_band and the soft filter on accent,
   Gemma ranks them top-3 with a short reason for each.

The top-1 is committed to `04_voice_assignments.json`; the other two
candidates are kept so a human reviewer can swap in a different pick without
re-running the LLM.

### 6.3 Manual narrator selection

The narrator is voiced over a *huge* portion of the book (~60-70% of beats
in third-person novels). Auto-casting it usually produces something
acceptable but rarely something great. The pipeline launcher exposes a
`--narrator-clip <clip_id>` flag so the user picks the narrator by ear after
seeding the voicebank.

If `--narrator-clip` is omitted, the narrator is auto-cast like any other
character with synthesised demographics derived from the voice descriptor.

### 6.4 Cross-volume continuity

Voice continuity across a series is non-negotiable for listeners.
When a non-first volume of a series is processed, the pipeline:

- Auto-detects prior volume artifacts (`artifacts/<series>/volume-*/`).
- Loads the most recent prior `04_voice_assignments.json`.
- For every character in the current volume whose canonical name matches a
  prior assignment, **reuses the prior clip** verbatim (no LLM call).
- For characters new to this volume, runs the standard two-call matching.
- For the Narrator, the prior clip is reused **unless** `--narrator-clip`
  is explicitly passed (allowing intentional narrator changes between
  volumes if desired).

### 6.5 YouTube ref clips (deferred to v2)

Not yet implemented. When added, will sit alongside `common_voice` as a
source type with `license: "personal_use_only"` flag that propagates into
final-output gating.

## 7. Configuration

Single `config.yaml` per book:

```yaml
book_id: "stormlight_ch1_3"
input: "books/stormlight.epub"
chapters: [1, 2, 3]            # subset for dev
llm:
  endpoint: "http://localhost:8000/v1"
  model: "google/gemma-4-31B-it"
  dev_model: "google/gemma-4-E4B-it"
tts:
  model: "ResembleAI/Dramabox"
  device: "cuda:0"
  cfg_scale: 3.0
mix:
  intra_scene_silence_ms: 250
  inter_scene_silence_ms: 1000
  inter_chapter_silence_ms: 2000
  loudness_target_lufs: -18
voicebank: "voicebank/"
```

## 8. Open questions / things to decide before coding

1. **EPUB chapter detection** is famously messy. Do we want a manual TOC
   override file, or trust the heuristic and let the user fix Stage 0 output?
2. **Narrator voice** — single fixed voice for the whole book, or per-POV
   character (first-person novels)? POV detection is doable but adds a stage.
3. **Evaluation harness.** Should we build a small "spot-check" tool that
   plays the first beat of each scene for QA before committing to a
   full-book render? Strongly recommended given multi-hour render times.
4. **Watermark.** Dramabox watermarks every output. Fine for personal use;
   worth noting if you ever want commercial distribution (need a Resemble
   license).
5. **Long-context strategy.** Gemma 4 has 256K context but quality on long
   inputs varies. Default plan is "per chapter" granularity. Worth a
   one-day spike to measure: does feeding 3 chapters at once improve
   character/scene consistency enough to justify the cost?

## 9. Pipeline launcher

`scripts/run_pipeline.sh <series>/<volume-XX> [--narrator-clip cv_xxx]`
orchestrates the full per-volume flow with the GPU handoff:

1. **LLM phase** (vLLM up, Dramabox down):
   - Ingest
   - s1 (cast, with cross-volume merge if prior volumes exist)
   - s2 (scenes)
   - Voice cast (with `--narrator-clip` override + cross-volume reuse)
   - s3 (director, using assigned clip metadata)
2. **GPU handoff**: stop vLLM, free VRAM.
3. **TTS phase** (Dramabox up):
   - s4 (TTS, wrapped in `s4_retry.sh`)
4. **No-GPU phase**:
   - s5 (mix + m4b)

The launcher prints clear "STOP vLLM NOW" / "STOP Dramabox NOW" prompts at
each handoff. It can be re-run safely: each stage is idempotent and skips
work whose inputs haven't changed.

## 10. Dependencies (actual)

Core (`pyproject.toml`):
- `openai`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `jinja2`

`serve` extra (vLLM phase):
- `vllm>=0.19.0`, `torch` (cu130 wheels via `[tool.uv.sources]`)

`voice` extra (voicebank seeding):
- `soundfile`, `librosa`, `tqdm`
- System: `ffmpeg` (MP3 decode via librosa)

`tts` extra (Dramabox phase):
- `soundfile`, `huggingface-hub`
- Vendored Dramabox at `external/DramaBox/` (cloned + installed by
  `scripts/setup_dramabox.sh`)

System for stage 5: `ffmpeg` (concat / loudnorm / AAC mux).

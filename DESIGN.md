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

**Global merge** runs in two phases, because feeding every per-chapter list
(full descriptions + evidence) straight to the LLM blows the context window —
a character recurring across N chapters costs ~N× its JSON, and a long volume
exceeds the model's input budget (observed: ~36K input tokens on a 25-chapter
volume, over the 64K ceiling once the output budget is added).

1. **Deterministic clustering** (`cluster_characters`): union-find over the
   per-chapter entries, fusing any that share a normalized name/alias key, plus
   a fuzzy name pass (`difflib` ratio ≥ 0.9, min length 4) for transliteration
   / typo variants ("Gunther"/"Gunter"). Enumerated names whose digit runs
   differ ("Knight 1"/"Knight 2") are never fuzzy-fused. Each cluster combines
   its members' fields by rule (canonical = most-frequent name form, alias =
   union, gender/age = majority vote, description = longest, evidence = union).
2. **LLM refine** (`merge_clusters`): a Gemma 4 call receives only a COMPACT
   per-cluster summary (capped description, ≤2 evidence quotes, a `chapters`
   occurrence count) — bounded by distinct-character count, not chapter count
   (~91× smaller on the 25-chapter case). It does the judgment clustering
   can't: fusing the same person under unrelated names, polishing descriptions,
   and dropping trivial one-chapter background characters. A hard input-size
   guard (`_MERGE_INPUT_CHAR_BUDGET`) falls back to the deterministic merge
   rather than risk a context-length error.

Outputs: `01_characters.json` (the merged cast) and
`01_characters_merge_log.json` — provenance recording what merged into each
cluster, which clusters are "lone" (single chapter), and each one's final
disposition (kept / dropped / new-in-final).

> **Model choice.** Use **Gemma 4 E4B** for dev/iteration (fits in ~10 GB
> VRAM, ~2× faster) and **Gemma 4 31B Dense** for production runs. Both via
> the same vLLM endpoint — only `--model` changes.

### 2.3 Stage 2 — Scene & speaker segmentation (Gemma 4)

Input: chapter text + global cast list. Output per chapter:
`02_scenes/<chapter_id>.json`.

**Two passes, not one.** A single call previously did three different jobs —
find scene boundaries, tag every line as narration/dialogue, and reproduce the
text — in one large output. Splitting them raises fidelity (which §2.8 shows is
the hard ceiling on sync match rate) because each prompt is simpler, and keeps
each call small enough to afford the `source_span` field below.

**Pass 2a — Scene boundaries.** Input: the chapter text with chapter-global
paragraph numbers. Output: scenes with `scene_id`, `location_hint`, the `cast`
present, and `start_paragraph` / `end_paragraph` — **no
beats**. This is a bounded task (pick boundaries among numbered paragraphs),
the same kind the chunker already does reliably
([`chunker.py`](src/lnvox/llm/chunker.py)), so the model is dependable here.
(The `cast` list is 2a metadata used to prime/debug 2b; it is not persisted on
the final merged `Scene`, which keeps only the paragraph range + beats — see
the JSON below.)

**Pass 2b — Beat tagging, per scene.** Input: *only* that scene's paragraphs
(sliced from the source by 2a's range) + cast. Output: the scene's `beats`,
each with `type` / `text` / `speaker` **plus `source_span`**. The small,
single-scene context is what makes both the higher fidelity and the extra
`source_span` output affordable.

`source_span` — the **verbatim, contiguous slice of the source text the beat is
grounded in**. It is lossless, in deliberate contrast to `text`, which is lossy
(quote marks stripped, `"she said"` attribution dropped, whitespace collapsed).
`source_span` is the sync key consumed by §2.8; `text` remains what is sent to
TTS. The two differ exactly where the lossy transform happened, which is why
`source_span` — not `text` — is the reliable anchor back to the original.

```json
{
  "chapter_id": "ch03",
  "scenes": [
    {
      "scene_id": "ch03_s1",
      "location_hint": "Vex's study at dusk",
      "start_paragraph": 12,
      "end_paragraph": 19,
      "beats": [
        {"type": "narration", "text": "The duke turned from the window.",
         "source_span": "The duke turned from the window."},
        {"type": "dialogue", "speaker": "Lord Vex", "text": "You're late.",
         "source_span": "“You're late,” Vex said without turning."},
        {"type": "dialogue", "speaker": "Mira", "text": "I came as soon as I could.",
         "source_span": "“I came as soon as I could.”"}
      ]
    }
  ]
}
```

Note the second beat: `text` drops the `"Vex said without turning"` tag, but
`source_span` keeps it — so the span matches the source exactly even though
`text` no longer does.

**Paragraph numbering — chapter-global.** The chapter text is split on `\n\n`
into a paragraph list *once*; chunking (§2.3 chunker) accumulates whole
paragraphs so each chunk carries its base paragraph index, and Pass 2a emits
chapter-global paragraph numbers. Scene ranges are therefore directly usable by
s6 with no per-chunk offset bookkeeping. (This replaces the char-based chunker
splitting that returns opaque strings.)

`narration` is voiced by a fixed Narrator character (auto-added to cast).
Quoted speech inside narration is attributed when the LLM can infer it,
otherwise stays as narration.

**Failure modes to watch.**
- Long passages with `"…," she said` style — the LLM must split the dialogue
  from the tag in `text`. The `source_span` for that beat should *keep* the tag.
- `source_span` drift: if the model paraphrases the span instead of copying it,
  the exact match in §2.8 fails and the beat falls back to fuzzy matching. The
  rate of exact `source_span` hits is therefore a direct s2-fidelity metric
  (see §2.8).

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

**Anchor from Stage 2.** Each beat now carries `source_span` — the verbatim
source slice it was grounded in — used as the **exact** match key. Because it
is lossless it matches the original directly, where the lossy `text` could not.
The per-scene `start_paragraph` / `end_paragraph` fields are also carried
through (s2 → s3), but they index the *chapter `.txt`* paragraph list, which is
a **different coordinate space** from the EPUB-XHTML shadow s6 matches against
(ingest reflows the text). Mapping paragraph index → shadow offset reliably is
itself an alignment problem, and exact `source_span` matching removes the
false-positive pressure that a hard scene window was meant to relieve — so s6
keeps the cheaper cursor + `_MAX_FORWARD_JUMP` forward cap rather than building
that mapping. The paragraph ranges remain available as scene metadata for a
future reader UI / debugging.

**Algorithm.** Per chapter, all of its `source_parts` XHTML are concatenated
into one normalized "master shadow" string with a parallel
`[char_index → (text_node, original_offset)]` map. Matching then proceeds:

1. **Shadow + DOM index.** Walk text nodes; build the normalized shadow and
   the offset map. Normalization (search-side only — original casing kept in
   the map): lowercase, smart→straight quotes, em/en dash→`-`, collapse
   whitespace runs to a single space. (Ligatures / `…` deliberately left as-is
   so the normalized↔original char-offset mapping stays 1:1.) The same
   normalization is applied to each `source_span` before matching.

2. **Primary — exact `source_span`.** Forward from the cursor (bounded by
   `_MAX_FORWARD_JUMP`), search for the normalized `source_span`; on a hit,
   claim exactly that span (confidence `span-exact`) and advance the cursor.
   This is the common path and is unambiguous.

3. **Fallback — the legacy fuzzy ladder**, used only for beats whose
   `source_span` is missing (legacy data / split remainders) or did not match
   exactly (drift / hallucination):
   - **Strict, forward (Pass 1).** `exact` (short beats) and `anchored`
     (head + tail of `text` both found), advancing the cursor. The
     `_MAX_FORWARD_JUMP = 8000` cap bounds how far ahead a match may start.
   - **Lenient gap-fill (Pass 2).** For each still-unmatched beat, search only
     the gap between its bracketing matches (plus `_PASS2_BACKWARD_SLACK = 2000`
     chars backward slack, because Stage 2 sometimes reorders dialogue
     attribution). The beat's `source_span` is retried exactly within the gap
     first, then the ladder: `head-only`, `tail-only`, `backtrack`, then fuzzy
     `SequenceMatcher` (≥`_FUZZY_MIN_RATIO=0.20`). Lenient matches claim ONLY
     the verified anchor/substring, never `cursor→anchor`. Repeated up to 3
     rounds.

4. **DOM wrapping.** Group matches by node; split each node into
   text/span/text/… parts in one pass so multiple beats per node work.
   Repack into a new EPUB, overlaying only the modified XHTML.

**Fidelity metric (free).** The fraction of beats resolved by step 2 (exact
`source_span`) vs. forced into the step-3 fallback is a direct measure of
Stage-2 grounding fidelity — surfaced in the `match_confidence` histogram (the
`span-exact` bucket). Contiguity gaps between consecutive `source_span`s within
a scene also flag text the model *omitted*, a stronger omission detector than
`unmatched.json`.

**Real-data failure modes handled**: dropped attribution tags (kept in
`source_span`, or anchor gap tolerance in fallback); paraphrased `source_span`
(falls back to `head-only`/`tail-only`); reordered attribution (fallback
backward slack); sentence-split narration (`_split_long_text`) and same-speaker
merge (`_merge_same_speaker`) staying ordered via the cursor.

**Measured (pre-`source_span` baseline)**: level99/volume-01 → 98.9%,
level99/volume-02 → 95.7% using the fuzzy ladder alone. The unmatched remainder
were genuine Stage-2 hallucinations (independent per-beat ceiling 92–94%). The
`source_span` anchor is expected to lift the exact-match share
well above this and shrink the fuzzy-fallback population; re-measure after
implementation.

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
- **Apple Silicon variant.** vLLM is swapped for Apple `mlx_lm.server` (same
  OpenAI endpoint contract, so `LLMClient` is unchanged) and Dramabox runs on
  the MPS device with quantization + `torch.compile` disabled. Full details
  and known limitations live in §11.

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
  device: "cuda:0"                 # or "mps" on Apple Silicon — see §11
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

On Apple Silicon the same launcher runs `scripts/serve_mlx.sh` instead of
`scripts/serve_vllm.sh` for the LLM phase and passes `--device mps` to s4 —
no other orchestration changes (see §11).

## 10. Dependencies (actual)

Core (`pyproject.toml`):
- `openai`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `jinja2`

`serve` extra (vLLM phase, Linux/CUDA):
- `vllm>=0.19.0`, `torch` (cu130 wheels via `[tool.uv.sources]`, marker-gated
  to `sys_platform == 'linux'` so `uv sync` doesn't try to fetch CUDA wheels
  on macOS — see §11).

`mlx` extra (Apple Silicon LLM phase):
- `mlx-lm` (provides `mlx_lm.server`, OpenAI-compatible). Marker-gated to
  `sys_platform == 'darwin'`. See §11 for the serving topology.

`voice` extra (voicebank seeding):
- `soundfile`, `librosa`, `tqdm`
- System: `ffmpeg` (MP3 decode via librosa)

`tts` extra (Dramabox phase):
- `soundfile`, `huggingface-hub`
- Vendored Dramabox at `external/DramaBox/` (cloned + installed by
  `scripts/setup_dramabox.sh`)

System for stage 5: `ffmpeg` (concat / loudnorm / AAC mux).

## 11. Apple Silicon path (secondary target)

Linux + CUDA stays the primary supported topology. Apple Silicon is a
second target: the design must keep CUDA working unchanged, while also
producing an `.m4b` end-to-end on an M-series Mac. This section is the
contract for what differs and what stays the same.

### 11.1 Topology

```
                        ┌──────────────────────────┐
   LLM phase            │ scripts/serve_mlx.sh     │   (Apple Silicon)
                        │   → mlx_lm.server :8000  │
                        └────────────┬─────────────┘
                                     │  same OpenAI endpoint contract
   (or, on Linux/CUDA)               ▼
                        ┌──────────────────────────┐
                        │ scripts/serve_vllm.sh    │   (Linux + CUDA)
                        │   → vllm OpenAI :8000    │
                        └────────────┬─────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   LLMClient         │  unchanged on either path
                          └──────────┬──────────┘
                                     │
                       s1 / s2 / voice / s3 (all stages)
                                     │
                                     ▼
   TTS phase            ┌──────────────────────────┐
                        │ DramaboxClient(          │   device picked from
                        │   device="cuda" | "mps") │   config / CLI flag
                        └──────────────────────────┘
```

The only stage that has a device of its own is s4. Every other stage is
either pure-Python (ingest, mix, sync) or talks to the LLM over HTTP — so
the *only* code that knows whether we're on CUDA or MPS is the s4 client
factory.

### 11.2 LLM side — `mlx_lm.server`

- `mlx_lm.server` ships an OpenAI-compatible `/v1/chat/completions`
  endpoint. `LLMClient` ([client.py:57-60](src/lnvox/llm/client.py#L57-L60))
  doesn't care which backend is behind that URL, so the only artifact
  needed on the MPS path is a new launcher: `scripts/serve_mlx.sh`
  (model + port + max-tokens — much smaller surface than the vLLM script).
- Models are pulled from `mlx-community/*` (pre-converted MLX weights). The
  closest matches to the primary Gemma 4 picks are tracked in the script's
  comments; once Gemma 4 MLX checkpoints are routinely available they
  become the default. Until then the dev/E4B path runs against the
  best-available MLX-quantized Gemma.

**Known limitations** of this path that the design accepts rather than
papers over:

1. **No `guided_json` enforcement.** vLLM honours
   `extra_body={"guided_json": schema}` (used in
   [client.py:118](src/lnvox/llm/client.py#L118)) so the model's output is
   constrained to the pydantic schema server-side. `mlx_lm.server` ignores
   this field today. The client already validates + retries via
   `structured()`, so behaviour degrades from "guaranteed structural match"
   to "validate-and-retry"; expect a higher first-attempt parse-fail rate
   on Mac. Track upstream MLX structured-output work and revisit.
2. **No `repetition_penalty` knob.** The vLLM-specific
   `extra_body["repetition_penalty"]` lever
   ([client.py:119-120](src/lnvox/llm/client.py#L119-L120)) isn't honoured
   either. The runaway-loop escape hatch documented in
   [config.py:16-19](src/lnvox/config.py#L16-L19) goes away; the
   workaround on MPS is to bump temperature or switch to a less
   loop-prone checkpoint.
3. **No prefix caching across calls.** vLLM's `--enable-prefix-caching`
   amortises shared system+user prompt prefixes across the many
   per-chapter/per-scene LLM calls; mlx-lm has no equivalent today. Stage
   1 (per-chapter) and stage 3 (per-scene) make many calls with shared
   system prompts, so the wall-clock penalty on Mac is real but bounded —
   the prompts aren't huge.

These are explicitly *acceptable* losses for v1; we don't add a fallback
guided-decoding layer (e.g. outlines) to the client just yet. The bar is
"design parity is preserved at the contract level; performance and
fidelity may differ."

### 11.3 TTS side — Dramabox on MPS (best-effort)

Goal: pass `device="mps"` through to DramaBox **without touching
DramaBox's source**. Anything beyond what `DramaboxClient` already wraps
([dramabox_client.py](src/lnvox/tts/dramabox_client.py)) is out of scope
for v1.

What `DramaboxClient` needs to do on MPS (small wrapper changes, no
DramaBox patches):

| Knob              | CUDA default   | MPS override       | Why                                                                                                                                |
|-------------------|----------------|--------------------|------------------------------------------------------------------------------------------------------------------------------------|
| `device`          | `"cuda"`       | `"mps"`            | CLI / config flag, already supported by the wrapper constructor.                                                                   |
| `dtype`           | `"bf16"`       | `"fp16"`           | MPS has materially worse bfloat16 coverage than fp16; forcing fp16 sidesteps the worst of it.                                      |
| `bnb_4bit`        | `True`         | `False`            | bitsandbytes is CUDA-only. No MPS 4-bit path exists in DramaBox.                                                                   |
| `compile_model`   | `True`         | `False`            | `torch.compile` is flaky on MPS for diffusion models; the default eager path is the safer baseline.                                |

These defaults are picked in `DramaboxClient.__init__` based on the
`device` argument; the CUDA path is byte-identical to today's behaviour.

**Known limitations** the design accepts:

1. **No DramaBox source patches.** `torch.cuda.empty_cache()` and
   `torch.cuda.memory_allocated()` are called unconditionally inside
   DramaBox ([blocks.py:433,486](external/DramaBox/ltx2/ltx_pipelines/utils/blocks.py)).
   On MPS these raise / return zero. If a particular call site errors,
   the TTS run dies — we surface the trace rather than monkey-patch from
   our wrapper. Upstreaming or forking DramaBox is explicitly out of
   scope for v1.
2. **2× Gemma-encoder memory.** With `bnb_4bit=False` the prompt-encoder
   Gemma (3 12B) loads in fp16 instead of 4-bit. Budget ≈ 24 GB unified
   memory for the encoder alone. The pipeline needs a 36 GB+ Mac to fit
   transformer + encoder + KV-cache + activations.
3. **Throughput.** DramaBox is diffusion-based and unoptimised for MPS.
   Expect 5–10× real-time render (vs. ~1–2× on a 4090). A 9 h audiobook
   becomes a 2-day render on Mac. The s4 cache and `s4_retry.sh` resume
   semantics are unchanged and remain the right primitives.

### 11.4 Code surface affected (design-only listing — no code yet)

| Area                                          | Change                                                                                                                                                       |
|-----------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `pyproject.toml`                              | Make `[tool.uv.sources].torch` Linux-only via a `sys_platform` marker; add an `mlx` optional extra with `mlx-lm; sys_platform == 'darwin'`.                  |
| `scripts/serve_mlx.sh` (new)                  | Apple Silicon analog to `serve_vllm.sh`. Picks an `mlx-community/*` Gemma checkpoint by `LNVOX_LLM_MODEL`; launches `python -m mlx_lm server --port …`.      |
| `src/lnvox/tts/dramabox_client.py`            | `DramaboxClient.__init__` reads `device`, applies the per-device defaults table above. CUDA default behaviour preserved exactly.                             |
| `src/lnvox/cli.py` (`stage4 --device`)        | Default flips from hard-coded `"cuda"` to an auto-detect helper (`mps` if `torch.backends.mps.is_available()`, else `cuda`). The flag stays a manual override.|
| `scripts/run_pipeline.sh`                     | Pick `serve_mlx.sh` when `uname -s == Darwin`. Pass `--device mps` to `lnvox s4` on the same condition. No stage ordering changes.                            |

Nothing about the stage contracts (§2), the artifact layout (§5), the
voice subsystem (§6), or any prompts (`src/lnvox/llm/prompts/`) changes.

### 11.5 What's explicitly **not** in this design

- Mac CI. Nothing is automated; the Mac path is dev-grade in v1.
- Replacing `bitsandbytes` with an MPS-side quant alternative (e.g.
  per-channel int8 via `torch.ao.quantization`).
- Outlines/LMFE-style guided decoding to recover the lost
  `guided_json` enforcement.
- Multi-device parallelism (CUDA box doing LLM while Mac does TTS, or
  vice versa). The pipeline launcher remains single-host.

Each of those is a sensible v2 thread; they're called out so we don't
accidentally drift into them while wiring up v1's Mac path.

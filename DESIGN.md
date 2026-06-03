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
drives the descriptor prefix Dramabox sees.

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
Dramabox expects a voice/performance descriptor immediately followed by the
quoted line — one beat per `prompt`:

```
Lord Vex, mid-50s, baritone, weary disappointment, "You're late."
Mira, breathless, defensive, "I came as soon as I could."
```

(The `prompt` field is built as `<direction>, "<text>"` — see
[s3_director.py](src/lnvox/stages/s3_director.py). Lecture mode (§13) builds
the same shape with the narrator descriptor and no performance cue.)

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

## 12. Voicebank Studio (curation GUI)

A standalone PySide6 desktop tool for curating the voicebank by ear. The
auto-seeder (`seed_from_common_voice`, §6.1) gets you a bulk voicebank
quickly, but picking *good* reference voices — and especially the narrator
(§6.3) — is a listening task the CLI can't do. The Studio is that listening
surface.

### 12.1 Scope

Operations 1–4 act on `voicebank/manifest.json` + `voicebank/clips/`;
operation 5 acts on a book's `04_voice_assignments.json`:

1. **Listen** — play any voicebank clip; see its full metadata.
2. **Import from Common Voice** — browse the raw `data/…/en` corpus by
   *speaker*, preview their individual mp3 utterances, preview the merged
   reference clip, and **promote** a speaker into the voicebank. Promotion
   reuses `build_speaker_clip()` verbatim, so a Studio-added clip is
   byte-for-byte the kind of clip the seeder would have produced (mono 24 kHz,
   top-voted utterances concatenated with 0.25 s gaps, demographics + up to 3
   sample sentences preserved).
3. **Add manual** — import an arbitrary wav/mp3 from disk (transcoded to mono
   24 kHz wav) and hand-enter its taxonomies (`gender`, `age_band`, `accent`,
   sentences, license, notes).
4. **Erase** — delete a clip from the manifest and remove its wav.
5. **Cast** (Casting tab) — open a `04_voice_assignments.json` (`BookCasting`,
   §6.2), see every character with its LLM `target` and top-3 `ranked`
   candidates, audition candidates *and* any clip in the voicebank, and
   reassign `assigned_clip_id` for any character by ear. Save writes the
   `BookCasting` back via `model_dump_json` — `target`, `ranked`,
   `candidates_considered`, and `voice_descriptor` are round-tripped untouched;
   only `assigned_clip_id` changes. This is the by-ear counterpart to the
   automated Stage-V match and the manual-narrator flow (§6.2–6.3).

Explicitly **out of scope**: editing existing clips' audio, multi-corpus
import (only the local CV layout is wired), and any pipeline invocation. The
Studio only reads/writes the voicebank and casting JSON; it never re-runs the
LLM matcher (a cross-gender/age cast just prompts a confirmation).

### 12.2 Packaging

Standalone — `scripts/voicebank_studio.py`, run with
`uv run python scripts/voicebank_studio.py` after `uv pip install PySide6`.
Not wired into the `lnvox` CLI and not added as a project dependency; PySide6
is a dev-machine-only concern and the pipeline never imports it. The script
*does* import `lnvox.voices` (schema, manifest I/O, `build_speaker_clip`) so
curation logic stays single-sourced with the seeder.

### 12.3 Common Voice browsing without loading 600 MB

`validated.tsv` is ~600 MB / 2.5 M rows; the GUI must not block on it. A
`QThread` worker streams the TSV with `csv.DictReader`, applies the same
`_eligible()` gate as the seeder (≥2 up-votes, 0 down-votes, mappable
age+gender), groups eligible rows by `client_id`, and emits each speaker the
moment it accumulates `min_utts` eligible rows *and* matches the active
attribute filters (gender / age / accent substring). It **stops early** once
`max_speakers` ready speakers are found, so a typical browse reads only the
head of the file. This trades the seeder's deterministic `client_id` ordering
for file order — fine for a browse tool, and called out here so nobody expects
the Studio to surface the *same* speakers the seeder would at a given cap.

### 12.4 Audio playback

Playback goes through `QMediaPlayer` + `QAudioOutput`, but every non-wav input
is first decoded (librosa) to a temp 24 kHz wav and that wav is played. This
sidesteps per-platform mp3-codec availability in the Qt multimedia backend:
the only format Qt is ever asked to play is wav. Merged-clip preview reuses
`build_speaker_clip()` into a temp dir (no manifest mutation) and plays the
result, guaranteeing the preview is exactly what promotion would write.

## 13. Lecture mode (single-voice, verbatim narration)

### 13.0 Goal & what lecture mode is / isn't

**Goal.** A second delivery mode for **non-fiction and technical books**: one
narrator voice, read straight through, with **no dramatization**. The source
text is used *as is* — the pipeline never invents scenes, never splits dialogue
from attribution, never casts a multi-voice ensemble, and never writes
emotional performance cues. It produces the same `.m4b` (+ optional synced
EPUB) the narration mode does, just with a single steady reader.

This is selected with a new `--mode lecture` flag (the existing multi-voice
behaviour is `--mode narration`, the default).

**What it deliberately drops vs. narration mode.** Stage 1 (character
extraction), Stage 2 (scene/speaker segmentation), and the dramatizing half of
Stage 3 (the Director: emotional state, performance cues, per-speaker
descriptors) all exist *only* to find characters, segment scenes, attribute
dialogue, and act it out. Every one of those is forbidden in lecture mode, so
all three collapse into a single deterministic stage (§13.3). There is no
character LLM, no scene LLM, no Director.

**What it keeps.** The verbatim-anchoring design that already runs through the
narration path. The `Beat`/`DirectedBeat` schema already separates `text` (the
lossy string sent to TTS) from `source_span` (the verbatim source slice used as
the §2.8 sync key). Lecture mode uses that exact split: `source_span` is the
untouched original, `text` is the speech-normalized form (§13.6). Because
`source_span` stays byte-faithful to the source, Stage 6 matching is
near-perfect (a far higher `span-exact` share than narration — there are no
paraphrasing scene/director passes to drift it).

**Non-goals (inherited from §0 plus).** No per-section voice switching, no
"read the code aloud" TTS of non-prose blocks (those are handled as *visual
elements*, §13.2/§13.5), no summarization or restructuring of the text.

### 13.1 Pipeline overview

```
   ┌──────────────────────┐        ┌────────────┐
   │ 0a. ingest-epub       │        │ Voicebank  │
   │  (classify · drop     │        └──────┬─────┘
   │   boilerplate · render│               │ narrator ref clip
   │   code/tables → PNG)  │               ▼
              │ 00_text.jsonl       ┌──────────────┐
              │ + Narrator-only     │ V. Voice cast│ (Narrator only)
              │   01_characters.json│  (--narrator │
              │ + non-prose blocks  │    -clip)    │
              ▼                     └──────┬───────┘
        ┌───────────────────────────┐     │ narrator descriptor
        │ L. lecture                │◄────┘
        │  (split + speech-normalize│
        │   → 03_directed/*.json)   │
        └──────────┬────────────────┘
                   ▼
            ┌──────────┐  ┌──────┐
            │ 4. TTS   │→ │ 5.   │→ .m4b
            │ (Dramabox│  │ Mix  │
            │  1 voice)│  │      │
            └──────────┘  └──┬───┘
                            ▼
                     ┌─────────────────────────┐
                     │ 6. Sync epub→spans       │
                     │  + non-prose visual      │
                     │    elements (like images)│
                     └─────────────────────────┘
```

Compared to narration mode (§1): **s1, s2, and the Director are replaced by the
single `lecture` stage; everything from Stage V onward is unchanged code.** The
only stage that knows which mode it's in is `lecture` (and a structure-aware
branch in ingest); s4/s5/s6 receive identical artifacts.

**Mode flag.** `scripts/run_pipeline.sh <book> --mode lecture
[--narrator-clip cv_xxx]`. The launcher's LLM phase shrinks to *just the
normalize pass* (§13.6) — no character/scene/director calls — then the same
vLLM→Dramabox GPU handoff. With `--no-normalize` (§13.6) lecture mode has **no
LLM phase at all**: the whole vLLM half of the launcher is skipped and the run
is ingest → cast → split → TTS → mix.

### 13.2 Ingest — block classification, drop list, and rendering

Technical books are full of blocks that read terribly aloud: code listings,
tables, math/equations, figure captions, footnotes — plus whole pages that
shouldn't be read at all (TOC, copyright, index). Lecture-mode ingest does three
things narration-mode ingest doesn't: **classify** every block, **drop** the
boilerplate, and **render** the keep-but-don't-narrate blocks (code/tables) into
images so they ride the *exact same path Stage 6 already uses for
illustrations* (§2.0, §2.8 `images[]`) — surfaced to the reader at the right
playback offset, never sent to TTS.

Stage 0a (`ingest-epub`, [`epub.py`](src/lnvox/ingest/epub.py)) gains a
`--mode lecture` branch implementing the steps below.

**(a) Classification — deterministic first, LLM only for the tail.** An EPUB
already encodes most of this; we lean on the markup and reach for the LLM only
where the markup is silent. The ladder, per block:

1. **Structural / semantic (free, deterministic).** `<pre>`/`<code>` → `code`;
   `<table>` → `table`; `<figure>`/`<figcaption>` → `figure`; footnote
   `<aside>` / `<*[epub:type=footnote|endnote]>` → `footnote`; MathML `<math>`
   / display-equation blocks → `equation`. EPUB3 `epub:type` landmarks and the
   NCX/nav document classify whole spine items: `toc`, `copyright-page`,
   `titlepage`, `index`, `bibliography` → **drop** (see (b)). This reuses and
   extends the stem regex Stage 0a already applies
   (`cover/toc/copyright/signup/insert\d+/…`).
2. **LLM fallback (only for untagged blocks).** Publishers vary wildly — some
   render code as monospace-styled `<p>` and ship boilerplate with no
   `epub:type`. Any block the rules can't classify gets a single cheap LLM call
   returning `{prose | code | table | drop}` + a one-line reason. Bounded by an
   input-size budget like the s1 merge (§2.2). Blocks the rules *did* classify
   never hit the LLM, so most books make few or zero calls — and a book with
   clean semantic markup makes **none**. (`--ingest-classifier none` forces
   rules-only: untagged ⇒ treated as prose.)

This is the same "deterministic clustering, LLM refine" division of labour as
§2.2 — fast, reproducible, debuggable, LLM only for the judgment rules can't make.

**(b) Drop list.** Dropped spine items / blocks are excised from
`00_text.jsonl` entirely (not rendered, not narrated, not a visual element).
Default drop set (configurable in `config.yaml`):

- `toc` — table of contents (a list of page numbers; never narrate).
- `copyright-page` / `titlepage` — copyright, ISBN/publisher boilerplate,
  "also by this author" ad pages.
- `index` / `bibliography` — back-of-book index and reference lists.

**Kept and narrated:** the narrative body, plus `preface` / `foreword` /
`introduction`, and — deliberately *not* in the default drop set —
`dedication` / `acknowledgments` / `colophon` (short, and some listeners want
them; flip them into `drop` per book if undesired).

**(c) Render code/tables → PNG.** This is the core of "make images from the code
and tables that are text." We already hold the verbatim block HTML, so:

- **Code** → syntax-highlight with **Pygments** (lexer guessed from the
  language class hint or content), wrap in a minimal HTML doc with a monospace
  theme.
- **Tables** → keep the `<table>` markup, apply a clean bordered CSS.
- Rasterize that HTML → PNG with **Playwright (headless Chromium)** at a
  reader-friendly width (~1080px) and 2× device-scale for crispness; tall
  listings/tables produce tall images the reader scrolls. Playwright lives
  behind an optional `render` extra (§13.7) — it downloads a browser, so it's
  not a core dependency. Output PNGs land in `novels/<book>/images/` next to the
  extracted illustrations, **content-hash-named** (§5 idempotency) so a re-run
  doesn't re-rasterize unchanged blocks.

`figure` / `footnote` / `equation` blocks are recorded as visual elements but
**not rasterized** in v1 — figures already point at an extracted image,
footnotes/equations carry their verbatim HTML for the reader to render
(rasterizing those is a v2 thread, §13.9).

**(d) Visual-element record.** Each kept-but-not-narrated block is recorded
alongside the existing `images[]` with the anchoring fields s6 already uses:

```json
{
  "kind": "code | table | figure | footnote | equation",
  "src": "images/code_3a8f1c.png",     // rendered PNG (code/table) or extracted asset (figure)
  "xhtml": "chapter3.xhtml",
  "spine_page": "chapter3",
  "after_paragraph": 41,                // chapter-global paragraph it follows
  "html": "<pre>…</pre>"                // verbatim block markup (reader fallback / footnotes/equations)
}
```

s6 later converts `after_paragraph` → `after_beat_id` / `before_beat_id` /
`trigger_seconds` (§13.5), identical to how it places an inline image.

For **`.txt`** sources there is no structure to detect, so everything is prose
and nothing is dropped or rendered (literal read falls out naturally). For
**`.md`** a best-effort pass treats fenced code blocks and `![]()` figures as
non-prose; full table/footnote/landmark detection is markdown-flavour-dependent
and is a v2 thread (§13.9). **EPUB is the realistic input for this mode** and
gets the full ladder.

Ingest in lecture mode also writes a **Narrator-only `01_characters.json`
stub** (a single `Character` named `Narrator`), so `voice cast` (§13.4) runs
completely unchanged — no s1 needed.

### 13.3 Stage L — the lecture stage (s1+s2+s3 replacement)

`lnvox lecture <book>` ([`src/lnvox/stages/lecture.py`](src/lnvox/stages/lecture.py),
new). Input: `00_text.jsonl` + `04_voice_assignments.json` (for the narrator's
voice descriptor). Output: `03_directed/<chapter_id>.json`
(`ChapterDirected`, the **same schema s4 already consumes** — see
[schemas.py:166](src/lnvox/llm/schemas.py#L166)). Two sub-steps, neither of
which finds characters or scenes:

1. **Deterministic beat split.** Split each chapter into paragraphs with
   `split_paragraphs` ([chunker.py:95](src/lnvox/llm/chunker.py#L95)), then
   sentence-group each paragraph into beats ≤ `MAX_MERGED_BEAT_CHARS` (~500
   chars / ~40 s) by **reusing `_split_long_text`**
   ([s3_director.py:269](src/lnvox/stages/s3_director.py#L269)) — the exact
   length policy Dramabox wants (§2.6). Every beat is `type:"narration"`,
   `speaker:"Narrator"`, and its `source_span` is the **verbatim** contiguous
   source slice (whitespace-collapsed only). Scenes are a thin grouping (one
   `DirectedScene` per chapter, or per heading section) carried only so the
   downstream silence layout (§2.7) and `scene_id`-based beat ids stay valid.

2. **LLM speech-normalize** (the *only* LLM call in lecture mode, §13.6). For
   each beat, `text` = a speech-friendly rendering of `source_span` (numbers →
   words, units/currency/percent verbalized, common abbreviations expanded,
   symbols spoken). **`source_span` is never touched** — it remains the
   verbatim sync anchor. The normalize call is per-paragraph-group (not
   per-beat) for prompt efficiency, idempotent, and cached per chapter like
   every other stage. `--no-normalize` sets `text = source_span` verbatim and
   skips the LLM entirely.

The `prompt` field is built as `<narrator descriptor>, "<text>"` — the same
Dramabox shape s3 produces (§2.5) — where the narrator descriptor comes
straight from the cast's `voice_descriptor` (`descriptor_from_clip` of the
assigned narrator clip, or `DEFAULT_NARRATOR_DESCRIPTOR`). **Crucially it
carries no performance cue** — lecture mode never adds a `whispered`/`weary`-style
direction. Example directed beat:

```json
{
  "type": "narration",
  "speaker": "Narrator",
  "direction": "adult, British, male, clear measured reading voice",
  "text": "By nineteen seventy-three, ARPANET linked forty hosts, see Figure three.",
  "prompt": "adult, British, male, clear measured reading voice, \"By nineteen seventy-three, ARPANET linked forty hosts, see Figure three.\"",
  "source_span": "By 1973, ARPANET linked 40 hosts (see Fig. 3)."
}
```

Note `text` (spoken) ≠ `source_span` (verbatim) exactly where normalization
fired — and s6 anchors on `source_span`, so the synced EPUB still highlights
the original `"By 1973, ARPANET linked 40 hosts (see Fig. 3)."`.

### 13.4 Stage V — voice cast (Narrator only)

Unchanged CLI ([`voice cast`](src/lnvox/cli.py)). With the Narrator-only stub
cast from §13.2, it casts exactly one voice. `--narrator-clip <id>` is the
expected path here (the narrator is 100% of a lecture audiobook — §6.3 applies
even more strongly than in narration mode); auto-cast falls back to
`DEFAULT_NARRATOR_*`. Cross-volume narrator reuse (§6.4) works as-is. Because
casting runs *before* the lecture stage builds prompts, the narrator descriptor
is available to step 1's `prompt` assembly — same "cast before you write the
descriptor prefix" ordering rationale as §2.4.

### 13.5 Stages 4 / 5 / 6 — unchanged code

- **s4 (TTS)** and **s5 (Mix)**: byte-identical code paths. One speaker → the
  content-hash cache (§2.6) and `s4_retry.sh` behave exactly as in narration
  mode. (Throughput is the same per-beat; a lecture book is usually shorter
  audio than a novel.)
- **s6 (Sync)**: the matcher is unchanged and benefits — verbatim `source_span`
  means the `span-exact` bucket (§2.8) should dominate, with the fuzzy ladder
  almost idle. The one **extension**: the non-prose visual elements recorded at
  §13.2 are placed between beats with `after_beat_id` / `before_beat_id` /
  `trigger_seconds`, **reusing the exact image-placement logic** already in
  [s6_sync.py](src/lnvox/stages/s6_sync.py) (the `chapter_images` /
  `after_beat`/`before_beat` walk). They join the manifest as a generalized
  `visual_elements[]` list (illustrations are `kind:"image"`; the new kinds are
  `code` / `table` / `figure` / `footnote` / `equation`). Because code and
  tables were **rendered to PNG at ingest** (§13.2c), those elements carry a
  `src` PNG and are displayed by ln-reader through the *same image-flip code
  that already exists* — zero new reader work; only `footnote`/`equation` carry
  HTML to render. The element appears when playback reaches `trigger_seconds`,
  exactly like an illustration today — precisely the "handle it in the reader"
  behaviour this mode targets.

### 13.6 The normalize pass — contract

A single prompt (`src/lnvox/llm/prompts/lecture_normalize.jinja`, new),
schema-guided like every other LLM stage (§4). Its contract is **respell for
the ear, never rewrite**:

**MUST**: expand numbers/dates/ordinals to spoken words; verbalize units,
currency, percentages, math operators (`%`→"percent", `=`→"equals",
`×`→"times"); expand standard abbreviations (`e.g.`→"for example", `Fig.`→
"Figure", `et al.`→"and others", `cf.`→"compare"); render URLs/emails
speakably or elide to a short spoken form; keep sentence order and every
content word.

**MUST NOT**: paraphrase, summarize, add, drop, reorder, or add any emotional /
performance direction; touch `source_span`. Output the *same words*, only
respelled.

The client already validates + retries (`structured()`), and on failure the
beat falls back to `text = source_span` (verbatim) — so a flaky normalize call
degrades to literal reading, never to a dropped beat. On the Apple-Silicon path
(§11.2) this is the *only* stage exposed to the "no `guided_json`" limitation,
and it degrades the same graceful way.

### 13.7 Code surface (implemented)

| Area | Change |
|------|--------|
| `src/lnvox/ingest/epub.py` | `--mode lecture` branch: run the §13.2a classification ladder; apply the §13.2b drop list; emit `visual_elements` (code/table/figure/footnote/equation) into `.epub_meta.json` alongside `images`. Narration mode unchanged. |
| `src/lnvox/ingest/blocks.py` (new) | Block classifier: deterministic rules (DOM tags + `epub:type`/NCX landmarks + stem regex) → `{prose,code,table,figure,footnote,equation,drop}`, with the LLM fallback for untagged blocks. |
| `src/lnvox/ingest/render.py` (new) | HTML→PNG renderer. Pygments-highlights code / styles tables → minimal HTML → Playwright (headless Chromium) → content-hash-named PNG in `images/`. Imports Playwright lazily so core install works without the `render` extra. |
| `src/lnvox/ingest/text.py` | Lecture-mode helper to write the Narrator-only `01_characters.json` stub. |
| `src/lnvox/stages/lecture.py` (new) | The §13.3 stage: deterministic split (reusing `split_paragraphs` + `_split_long_text`) + normalize pass → `03_directed/*.json`. Idempotent/cached per chapter. |
| `src/lnvox/llm/prompts/lecture_normalize.jinja` (new) | The §13.6 normalize prompt. |
| `src/lnvox/llm/prompts/block_classify.jinja` (new) | The §13.2a LLM-fallback classifier prompt (untagged blocks only). |
| `src/lnvox/llm/schemas.py` | Small `NormalizedBeats` + `BlockClass` schemas for the two new LLM calls. `DirectedBeat`/`ChapterDirected` reused unchanged. |
| `src/lnvox/stages/s6_sync.py` | Generalize `images[]` → `visual_elements[]` (add `kind` + the non-prose kinds); reuse the existing after/before-beat placement walk. Back-compat: images keep `kind:"image"`. |
| `src/lnvox/cli.py` | New `lnvox lecture <book>` command; `--mode` + `--ingest-classifier {fallback,none}` plumbed where ingest needs it. |
| `scripts/run_pipeline.sh` | `--mode lecture`: route ingest → voice cast → `lecture` → s4 → s5 → s6, skipping s1/s2/s3. Shrink (or with `--no-normalize`, drop) the vLLM phase. |
| `pyproject.toml` | New optional `render` extra: `pygments`, `playwright` (+ a `playwright install chromium` setup step). Lazily imported — only needed when rendering code/tables. |
| `config.yaml` | Optional `mode: narration\|lecture`, `lecture: { normalize: true }`, and `ingest: { drop: [toc, copyright-page, titlepage, index, bibliography] }`. |

Nothing about the voicebank subsystem (§6), the Voicebank Studio (§12), the
TTS cache/idempotency (§5), or the s4/s5 stage contracts changes.

### 13.8 Known limitations the design accepts

1. **Normalize is best-effort, not exhaustive.** Domain math (LaTeX-dense
   passages), chemistry, dense tabular prose left inline, etc. will read
   imperfectly. The fallback is always literal `source_span`, so the worst case
   is "TTS does its raw best", never a crash or a dropped beat.
2. **Structure detection is EPUB-grade.** `.txt` carries no structure (all
   prose); `.md` is best-effort. A figure caption that an EPUB inlined as a
   plain `<p>` will be narrated as prose — we don't second-guess the markup.
3. **One narrator, whole book.** No per-chapter or per-POV voice changes
   (lecture mode is for non-fiction; the §8 POV question is narration-mode
   territory).
4. **The normalize pass weakens the strict "verbatim to TTS" reading** the user
   may have first imagined — but it preserves verbatim *sync* (`source_span`)
   and never dramatizes, which is the actual requirement. `--no-normalize`
   restores byte-literal TTS for anyone who wants it.
5. **Rendering needs the `render` extra (Chromium).** Without Playwright
   installed, code/table blocks are recorded as visual elements with their
   verbatim `html` but **no PNG** — the reader falls back to rendering the HTML
   itself. The pipeline never fails on a missing renderer; it degrades to
   HTML-only elements and logs which blocks weren't rasterized.
6. **A code/table that's audio-only is silent.** A listener with no companion
   reader open hears nothing where a rendered block sat (the audio just skips
   to the next prose beat). That's the intended tradeoff of "show it, don't read
   it" — flagged so it's a conscious choice, not a surprise.

### 13.9 What's explicitly **not** in this design

- Reading non-prose blocks aloud (code-to-speech, table linearization). They
  are visual elements only in v1.
- Rasterizing `figure` / `footnote` / `equation` blocks (only `code`/`table`
  are rendered to PNG in v1; the rest carry HTML for the reader).
- Full markdown table/footnote/landmark detection (EPUB gets the full ladder;
  `.md` is best-effort).
- A bespoke lecture player UI — ln-reader's existing image-flip mechanism is
  the integration surface.
- Mixing lecture and narration within one book.

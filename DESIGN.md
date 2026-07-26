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

**Cross-volume continuity.** When the book ID is `<series>/volume-NN`, the
pipeline detects prior volumes in the same series (`artifacts/<series>/volume-*`).
Continuity — a recurring character keeping the same voice — is applied at
**voice-cast** time, which reuses a prior volume's clip assignment by canonical
name. The Stage 1 merge itself is per-volume and deliberately does NOT feed
prior-volume casts into the LLM: on a long series that input grows without
bound (vol-13 would ship ~480K chars of priors), and a differently-named entry
is better fused from the current volume's evidence than coerced to match a
prior list.

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
   per-chapter entries, fusing entries that share an *identity-safe* normalized
   name/alias key. A key is identity-safe only when it denotes one person:
   pronouns are never keys, and a key shared across genuinely different
   canonical names — a generic role/title/relation like "attendant", "the
   merchant", "milady", "high bishop" — is rejected (`_names_all_similar`), so
   it can't chain unrelated characters (across genders and ages) into one
   mega-cluster. Only spelling variants of one name ("Rozemyne"/"Lady
   Rozemyne"/"Myne") cluster together. A fuzzy name pass (`difflib` ratio ≥ 0.9,
   min length 4) catches transliteration / typo variants ("Gunther"/"Gunter");
   enumerated names whose digit runs differ ("Knight 1"/"Knight 2") are never
   fused. Each cluster combines its members' fields by rule (canonical =
   most-frequent form, aliases = union minus pronouns and generic descriptors,
   gender/age = majority, description = longest, evidence = union).
2. **LLM merge-groups** (`merge_clusters`): a single Gemma 4 call receives a
   COMPACT per-cluster summary of the *current* volume only (no prior volumes,
   no descriptions to rewrite) and returns ONLY the GROUPS of clusters that are
   the same person under different names. Stage 1 applies those groups to its
   own clean clusters (`_apply_merge_groups`), so nothing the model writes —
   descriptions or aliases — re-enters the cast. This does the cross-name
   fusion judgment clustering can't, while avoiding a full-cast rewrite's
   failure modes (an un-guided backend reintroducing generic aliases or looping
   to the token cap) and the prior-volume context blow-up. A same-gender safety
   net refuses any proposed group that would fuse a known gender split; a hard
   input-size guard (`_MERGE_INPUT_CHAR_BUDGET`) and any transport/model error
   fall back to the deterministic clusters.

Outputs: `01_characters.json` (the merged cast) and
`01_characters_merge_log.json` — provenance recording what merged into each
cluster, which clusters are "lone" (single chapter), and each one's final
disposition.

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

A merge pass fuses consecutive same-speaker beats (capped at 375 chars per
beat — short beats keep Dramabox's noise/intelligibility in check; see §2.6).

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

**Edge-silence trim (added 2026-07-24).** Dramabox sizes its latent from a
duration estimate; the overshoot renders as silence, mostly leading —
measured: a play carried a median 2.2 s lead (37% of all rendered audio was
edge silence), a novel ~15%. Every WAV is trimmed as it lands in
`05_audio/` (`tts/trim.py`: 10 ms RMS envelope, −40 dBFS, 100 ms keep-pad;
idempotent; upgrades cache entries in place; all-silent files left alone so
failures stay audible). All three render paths hook it — monolithic s4,
staged `_finalize`, VibeVoice save — and manifest durations are measured
post-trim, so s5/s6/§17.4 timing follows automatically. Kill switch:
`lnvox s4 --no-trim` / `LNVOX_S4_NO_TRIM=1`.

**Beat length matters.** Empirically Dramabox renders best on shorter beats —
longer text raises its noise floor and can slur into unintelligible speech. The
Director's merge pass caps fused beats at 375 chars (~30 s) and the prompt
pipeline splits any longer source narration at sentence boundaries before this
stage runs.

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
- **GGUF / llama-server variant** (§14). A third backend, native
  `llama-server` from llama.cpp, runs on either platform and shares the
  OpenAI endpoint contract. Useful when an MLX-quantized build of the
  desired model isn't available, or as the macOS fallback if mlx-lm
  doesn't work on your hardware.

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
operation 5 acts on a book's `04_voice_assignments.json`; operation 6 is a
read-only TTS auditioning surface:

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
6. **TTS Lab** (TTS Lab tab) — audition Dramabox prompts against a chosen
   voicebank clip to find the best way to phrase a direction/description. The
   Dramabox model is loaded **once, manually** via a "Load model" button — never
   on construction or when other tabs are used (it's ~15 GB in VRAM; the lazy
   `lnvox.tts.dramabox_client` import keeps the Studio launchable with no
   torch/Dramabox present). Format buttons compose the same description + line
   into different shapes (`desc, "line"` vs `[desc] "line"` vs `"line"` …) so a
   listener can hear which phrasing leaks the description into the narration;
   `seed` / `cfg_scale` / `stg_scale` are exposed (keep the seed fixed to
   isolate the prompt). Generations accumulate in a replayable history. Renders
   go to a temp dir — the lab writes nothing to the voicebank.

Explicitly **out of scope**: editing existing clips' audio, multi-corpus
import (only the local CV layout is wired), and any pipeline invocation. The
Studio only reads/writes the voicebank and casting JSON (the TTS Lab only
*reads* clips and runs Dramabox); it never re-runs the LLM matcher (a
cross-gender/age cast just prompts a confirmation).

### 12.2 Packaging

Standalone — `scripts/voicebank_studio.py`, run with
`uv run python scripts/voicebank_studio.py` after `uv pip install PySide6`.
Not wired into the `lnvox` CLI and not added as a project dependency; PySide6
is a dev-machine-only concern and the pipeline never imports it. The script
*does* import `lnvox.voices` (schema, manifest I/O, `build_speaker_clip`,
`descriptor_from_clip`) so curation logic stays single-sourced with the seeder.
The TTS Lab (§12.1 op 6) additionally needs the `tts` extra + a GPU, but only
when that tab's "Load model" button is pressed — `lnvox.tts.dramabox_client` is
imported lazily inside the load worker, so the other three tabs run with neither
Dramabox nor torch installed.

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
   sentence-group each paragraph into beats ≤ `MAX_MERGED_BEAT_CHARS` (375
   chars / ~30 s) by **reusing `_split_long_text`**
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

## 14. GGUF / llama-server path (third LLM backend)

`llama-server` (the native binary from llama.cpp) is the third LLM backend,
peer to vLLM (§4) and mlx-lm (§11). It runs on both Linux and macOS, takes
GGUF weights (so the same checkpoint travels between platforms), and is the
designated fallback when mlx-lm doesn't have an MLX-quantized build of the
model you want — or when you want to A/B test specific quantization
recipes against the same prompts.

> **Update 2026-07-25 — llama is now the DEFAULT backend, every platform.**
> After the scenario-mode validation (§17: five plays, FR + EN, zero
> structural failures), the launcher, serve script, and `LLMConfig` all
> default to llama.cpp with `google/gemma-4-12B-it-qat-q4_0-gguf` at a
> 65,536-token context. The launcher exports `LNVOX_LLM_BACKEND` and a
> per-backend `LNVOX_LLM_MODEL` so the client's model name and the
> thinking-channel override (§14.4) always match the server; an unset
> `LNVOX_LLM_BACKEND` now behaves as llama in `LLMClient` too. vLLM and
> mlx stay first-class via `--llm-backend` — §14.6's "manual recovery"
> framing below predates this flip.

### 14.1 Topology

The LLM-phase serve script is selected at launch time:

```
LNVOX_LLM_BACKEND=vllm   → scripts/serve_vllm.sh   (Linux + CUDA;   default on Linux)
LNVOX_LLM_BACKEND=mlx    → scripts/serve_mlx.sh    (Apple Silicon;  default on Darwin)
LNVOX_LLM_BACKEND=llama  → scripts/serve_llama.sh  (either platform; opt-in)
```

All three expose the same OpenAI-compatible `/v1/chat/completions`
endpoint on `:8000`, so `LLMClient` ([client.py:57-60](src/lnvox/llm/client.py#L57-L60))
is unchanged. The pipeline launcher's `--llm-backend` flag (or the env var
above) picks one; everything downstream is identical.

### 14.2 Install

`llama-server` is a native binary, NOT a Python package. Install it
out-of-band — there is no `llama` extra in `pyproject.toml`:

- **macOS:** `brew install llama.cpp` (Metal-enabled).
- **Linux:** either grab a pre-built release from the
  `ggerganov/llama.cpp` GitHub Releases page, or build from source:

  ```sh
  git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp
  cmake -B build -DGGML_CUDA=on
  cmake --build build --target llama-server -j
  ```

  Drop the produced `build/bin/llama-server` somewhere on `PATH`.

`scripts/serve_llama.sh` looks up `llama-server` on `PATH` and fails fast
with a clear install hint if it's missing.

### 14.3 Model selection

`LNVOX_LLM_MODEL` carries either:

- An HF GGUF repo id (auto-downloaded): the project default is
  `google/gemma-4-E4B-it-qat-q4_0-gguf` — Google's official QAT-quantized
  GGUF of the same E4B dev model served by `serve_vllm.sh`. Append
  `:Q4_0` etc. to pick a specific quant inside a multi-file repo.
- A local GGUF file path: `/path/to/gemma-4-31B-it-qat-q4_0.gguf`.

The serve script `-hf`'s the first form and `-m`'s the second.

### 14.4 Limitations vs vLLM

- **No `extra_body["guided_json"]` enforcement** — same loss as mlx-lm
  (§11.2.1). llama.cpp DOES support schema-constrained generation, but via
  `response_format: {"type": "json_schema", ...}` rather than the
  `guided_json` shape `LLMClient` currently sends
  ([client.py:118](src/lnvox/llm/client.py#L118)). The retry loop handles
  parse failures; first-attempt fidelity is below vLLM. Future change:
  thread a `response_format` override through `LLMClient` (benefits
  mlx-lm and llama-server equally).
- **`repetition_penalty` IS honored** (unlike mlx-lm). llama.cpp accepts
  `repeat_penalty` natively and the standard `frequency_penalty` /
  `presence_penalty` knobs. The runaway-loop escape hatch in
  [config.py:16-19](src/lnvox/config.py#L16-L19) keeps working on this
  path.
- **Prefix caching via `--cache-reuse N`** — bounded to N tokens of
  matched prefix per request. Less powerful than vLLM's automatic
  unbounded prefix cache but real. `serve_llama.sh` enables it.
- **Throughput** is install-dependent: a Q4_K_M Gemma 27B on a 4090 lands
  around 25-40 tok/s; the same quant on an M3 Max Metal build lands
  ~10-20 tok/s. Measure on your hardware before committing to it for
  production runs.

### 14.5 Code surface affected

| Area                                       | Change                                                                                                                                                                                                       |
|--------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `scripts/serve_llama.sh` (new)             | Exec native `llama-server` with `LNVOX_LLM_MODEL` (HF repo or local path), `LNVOX_LLM_PORT`, `LNVOX_LLM_HOST`, `LNVOX_LLM_N_GPU_LAYERS` (999 = full offload), `LNVOX_LLM_MAX_LEN`. Hard-exits if the binary isn't on `PATH`. |
| `scripts/run_pipeline.sh`                  | New `LNVOX_LLM_BACKEND` env var + `--llm-backend {vllm,mlx,llama}` flag. Defaults: Darwin→`mlx`, Linux→`vllm`. `prepare_llm_env` and `start_vllm` dispatch on it.                                              |
| `pyproject.toml`                           | **No change** — llama-server is a native binary, not a Python package.                                                                                                                                       |
| `src/lnvox/{llm,tts,stages,cli}/*`         | **No change** — `LLMClient` already speaks the OpenAI dialect llama-server provides.                                                                                                                          |

### 14.6 What's explicitly **not** in this section

- **Auto-fallback.** Setting `LNVOX_LLM_BACKEND=llama` is a manual
  recovery step, not an automatic one. If mlx-lm fails to start, the
  pipeline aborts; the user re-runs with `--llm-backend llama`. Auto-
  failover would tangle the launcher's lifecycle code and is out of scope.
- **Threading `response_format` schemas through `LLMClient`.** Both mlx-lm
  and llama-server would benefit, but it's an API design pass and
  belongs in a separate v2 change. Until then, both backends rely on the
  client's existing validate-and-retry loop.
- **Bundling llama.cpp.** Vendoring the binary or pinning a build version
  is intentionally avoided — installs are heterogeneous (Metal vs.
  cuBLAS vs. ROCm) and out-of-band wins.

## 15. Staged s4 (batched TTS phases, crash-isolated)

Split the monolithic per-beat Dramabox render into four GPU phases with
disk checkpoints between them. Each phase loads exactly ONE model, sweeps
all pending work items in batches, and writes small tensors to disk.
A crash loses at most one item and restarts pay a single-model reload,
not the full four-model `TTSServer` boot.

### 15.1 Why

Consumer GPUs without ECC (the RTX 5090 dev box) crash sporadically
mid-render: transient CUDA errors, driver resets, OOM under VRAM
pressure. Today's mitigation is brute force — `s4_retry.sh` /
`MAX_RETRIES` restart the whole stage and rely on the per-beat content
cache to skip finished work. Every restart re-pays the full
`TTSServer._load_all()`: Gemma (~11 s) + DiT (~8 s) + VAE encoder +
decoder + `torch.compile` warm-up, all before the first pending beat
resumes. On a run with dozens of crashes that overhead dominates.

The monolithic path also carries structural costs
([inference_server.py](external/DramaBox/src/inference_server.py)):

- **Four models resident in VRAM simultaneously** (Gemma-3-12B bnb-4bit
  prompt encoder ≈ 8 GB, the LTX audio-only DiT, VAE encoder, VAE
  decoder + BigVGAN BWE). During the 30-step CFG+STG denoise the guider
  triples the effective batch, so peak pressure lands on top of ~all
  weights. Thin headroom is itself a crash driver.
- **Per-beat waste:** `generate()` re-encodes the constant negative
  prompt through Gemma on EVERY beat (`[prompt, DEFAULT_NEG]`), and
  re-VAE-encodes the voice reference on every beat (only the RE-USE
  denoise is cached, keyed by path).
- **Long beats have no internal checkpoints:** `generate_long()` renders
  N chunks inside one call and crossfades in memory — a crash on chunk
  3/4 loses all four.

### 15.2 Dramabox inference anatomy

`TTSServer.generate()` is already a linear composition of separable
steps. What each one needs and produces:

| # | step               | model needed                    | output (per item)                | fp16 size    |
|---|--------------------|---------------------------------|----------------------------------|--------------|
| 0 | plan               | none (CPU)                      | work item: prompt chunk, frames, seed, cache key | bytes |
| 1 | ref conditioning   | RE-USE + VAE **encoder**        | ref latent, per unique clip      | ~1 MB        |
| 2 | prompt encode      | Gemma-3-12B (bnb-4bit)          | `a_ctx` audio encoding           | ~1–2 MB      |
| 3 | denoise            | LTX audio DiT (the big one)     | denoised latent `(1,128,F,1)`    | ~130 KB/20 s |
| 4 | decode + finish    | VAE **decoder** + BigVGAN BWE   | final WAV → `05_audio/` + cache  | audio        |

Steps 0–2 are cheap sweeps. Step 3 dominates wall-clock and stays
sequential in v1. Step 4 is fast. The negative-prompt context is
encoded ONCE for the whole book instead of once per beat, and refs are
encoded once per unique clip instead of once per beat.

### 15.3 Phase layout

`lnvox s4 --staged` runs a driver that executes phases in order, each as
a **subprocess** (fresh CUDA context — a poisoned context from a prior
fault can't leak forward):

```
P0 plan     artifacts/<book>/05_audio/_staged/plan.json
P1 refs     _staged/refs/<clip_hash>.safetensors        (per unique voice clip)
P2 ctx      _staged/ctx/<item_id>.safetensors + neg.safetensors
P3 denoise  _staged/latents/<item_id>.safetensors
P4 decode   05_audio/<chapter>/<beat_id>.wav + cache/tts/<key>.wav + manifest.json
```

- **Work item = beat chunk.** P0 replicates the monolithic sizing
  exactly: `estimate_duration(prompt, multiplier)` per beat, and beats
  whose estimate exceeds `max_chunk_duration` (45 s) are split with
  `text_chunker.chunk_prompt_for_duration` up front. Beats whose final
  WAV is already in the content cache are marked done in the plan — an
  interrupted monolithic run resumes seamlessly under staged and vice
  versa (same `cache_key` recipe, same `MODEL_VERSION`).
- **P1** dedupes by voice clip: RE-USE denoise → mono/peak-norm/tile to
  10 s → VAE encode, keyed by clip content hash + ref duration. A book
  has tens of clips, not thousands of beats.
- **P2** sweeps all prompts through Gemma one item at a time
  (`PromptEncoder` takes a list but encodes sequentially inside — there
  is no true batched forward to exploit), checkpointing each context
  tensor as it lands. The savings are the negative prompt encoded once
  per book instead of once per beat, and Gemma running with the whole
  GPU to itself.
- **P3** loads ref latent + ctx from disk, rebuilds the latent state
  (conditioning applied BEFORE noise, same `manual_seed(seed)` order as
  `generate()` — outputs are numerically identical modulo kernel
  nondeterminism), runs the 30-step euler loop with the same
  guider/rescale config, applies the frame-513 silence-prior fix, saves
  the latent. Items are sorted by `n_frames` so `torch.compile(dynamic=True)`
  sees few distinct shapes.
- **P4** VAE-decodes each latent, equal-power-crossfades chunk groups
  back into per-beat WAVs (same `_equal_power_crossfade`, 50 ms), writes
  the WAV + content cache entry, deletes the item's intermediates, and
  writes the per-chapter `manifest.json` (schema unchanged — s5/s6
  don't know staged exists).

All writes are `tmp + os.replace` atomic; "done" = output file exists.

### 15.4 Crash isolation & resume

- The driver restarts a failed phase subprocess; completed items skip by
  file existence, so a crash costs one item's compute plus one
  single-model load (P3 restart ≈ DiT load only, vs the full four-model
  boot today).
- **No item is ever skipped.** An audiobook with a silently missing
  beat is worse than a stalled run, so every item retries until it
  renders — crashes on non-ECC hardware are transient, and the same
  item almost always succeeds on the next attempt. `_staged/attempts.json`
  counts per-item failures purely as diagnostics (a beat that crashed
  5× is worth looking at even after it eventually rendered). The one
  guard: if `LNVOX_S4_STALL_LIMIT` (default 10) consecutive phase
  restarts complete **zero** new items, the driver aborts loudly naming
  the stuck item — a deterministic crasher should fail the stage for a
  human to look at, not silently grind forever or get skipped.
- `run_pipeline.sh`'s outer retry loop and `s4_retry.sh` become
  redundant for staged runs (the driver owns retries); they stay for the
  monolithic path.

### 15.5 Storage

Intermediates for a ~5 000-item novel peak at roughly: refs ~50 MB, ctx
~5–10 GB (the dominant term — fp16, deleted per item as P4 lands its
WAV), latents ~1 GB. `--keep-staged` retains everything for debugging;
default is delete-on-finish, so steady-state overhead is bounded by the
not-yet-decoded window.

### 15.6 Code surface affected

| Area                              | Change                                                                                                                                          |
|-----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `src/lnvox/tts/staged.py` (new)   | Phase implementations. Imports Dramabox building blocks (`PromptEncoder`, `AudioConditioner`, `AudioDecoder`, DiT builder, `auto_rescale_for_cfg`, `_equal_power_crossfade`, `text_chunker`) exactly the way `inference_server.py` composes them — **Dramabox source stays unpatched**. |
| `src/lnvox/tts/staged_driver.py` (new) | Plan model, subprocess orchestration, attempts/poisoned bookkeeping.                                                                        |
| `src/lnvox/cli.py`                | `lnvox s4 --staged` flag + hidden `lnvox s4-phase {plan,refs,ctx,denoise,decode}` per-phase entry points (what the driver spawns).               |
| `scripts/run_pipeline.sh`         | `--staged-tts` (or `LNVOX_S4_STAGED=1`) routes s4 through the driver and skips the outer retry loop for it.                                       |
| `src/lnvox/stages/s4_tts.py`      | Untouched — monolithic path remains the default until staged is validated on a full book.                                                        |

### 15.7 Expected wins (honest)

- **Crash restart cost:** full 4-model boot + compile warm-up (≈ 40–60 s)
  → one model (P3 ≈ 10–15 s). This is the headline win on non-ECC
  hardware; with dozens of crashes per book it's hours.
- **Fewer crashes to begin with:** P3 runs with only the DiT resident —
  the VRAM freed from Gemma + both VAE halves becomes headroom under
  the 3× guider batch.
- **Throughput on a crash-free run:** modest (~10–20 %) — the denoise
  loop dominates and is unchanged. The savings are the eliminated
  per-beat negative-prompt encode, per-beat ref VAE encode, batched
  Gemma calls, and fewer compile shape misses from frame-sorted P3.
- **Long beats** checkpoint per chunk instead of per beat.

Before building: capture one crash-free chapter with the existing per-step
log lines (`Prompt:`, `Denoise (30 steps):`, `Decode (LTX BWE):`) to
validate these ratios on the 5090.

### 15.8 What's explicitly **not** in this section

- **True batched denoise (batch > 1 in P3).** The LTX euler loop +
  `BatchSplitAdapter` suggest it's reachable, but padding/attention-mask
  semantics across mixed-length latents are unverified. v2 experiment
  behind an env flag; v1 keeps per-item denoise.
- **Parallel phases / multi-GPU.** The phase boundary would support a
  producer-consumer overlap (P2 on one card, P3 on another); out of
  scope for one-GPU boxes.
- **MPS.** Staged runs fine as subprocesses on Apple Silicon and
  inherits the §11.3 knobs via the same code path, but is not
  smoke-tested there; monolithic stays the Darwin default.
- **Changing voice or sampling params.** cfg/stg/seed/duration logic is
  byte-for-byte the monolithic recipe — this section is plumbing, not
  quality tuning.

## 16. VibeVoice TTS backend (session-mode second TTS engine)

Add `microsoft/VibeVoice-Large` as an alternative Stage-4 engine, selected
the same way the third LLM backend was (§14): an env var + launcher flag
and a parallel client class. Unlike Dramabox's beat-at-a-time rendering,
this backend uses VibeVoice's headline feature — **long-form multi-speaker
generation** — rendering whole scene *sessions* (up to 4 voices per call)
so dialogue turn-taking and prosody flow continuously. Output goes to a
new `05_audio_v2/` tree so both engines' renders coexist per book for A/B
listening. Dramabox and `05_audio/` stay the default and are untouched.

### 16.1 What & why

VibeVoice-Large is Microsoft's long-form conversational TTS: a Qwen2.5-7B
LLM backbone over 7.5 Hz continuous acoustic/semantic tokenizers with a
lightweight diffusion head (~10 steps). MIT-licensed. One generation
session supports up to 4 distinct speakers, ~45 min of audio, 32 K context.
Voice identity comes **entirely from a reference WAV** (zero-shot cloning) —
there is no text style-descriptor channel like Dramabox's screenplay prompt.

Why a second engine:

- **Multi-speaker sessions.** A scene rendered in one call gets natural
  turn-to-turn pacing and consistent prosody — the thing per-beat
  concatenation can never give.
- **Voice cloning is first-class.** The voicebank clip assigned by
  `voice cast` becomes the voice, directly — and later, *emotion variants*
  of that clip become the emotion channel (§16.7).
- **Single model resident** (~18 GB bf16) vs Dramabox's four-model
  `TTSServer` — simpler VRAM story, no §15 staging needed, and different
  failure modes to de-risk whole-book renders.

Sourcing caveat: Microsoft pulled the original inference code; the
maintained code is the community fork
([vibevoice-community/VibeVoice](https://github.com/vibevoice-community/VibeVoice),
MIT). Weights are on ModelScope (`microsoft/VibeVoice-Large`) and mirrored
on HF (`microsoft/VibeVoice-Large`, `aoi-ot/VibeVoice-Large`).

### 16.2 Backend selection & artifact layout

- `LNVOX_TTS_BACKEND` ∈ {`dramabox` (default), `vibevoice`} + a
  `--tts-backend` option on `lnvox s4` (flag wins over env).
- **`05_audio_v2/`**: the vibevoice backend writes
  `05_audio_v2/<chapter>/<session_id>.wav` + `manifest.json`, never
  touching `05_audio/`. The manifest reuses the existing
  `ChapterAudio`/`RenderedBeat` schema with one entry per *session*
  (`beat_id` = session id), so Stage 5 consumes it unchanged.
- `lnvox s5 --v2` mixes from `05_audio_v2/` instead of `05_audio/`.
  Everything else in s5 (silence pads, loudnorm, m4b mux) is identical:
  consecutive sessions of the same scene get the intra-scene pad,
  scene changes the inter-scene pad — pacing *inside* a session is
  VibeVoice's own turn-taking.
- `run_pipeline.sh --tts-backend vibevoice` dispatches
  `prepare_vibevoice_env` (instead of `install_dramabox_reqs`), runs s4
  with the flag under the same `s4_retry.sh` loop, and passes `--v2` to s5.
- `--staged` + vibevoice is a **hard error** (launcher and CLI): §15
  exists because Dramabox holds four models and crashes mid-run;
  VibeVoice is one checkpoint with a ~30–60 s boot, so monolithic +
  retry (restart cost = that boot) is the whole crash story.
- **s6 sync does not run for v2 audio** in this iteration: a session WAV
  has no per-beat timestamps, so beat-level highlighting has nothing to
  anchor to. Recovering beat times via forced alignment is future work
  (§16.10).

### 16.3 Session planning

The unit of generation is a **session**: a consecutive run of beats
inside one scene. The planner walks each scene's beats in order and
greedily accumulates, closing the session when the next beat would

- introduce a **5th distinct speaker** (model limit is 4), or
- push the session past **`--max-session-chars`** (default 3000 ≈ 3–4 min
  of speech — the cache-granularity vs. prosody-continuity tradeoff; one
  edited line re-renders its session, not the chapter).

Sessions never span scenes (scene boundaries are where s5 inserts real
silence). A single beat longer than the cap gets its own session — beats
are never split (the Director already caps them at 375 chars anyway).

Within a session, characters are numbered `Speaker 1..N` **in order of
first appearance**, and `voice_samples` is passed in that same order —
exactly the fork's expected mapping. The script is one line per beat:

```
Speaker 1: It was the last day of summer vacation, and Kamijou Touma had…
Speaker 2: You've got to be kidding me.
Speaker 1: The girl on his balcony did not look like she was kidding.
```

(`DirectedBeat.text` verbatim, whitespace collapsed to single spaces —
the screenplay `prompt`/`direction` strings are Dramabox-only; VibeVoice
would read the direction prefix aloud.)

**Refs are mandatory** (no descriptor fallback exists): speaker's
assigned clip → Narrator's assigned clip → error telling the user to
re-run `lnvox voice cast`. Deterministic — the chosen filenames land in
the cache key.

**Cache**: key = `hash(script, ordered ref filenames, MODEL_VERSION)`,
same `cache/tts/<key>.wav` pool as Dramabox (keys can't collide across
engines — `MODEL_VERSION` differs). Manifest entries carry
`scene_id`, `speaker` = the joined speaker list, `type` = `dialogue` if
any beat is dialogue else `narration`.

### 16.4 Client & generation

New `src/lnvox/tts/vibevoice_client.py`. Unlike Dramabox it's a real
package: env prep does `uv pip install -e external/VibeVoice`, no
`sys.path` games.

```python
class VibeVoiceClient:
    MODEL_VERSION = "vibevoice-large-cfg1.3-ddpm10-48k"
    DEFAULT_PARAMS = {"cfg_scale": 1.3, "ddpm_steps": 10, "seed": 42}

    def __init__(self, device="cuda", model_path=None): ...
    def generate_session(self, *, script, voice_refs, output_path,
                         seed=None, cfg_scale=None): ...
```

- Loading: `VibeVoiceProcessor.from_pretrained(model_path)` +
  `VibeVoiceForConditionalGenerationInference.from_pretrained(...)` with
  the §16.5 dtype/attention knobs, then `set_ddpm_inference_steps(10)`.
  `model_path` resolution: explicit arg → `LNVOX_VIBEVOICE_MODEL` →
  `models/VibeVoice-Large/` if present → HF id `microsoft/VibeVoice-Large`.
- Generation: `processor(text=[script], voice_samples=[refs], …)` →
  `model.generate(cfg_scale=…, tokenizer=processor.tokenizer,
  generation_config={"do_sample": False})`, with `torch.manual_seed(seed)`
  per session so re-rolls are reproducible (and a different `seed` is the
  re-roll lever).
- **Sample-rate normalization**: VibeVoice emits 24 kHz mono; the whole
  downstream chain assumes 48 kHz stereo (s5 silence pads + concat
  demuxer need homogeneous inputs). The client resamples to 48 kHz and
  duplicates to stereo at save time via `soxr` (added to the `tts`
  extra); the cache stores the normalized WAV. Upsampling adds no
  quality, only uniformity — making s5 rate-aware was rejected because a
  book mixing engines must still concat cleanly.

### 16.5 Per-device knobs

Same philosophy as §11.3 — per-device defaults, explicit args win:

| Device | dtype | attention | Notes |
|---|---|---|---|
| CUDA x86_64 (5090) | bf16 | `flash_attention_2` if importable, else `sdpa` | ~18 GB weights; fits 24 GB+ cards. |
| CUDA aarch64 (Spark) | bf16 | `sdpa` (no flash-attn aarch64 wheel; source build optional) | torch 2.10 cu130 path already in place. |
| MPS | fp32 | `sdpa` | Fork's MPS branch is fp32-only (fp16 artifacts). ~36 GB+ unified needed — experimental. |
| CPU | fp32 | `sdpa` | Works; glacial. Smoke tests only. |

`_default_tts_device()` ([cli.py:474](src/lnvox/cli.py#L474)) is reused
as-is. Long sessions raise per-call VRAM vs. Dramabox's short beats;
if OOM shows up on 24 GB cards, lowering `--max-session-chars` is the
release valve (more, shorter sessions).

### 16.6 Install & weights

`scripts/setup_vibevoice.sh` mirrors `setup_dramabox.sh`:

1. Clone the community fork to `external/VibeVoice/`, pinned to a
   known-good commit (recorded in the script; `main` as of 2026-06-12 is
   `07cb79fe`). Override with `LNVOX_VIBEVOICE_REF`.
2. `uv pip install -e external/VibeVoice` into the tts-phase venv. torch
   is whatever the platform installer already put there (2.8 x86_64 /
   2.10 aarch64 / ≥2.10 Darwin — all sufficient). **transformers is a
   real conflict, not a coexistence** (found the hard way, 2026-07-23):
   the fork pins `transformers==4.51.3`, while Dramabox's Gemma encoder
   calls the `.model` submodule that only exists in transformers ≥4.52
   ([base_encoder.py:44](external/DramaBox/ltx2/ltx_core/text_encoders/gemma/encoders/base_encoder.py#L44))
   — and Dramabox's own `>=4.45` spec never pulls it back up. One venv
   therefore serves ONE engine at a time: `prepare_vibevoice_env`
   installs the pin, `prepare_tts_env` explicitly restores
   `transformers>=4.52`, and the Studio's TTS Lab warns on Load which
   engine the current venv supports (one engine per Studio session).
3. Weights (~18 GB): default `uvx modelscope download
   microsoft/VibeVoice-Large` into `models/VibeVoice-Large/` (no
   permanent modelscope dep); `--hf` flag uses the HF mirror via
   `hf download` instead. `LNVOX_VIBEVOICE_MODEL` overrides with any
   local path or HF repo id.

### 16.7 Emotion-expanded voicebank (designed, deferred)

The gamble in v1 is that cloning + text punctuation carries enough
affect. If sessions come out **monotonous**, the fix is not a new model —
it's better refs, manufactured with the engine we already have:

- New command `lnvox voice emote <book>`: for each cast (character,
  clip) pair, render **7 emotion variants** of the clip with *Dramabox*,
  prompting `<snapped descriptor>, <emotion>, "<calibration text>"` for
  emotion ∈ {calm, joy, sadness, anger, fear, surprise, disgust}.
  Output: `voicebank/emotions/<clip_id>/<emotion>.wav` + a manifest
  extension. Dramabox's descriptor channel *acts* the emotion;
  VibeVoice's cloning then *transfers* it — each engine doing the one
  thing the other can't.
- Calibration text: the clip's `sample_sentences` when present, else a
  fixed neutral paragraph. `license: personal_use_only` flags propagate
  to the variants (§6 rule).
- Stage 3 gains a per-beat `emotion` field (same 7-value enum, default
  `calm`) emitted by the Director alongside the existing `direction`.
- The session planner then picks each speaker's ref by their **dominant
  emotion within the session**; strong emotion swings become an
  additional session-split trigger. Emotion variant filenames land in
  the cache key for free (refs are already hashed by filename).

Not implemented until v2 renders are judged by ear — deliberately.

**Listening verdict (2026-07-23, first real render):** VibeVoice wins on
pronunciation accuracy, generation speed, and hallucination rate; it
loses on pacing — turns land like two or more people talking *at* each
other rather than a scene, and the judgment is that emotion refs alone
won't fully fix that. Dramabox stays the preferred default; VibeVoice
ships as the alternative for ears that weigh accuracy over acting (the
Studio's TTS Lab can audition both). This section stays deferred.

### 16.8 Code surface affected

| Area | Change |
|---|---|
| `src/lnvox/tts/vibevoice_client.py` (new) | Model wrapper per §16.4. |
| `src/lnvox/stages/s4_vibevoice.py` (new) | Session planner + renderer → `05_audio_v2/` (§16.3); reuses `ChapterAudio` schema and s4_tts's hash/duration/clip-map helpers. |
| `src/lnvox/stages/s4_tts.py` | Move-only: extract the `--limit` slicing into a shared helper. No behavior change. |
| `src/lnvox/cli.py` | s4: `--tts-backend` (+env default), `--max-session-chars`, staged guard, vibevoice dispatch. s5: `--v2`. |
| `scripts/setup_vibevoice.sh` (new) | Clone pinned fork + editable install + weight download (§16.6). |
| `scripts/run_pipeline.sh` | `--tts-backend` flag, `prepare_vibevoice_env`, s5 `--v2` pass-through, staged+vibevoice guard. |
| `pyproject.toml` | `soxr>=0.5` in the `tts` extra. No torch changes. |
| `tests/test_sessions.py` (new) | Planner invariants + renderer/cache with a fake client (no model needed). |
| `scripts/voicebank_studio.py` | TTS Lab: engine selector (dramabox/vibevoice), one-engine-per-session transformers warning on Load (§16.6), `Speaker N:` script auditions with the selected clip as every speaker's ref. |
| `src/lnvox/tts/staged*.py`, `s5_mix.py`, s6, voices | **No change.** |

### 16.9 Quality risks (accepted for v1)

- **Language coverage.** Trained on English + Chinese. Japanese names
  (the toaru corpus) may mispronounce; no text normalization is performed
  by the model, so numerals/abbreviations render as-is.
- **Ref-clip quality dominates.** Common Voice clips are noisy and
  short; cloning inherits the noise. Curating 5–15 s clean refs (Studio,
  §12) is the lever — and §16.7 raises the ceiling further.
- **Monotony.** No descriptor channel means `direction` cues are unused;
  affect rides on punctuation + cloning. This is exactly what §16.7
  exists for — listen first, then decide.
- **Session-level cache granularity.** One edited line re-renders a
  ~3000-char session, not a 375-char beat. Bounded by
  `--max-session-chars`.
- **Occasional hallucinated fillers / speaker drift** on long sessions (a
  known VibeVoice quirk, worse above ~4 speakers or very long scripts).
  Re-roll the session with a different seed; cap keeps sessions modest.

### 16.10 What's explicitly **not** in this section

- **Per-beat VibeVoice mode.** Rendering beat-by-beat forfeits the
  model's long-form strength while keeping all its weaknesses — Dramabox
  already owns that regime.
- **s6 sync for v2 audio.** Needs forced alignment (e.g. WhisperX or
  aeneas) to recover per-beat timestamps inside session WAVs — a design
  of its own, do it when a v2 book needs the reader.
- **Implementing §16.7 now.** Ears first.
- **VibeVoice-1.5B / Realtime-0.5B variants.** The client accepts any
  compatible checkpoint via `LNVOX_VIBEVOICE_MODEL`, but only Large is
  tested/documented.
- **Auto-fallback between TTS backends** (same rationale as §14.6) and
  **LoRA hooks** (`load_lora_assets` exists in the fork; ignored until
  needed).

## 17. Scenario mode (theater scripts → timed sync file + full-cast audio)

A third pipeline mode next to narration and lecture (§13): the input is a
**theater script** (a troupe's working document, not a book), and the
primary deliverable is not an audiobook but a **sync file** — one timed
entry per spoken line, `{timing, speaker, text, direction}` — plus the
full-cast TTS audio that timeline is measured against. Same skeleton,
same voicebank machinery, same engines; new ingest, a verbatim-preserving
structure pass, and a sync emitter.

**IP constraint (hard rule).** The test scripts under `scenarios/` are
protected IP. No verbatim script content — lines, staging, cues,
character names, titles — may appear in this document, in code, in
prompt templates, or in test fixtures; all examples below are invented.
`scenarios/` and `data/` are gitignored. Script text is sent only to the
locally-served LLM and the local TTS engines; it never leaves the machine.

### 17.1 What & why

The test corpus is `scenarios/*.md` — four real French scripts of very
different kinds (short comedy sketches; an ensemble piece; a *conduite*
/ tech run sheet; a full-length play). Use cases the sync file serves:

- **Line learning / rehearsal companion**: hear the whole play with cast
  voices; follow along per line; know who speaks when.
- **Régie cueing**: two of the four scripts embed sound/light cues.
  Cue entries with timestamps give the régisseur a dry-run conduite
  without actors.
- **French**: validated — the user has already confirmed both the LLM
  stages and the TTS engines handle French acceptably (prompts stay
  English; outputs follow the script's language). A dedicated **French
  voicebank** is seeded from the French Common Voice corpus now in
  `data/` (§17.5).

### 17.2 Input reality — why ingest is LLM-structured, not a regex

The four scripts share concepts but not syntax (all syntax shown with
invented placeholder names):

| Script | Dialogue syntax | Structure | Extras |
|---|---|---|---|
| A | `**Nom** \- ligne` (+ variants/typos: trailing dash glued to the name, `:` instead of `-`, inconsistent spacing in `**Rôle 1 (Prénom)**` labels) | `Séquence N – Titre` | italic staging lines AND italic staging *inside* dialogue lines |
| B | `Nom : ligne` | `### N. TITRE` | a characters section mapping **several roles → one actor**; bold one-word tech cues; group lines spoken by everyone |
| C | bold CAPS name alone on a line, text block follows | bold number + bold quoted title | it's a *conduite*: color-coded bold music/light cues interleaved throughout |
| D | `NOM : ligne` | period/act headers + numbered scenes | characters section with prose bios + role→actor mapping; voice-over speaker; sound/light cues |

Hand-writing a parser per troupe-formatting-quirk is a losing game. The
design mirrors lecture mode's deterministic-first philosophy (§13.2) but
inverts the ratio: scene/sequence headers are detected deterministically
where possible (they chunk the LLM's work), and a **structuring LLM pass**
classifies each chunk's lines into items. The non-negotiable invariant is
**verbatim text**: every dialogue item's `text` must be an exact substring
of its source chunk (after markdown unescaping) — validated in code, with
per-chunk retry, then a loud per-line fallback (kept as `staging` so
nothing silently disappears). The LLM structures; it never rewrites.

### 17.3 Pipeline & artifact layout

`lnvox ingest-scenario scenarios/<file>.md --id <id>` (needs the LLM
server — structuring is an LLM pass), then the existing `voice cast`,
then `lnvox scenario <id>` (the direction pass; needs casting for the
descriptors, mirroring the s2→cast→s3 order of narration mode), then
s4 / s5, then `lnvox scenario-sync <id>`.

| Step | Reuse | Output under `artifacts/<id>/` |
|---|---|---|
| Ingest + structure | new (`ingest/scenario.py` + `scenario_structure.jinja`) | `00_script.json` — scenes of ordered items `{type: dialogue\|staging\|cue, speaker?, text}` + the characters roster (name, bio, actor) when the script has one; also `00_text.jsonl` (one chapter per scene) so s5 picks up scene titles for chapter markers |
| Characters | s1 schemas reused | `01_characters.json` — roster is script-given; the same pass merges speaker-label variants (spacing/typos/parenthesized first names) and LLM-fills gender/age/description gaps for casting |
| Casting | `voice cast` **unchanged** | `04_voice_assignments.json`. Role-collapsing onto one actor's voice is done by hand in the Voicebank Studio's Casting tab — no `--by-actor` flag (decision 2026-07-24) |
| Direction | s3 machinery reused; new prompt | `03_directed/*.json` — `DirectedBeat` per line, `text` verbatim, + new `emotion` field (the §16.7 7-enum), `direction` = short cue **in the script's language** (for humans/sync), Dramabox `prompt` composed in English as today |
| TTS | s4 **unchanged** (both engines, `--tts-backend`) | `05_audio/<scene>/…` |
| Mix | s5 **unchanged** | `06_final/<id>.m4b` with scene chapter markers |
| Sync | new emitter | `07_sync/<scene>.json` + `07_sync/play.json` (+ `.srt` export) |

**Ingest is content-cached** (same philosophy as the s4 beat cache,
§2.6): every structuring/roster/characters LLM call is cached under
`cache/scenario/` keyed on the hash of the *rendered prompt* + schema +
model — so a re-run or crash-resume only pays for scenes whose text
actually changed, and editing a prompt template or switching models
re-keys automatically (stale entries can't survive by construction).
`--no-cache` bypasses it to re-roll the structuring.

Mapping onto existing stage shapes: one `ChapterDirected` per script
scene/sequence; within it, one `DirectedScene` per staging-delimited
run of dialogue — so s5's existing inter-scene pad lands exactly where
a staged action happens, and becomes the "staging pause" with zero new
s5 code. One beat per spoken line; the s3 375-char split still applies
for TTS *rendering* (long monologues), with the beat's
`source_paragraph` field (already on the schema for lecture mode)
carrying the script-line index so the sync emitter re-groups split
chunks into **one entry per script line**.

### 17.4 The sync file

Deterministic timing — the same cursor math s5/s6 already use (beat WAV
durations + the same pad values passed to both stages), not audio
probing. Example (invented content):

```json
{
  "scene_id": "01_sequence-1",
  "entries": [
    {"start": 12.34, "end": 15.20, "type": "dialogue",
     "speaker": "Gardien", "text": "Qui va là ?",
     "direction": "méfiant, voix basse", "emotion": "fear"},
    {"start": 16.20, "end": 18.20, "type": "staging",
     "text": "Il lève sa lanterne vers la porte."},
    {"start": 18.20, "end": 18.20, "type": "cue", "text": "SON 3"}
  ]
}
```

- **dialogue**: timed span of the line's rendered audio.
- **staging** (didascalies): a configurable pause on the timeline
  (default = s5's inter-scene pad, 1.0 s; the action takes stage time)
  and an entry in the sync file; never spoken.
- **cue** (sound/light/other): zero-duration marker at its position —
  the régie timeline for free.
- The m4b and the sync file are computed from the same plan and pad
  values, so they agree by construction (same stance as s6, §2.7).

### 17.5 French voicebank

The French Common Voice corpus now lives under `data/` alongside the
English one. Seeding stays the existing `voice seed-cv` flow pointed at
the French locale dir; the bank lands in a **separate voicebank
directory** so English and French casts don't mix. Plumbing:
`_voicebank_dir()` honors a new `LNVOX_VOICEBANK` env var (default
`voicebank`), read by `voice *`, `s4`, and the Studio (which already
takes `--voicebank`). A French project sets `LNVOX_VOICEBANK=voicebank-fr`
once; no per-command flags.

### 17.6 Decisions (confirmed 2026-07-24)

- **Group lines** (everyone speaks): rendered with the Narrator-fallback
  voice, group label kept in the sync `speaker` field. Choral audio is
  out of scope.
- **Voice-over / offstage speakers**: normal characters — cast them.
- **Direction language**: script language (French) in `direction` for
  humans; English descriptor composition stays internal to the TTS
  prompt. The `emotion` enum is language-neutral and shared with §16.7 —
  scenario mode is where that field enters the schema.
- **Actor mapping**: parsed and stored in the roster when the script has
  one (it informs manual casting in the Studio); no automatic collapsing.
- **Numbers/text normalization**: scripts are spoken-register already;
  prices/numerals are the known TTS hazard (§16.9) — accepted for v1.
- **SRT/VTT export**: included (subtitle-shaped tooling is everywhere;
  it's ~30 lines).

### 17.7 Code surface (planned)

| Area | Change |
|---|---|
| `src/lnvox/ingest/scenario.py` (new) | Markdown chunking, LLM structure pass, verbatim validation → `00_script.json` + `00_text.jsonl`. |
| `src/lnvox/llm/prompts/scenario_structure.jinja` (new) | Classify lines: dialogue/staging/cue + speaker labels, verbatim text. Invented examples only (IP rule). |
| `src/lnvox/llm/prompts/scenario_directions.jinja` (new) | Per-line emotion (7-enum) + short direction cue in the script's language. |
| `src/lnvox/stages/scenario.py` (new) | Direction pass → `03_voice_profiles.json` + `03_directed/*.json` (reuses s3's descriptor snap, split, prompt format). |
| `src/lnvox/stages/scenario_sync.py` (new) | Timing plan from manifests + script items → `07_sync/*.json` + SRT. |
| `src/lnvox/llm/schemas.py` | `DirectedBeat.emotion: str = "calm"`, scenario script schemas (roster, items). |
| `src/lnvox/cli.py` | `ingest-scenario`, `scenario`, `scenario-sync`; `_voicebank_dir()` honors `LNVOX_VOICEBANK`. |
| `scripts/run_pipeline.sh` | `--mode scenario` (LLM phase: ingest-scenario + voice cast + scenario; then s4/s5 + scenario-sync; skips s6). |
| `tests/test_scenario.py` (new) | Chunker, verbatim validation, sync timing math — on an invented French fixture script (IP rule). |
| s4, s5, voices matcher, TTS clients | **No change.** |

### 17.8 What's explicitly **not** in this section

- **Choral/unison rendering** for group lines.
- **Music/lighting playback** — sync carries cue timestamps; firing them
  is the régie tool's job, not ours.
- **Automatic role→actor voice collapsing** — the Studio's Casting tab
  is the tool for that (decision 2026-07-24).
- **A rehearsal app/UI** — the sync file is the contract; consumers are
  out of scope (same stance as the ln-reader split).

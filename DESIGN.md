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
  │        │  │  per-volume   │  │  (Gemma 4)  │  │   match)     │  │ directions)│  │  box)    │  │      │
  │        │  │  cast         │  │             │  │              │  │            │  │          │  │      │
  └────────┘  └───────────────┘  └─────────────┘  └──────────────┘  └────────────┘  └──────────┘  └──┬───┘
                     │                                  ▲                                            │
                     │                                  │ optional --narrator-clip override          ▼
                     │                                  │ + auto-reuse of prior volumes' casting    ┌─────────────┐
                     ▼                                                                              │ 6. Sync     │
              (per-volume cast)                                                                     │  epub→spans │
                                                                                                    └─────────────┘
```

**Critical ordering**: voice casting is **stage V**, after scene segmentation
and **before** the Director. The Director composes voice descriptors that
*match the assigned reference clip*, so a clip's actual gender / age / accent
drives the descriptor prefix Dramabox sees.

**Series & volumes**. Book IDs are hierarchical, `<series>/volume-NN`
(e.g. `toaru/volume-02`). When processing a non-first volume, the pipeline
auto-detects prior volume(s) in the same series (§6.4) and reuses their voice
clip assignments for recurring characters (V). The cast itself (s1) is
extracted per volume — prior casts are not merged in (§2.2).

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
   filename stem `cover`. The `images` list is in reading order: cover first,
   then each spine page's `<img>`/`<image>` references in document order, then
   any manifest image no page references. A reference is resolved relative to
   the page's own href (percent-decoded, `..` collapsed — `../Images/a.jpg`
   from `Text/ch1.xhtml` is `Images/a.jpg`), with a basename match only as
   the last resort.
4. Walk the spine in order. Skip front/back-matter stems matching
   `cover/toc/tocimg/copyright/signup/insert\d+/bonus\d+/color\d+`. For each
   remaining XHTML, extract `<h1>`/`<h2>` as title and concatenate `<p>`
   text. Group multi-part chapters (`chapter1.xhtml` + `chapter1_1.xhtml` +
   `chapter1_2.xhtml`) by stripping the trailing `_N` suffix. A title that is
   only a numeral (`<h2>III</h2>`) is replaced by the nav/NCX entry for that
   page when it names it.
5. Write `NN-<slug>.txt` per chapter group (NN = group order in the spine,
   zero-padded to the width the chapter count needs, minimum 2 — `001-…` for
   a 100+-chapter book; first line = title, blank-line-separated paragraphs
   as body). Re-extracting into the same folder first deletes the chapter
   files the previous run listed in its `.epub_meta.json` (and nothing else),
   so a run with different options can't leave a stale second set behind.
6. Write `.epub_meta.json` capturing title / authors / publisher / language /
   cover-image / images list / chapter map (`file`, `title` and
   `source_parts` for each chapter so Stage 6 can re-align back to the
   original XHTML) / `visual_elements` (lecture mode, §13.2).

Stage 0 (`lnvox ingest`) detects `.epub_meta.json` and propagates the cover
image path into `00_book_meta.json` for Stage 5 to embed in the final m4b.

### 2.1 Ingest

- Input: a folder of `.txt` files (typically the output of Stage 0a, or
  manually-prepared).
- Output: `artifacts/<book>/00_text.jsonl` — one record per chapter:
  `{chapter_id, title, text}`.
- Book id: `--book-id`, else the folder's path under `./novels`
  (`novels/toaru/volume-02` → `toaru/volume-02`, keeping the series for
  §6.4), else the folder name.
- Parser: `.txt` only — first non-empty line = title, the `NN` filename
  prefix = `chapter_id`, files ordered by that prefix NUMERICALLY (`9-…`
  before `10-…`). EPUB goes through Stage 0a. A `.md` parser was planned
  but never implemented. (Updated 2026-09-28.) Later stages order chapter
  files by name, so a hand-made folder should zero-pad its prefixes as
  Stage 0a does, or mixed widths (`9-`, `10-`) misorder after Stage 0.
- If `.epub_meta.json` is present, it is copied to `00_book_meta.json`
  (cover path made absolute) so Stage 5 can embed the cover.

### 2.2 Stage 1 — Character extraction (Gemma 4)

Run **per chapter** then **merge globally**. Per-chapter keeps prompts short
and avoids the 256K context being mostly wasted; the merge step deduplicates
aliases ("Lord Vex" / "Vex" / "the duke").

**Cross-volume continuity.** When the book ID is `<series>/volume-NN`, the
pipeline detects prior volumes in the same series: sibling directories of
`artifacts/<series>/` whose name has the same prefix (case-insensitive) and a
smaller trailing number, in numeric order (§6.4). Continuity — a recurring
character keeping the same voice — is applied at **voice-cast** time, which
reuses a clip assignment from any prior volume by canonical name or alias
(§6.4). The Stage 1 merge itself is per-volume and deliberately does NOT feed
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
   enumerated names whose enumerators differ are never fused — digit runs
   ("Knight 1"/"Knight 2") and a trailing single letter or roman numeral
   ("Attendant A"/"Attendant B", "Guard II"/"Guard III"); the same guard keeps
   a key those names share from counting as identity-safe. Each cluster
   combines its members' fields by rule (canonical =
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
   net refuses any proposed group that would fuse a known gender split —
   checked against the whole union built so far, so `[Male, Unknown]` then
   `[Unknown, Female]` can't chain a man and a woman together. A hard
   input-size guard (`_MERGE_INPUT_CHAR_BUDGET`) and an unusable model answer
   fall back to the deterministic clusters; a transport error (server down)
   fails the step instead, so the launcher's retry can restart the server
   rather than an un-merged cast being written. (Updated 2026-09-28.)

Outputs: `01_characters.json` (the merged cast) and
`01_characters_merge_log.json` — provenance recording what merged into each
cluster, which clusters are "lone" (single chapter), and each one's final
disposition.

> **Model choice.** The pipeline default is **Gemma 4 12B QAT**
> (`google/gemma-4-12B-it-qat-q4_0-gguf` on llama.cpp, every platform, §14).
> **Gemma 4 E4B** is the light dev pick (fits in ~10 GB VRAM, ~2× faster;
> still vLLM's default) and **Gemma 4 31B** the heavy one. Any backend, same
> endpoint — only `--llm-model` / `LNVOX_LLM_MODEL` changes. (Updated
> 2026-09-28; v1 recommended E4B for dev and 31B Dense for production, on
> vLLM.)

### 2.3 Stage 2 — Scene & speaker segmentation (Gemma 4)

Input: chapter text + global cast list. Output per chapter:
`02_scenes/<chapter_id>.json`.

**Two passes, not one.** A single call previously did three different jobs —
find scene boundaries, tag every line as narration/dialogue, and reproduce the
text — in one large output. Splitting them raises fidelity (which §2.8 shows is
the hard ceiling on sync match rate) because each prompt is simpler, and keeps
each call small enough to afford the `source_span` field below.

**Pass 2a — Scene boundaries.** Input: the chapter text with numbered
paragraphs (one call per chunk). Output: scenes with `scene_id`,
`location_hint`, the `cast` present, and `start_paragraph` / `end_paragraph` — **no
beats**. This is a bounded task (pick boundaries among numbered paragraphs),
the same kind the chunker already does reliably
([`chunker.py`](src/lnvox/llm/chunker.py)), so the model is dependable here.
(The `cast` list is 2a metadata used to prime/debug 2b; it is not persisted on
the final merged `Scene`, which keeps only the paragraph range + beats — see
the JSON below.)

The prompt asks for scenes that tile the chapter, and code now enforces it
(`_tile_boundaries`, updated 2026-09-28): ranges are clamped to the chapter,
sorted, a gap is folded into the earlier scene (a head gap into the first),
an overlap is trimmed off the later scene (one fully covered is dropped), and
the last scene is stretched to the final paragraph — Pass 2b only tags the
paragraphs inside a range, so a gap used to drop text from the audio and an
overlap voiced it twice. Each repair is logged. Both passes size their output
budget with `LLMClient.budget_for`, clamped to what the served context
(`max_model_len`, §4) leaves after the prompt.

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
paragraphs so each chunk carries its base paragraph index. Pass 2a's prompt
numbers a chunk's paragraphs locally (1..N) and the returned ranges are offset
by that base, so they are chapter-global. Scene ranges are therefore directly usable by
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

Input: `01_characters.json` + `<voicebank>/manifest.json` (§17.5), plus this
volume's existing `04_voice_assignments.json` and prior volumes' castings when
present.
Output: `04_voice_assignments.json` mapping each character (and the Narrator)
to a specific voicebank clip.

Two LLM calls per character:
1. **Target inference**: from the character's description + voice descriptor
   (passed through from any prior voice profile), Gemma emits search metadata:
   `{ gender, age_band, accent_keywords, timbre_keywords, manner_keywords }`.
2. **Ranked match**: hard-filter the voicebank by gender + age_band, soft-
   filter by accent, then ask Gemma to rank the remaining candidates and pick
   a top-3 (§6.2 for what is committed).

Characters whose gender is `nonbinary` / `unknown` are left unassigned (the
bank has no such clips); no LLM call is made for them.

**Narrator handling.** The Narrator is synthesised when s1 didn't produce one.
Unless this volume's casting has a locked Narrator, `--narrator-clip <clip_id>`
sets it — the Narrator voice is the single most audible voice in the finished
audiobook and is usually best chosen by hand; an id not in the bank exits 2
before any LLM call. Otherwise a prior volume's Narrator is reused, and a
first cast skips target inference for a fixed `female / adult` target with an
`england` preference (§6.3). (Updated 2026-09-28; this used to infer the
demographics from an s3 descriptor, defaulting to male/adult.)

**Cross-volume reuse.** For non-first volumes, the assignments of EVERY prior
volume are folded into one index (newest wins); a character matching a prior
assignment by canonical name or alias keeps the prior clip and skips both LLM
calls. This volume's own existing casting is a further layer: a `locked`
entry beats everything, an unlocked one only fills in where no prior volume
knows the character. Details and precedence in §6.4.

### 2.5 Stage 3 — Director (stage directions)

This is the stage that **aligns the pipeline to Dramabox's input format**.
Dramabox expects a voice/performance descriptor immediately followed by the
quoted line — one beat per `prompt`:

```
(mid-fifties british male - dry gravelly baritone - weary disappointment) "You're late."
(young adult female - bright quick voice - breathless - defensive) "I came as soon as I could."
```

(The `prompt` field is built by `format_prompt(direction, text)` in
[s3_director.py](src/lnvox/stages/s3_director.py) as
`(<direction>) "<text>"`, the direction lowercased and its commas turned into
` - `; the spoken `text` keeps its own punctuation. Lecture mode (§13) and
scenario mode (§17) build the same shape. Updated 2026-09-28: the original
`<direction>, "<text>"` form had Dramabox read the direction aloud — found by
ear in the Studio's TTS Lab, §12.1 op 6.)

The Director runs **after** voice cast (§2.4), so it knows each speaker's
assigned reference clip and can generate a voice descriptor that's
**consistent with that clip's actual gender / age / accent**. For each beat:

1. Speaker descriptor — derived from BOTH the character's personality (s1)
   AND the assigned clip's metadata (Stage V). A character cast on a
   `female/adult/england` clip gets a descriptor that says "adult British
   female", never "young adult male". One LLM call writes the descriptors
   for the whole cast (`03_voice_profiles.json`), and gender / age / accent
   are snapped back onto the clip's values afterwards; a casting that already
   carries a `voice_descriptor` (the Narrator's clip-derived one, a hand
   edit) is used verbatim.
2. Performance cue — one short (2–8 word) cue per dialogue line, emotion plus
   any vocal action (`exasperated, half-shouting`, `low and dangerous`),
   written from the whole scene (narration and dialogue in order).
   `direction` = `<descriptor>, <cue>`; narration beats are voiced by the
   Narrator and carry the descriptor alone.

Before any of this, s3 folds s2's free-text speaker tags onto cast names
(a leading article, an alias, "the narrator"), so a tag like "The Square"
keeps its cast voice instead of rendering with no reference clip.

A merge pass fuses consecutive same-speaker beats (capped at 375 chars per
beat — short beats keep Dramabox's noise/intelligibility in check; see §2.6).

Output: `03_directed/<chapter_id>.json` — same shape as scenes, but every
beat has a `speaker`, a `direction` string and a `prompt` field with the
fully-formatted Dramabox input, and the chapter records the descriptor each
speaker was composed with (`voice_descriptors`).

The cue LLM call is **per scene**, not per beat — context matters and
per-beat would hammer the model with redundant prompts. An unparseable answer
only costs that scene its cues; a transport error fails the step.

**Caching (casting-aware, updated 2026-09-28).** Each profile in
`03_voice_profiles.json` records what it was written for (the clip and any
stored descriptor); on a re-run only characters whose clip or descriptor
changed get a new profile (`--regen-profiles` regenerates all). A cached
chapter is reused while its `voice_descriptors` match; when a recast changed a
speaker's descriptor its prompts are re-voiced from the stored cues with no
LLM call, and only a changed speaker set sends it back through the Director.
So a recast after s3 needs no deleted files: s3 re-voices, and s4 re-renders
the affected beats (their prompt, hence cache key, changed).

### 2.6 Stage 4 — TTS (Dramabox)

For each beat in scene order:
- Load Dramabox once (it's 3.3B + Gemma 3 12B conditioner — sticky in VRAM).
- Look up the assigned `ref_clip` for the speaker from `04_voice_assignments.json`.
- Render `prompt` → `<beat_id>.wav` (48 kHz stereo).
- Cache by content hash of `(prompt, ref_clip_filename, model_version)` so
  re-runs after editing one line don't re-render the whole book.

Output: `05_audio/<chapter_id>/<beat_id>.wav` + `manifest.json` per chapter.

(That is the monolithic path. The launcher's default since 2026-07-03 is the
staged path, §15, which loads one model per phase instead of all four at once.)

**The recipe is in the cache key (updated 2026-09-28).** `model_version` is
`DramaboxClient.MODEL_VERSION + s4_variant_tag(device, staged)`, a suffix for
the *effective* render recipe on that device, spelled as deviations from the
original v1 recipe (so the tag doesn't move when defaults do). Staged runs
default to the bf16 encoder, 22 s chunks and an fp32 vocoder, tagged
`+enc-bf16+cap22+dec-fp32`; the monolithic path keeps the v1 recipe (untagged)
unless `LNVOX_S4_ENCODER_PRECISION=bf16`. The three `LNVOX_S4_*` revert knobs
re-key on CUDA/CPU, but on MPS they can't take effect (no bitsandbytes, the
F11 long-chunk crash, the fp16-vocoder failure — §11.3), so they
don't change the tag either — every staged MPS render is
`+enc-bf16+cap22+dec-fp32` (or `+capN` for an explicit cap). An empty knob
value counts as unset. A malformed value, or the staged-only chunk-cap /
decode-dtype knobs given explicitly to the monolithic path, makes `lnvox s4`
exit 2 before any work — as does monolithic Dramabox on MPS, which would
render silence (§11.3). Exit 2 is never retried (below).

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
denoise (likely CUDA fragmentation on RTX 50-series). On the default staged
path the driver relaunches the failed phase (§15.4). For monolithic Dramabox
(`LNVOX_S4_STAGED=0`) and VibeVoice, `scripts/s4_retry.sh` re-invokes the
stage until it exits clean; the content-hash cache ensures every restart is
a cheap resume. It stops at once with exit 2 when `lnvox s4` exits 2 — a
deterministic refusal a retry would only replay — and retries everything
else, crashes by signal included. (Updated 2026-09-28.)

**Throughput note.** Dramabox is diffusion-based; expect ~real-time-to-2×-RT
on a 4090. A 100k-word novel ≈ 9 h audio ≈ 2-3 h render (warm). Plan for an
overnight run for a full novel.

### 2.7 Stage 5 — Mix

Concatenate beats with silence padding (configurable, defaults: 250 ms intra-
scene, 1 s inter-scene, 2 s inter-chapter, plus an optional per-beat lead-in —
0 for books, 0.35 s in scenario mode, §17.4). All audio plumbing is delegated to
system `ffmpeg` — no in-process audio decoding.

Pipeline per chapter, then per book:
1. Generate three silence WAVs at 48 kHz stereo (a fourth for a lead-in).
2. Concat each chapter's beats interleaved with the silences → chapter WAV.
3. Concat all chapter WAVs with inter-chapter silence → book WAV.
4. Single-pass `loudnorm` to target −18 LUFS / −2 dB TP.
5. AAC encode and mux into `.m4b` (mp4 container) with chapter markers via
   ffmetadata, and the cover + illustrations as attached pictures (§23).
   `+faststart` flag makes the file streamable.

Output: `06_final/<title>.m4b` with chapter markers + a
`<title>.timings.json` sidecar (`/` in the title becomes `_` in both names).

(Updated 2026-09-28.) The sidecar is the mix's **layout contract**: s6,
scenario-sync and `lnvox sample` replay it instead of taking the layout from
their own flags, which used to drift from the s5 run (a `--lead-in`, a
`--chapters` subset, a `--cover` override). Keys: `book_title`, `m4b`,
`audio_dir` (`05_audio` / `05_audio_v2`), `total_duration_seconds`,
`target_lufs`, `intra_silence`, `inter_scene_silence`,
`inter_chapter_silence`, `lead_in_silence`, `embedded_images` (the pictures
in embed order, cover first) and `chapters[]` (`chapter_id`, `title`, and the
probed `start_seconds` / `end_seconds` / `duration_seconds` in the m4b). It
is written only after the encode succeeds; readers take the newest sidecar
unless given `--title`. The work dir `06_final/_work/` (~3 uncompressed
copies of the book) is deleted after a successful mix unless `--keep-work`,
and kept on failure for debugging.

### 2.8 Stage 6 — Sync layer (optional)

Players that highlight the current beat in sync with playback (WebKit reader,
Audiobookshelf, Plex Audiobooks, a custom front-end) need a mapping from
audio time → highlighted text span in the source. Stage 6 produces that
mapping by re-aligning the Stage-3 beats onto the original EPUB's XHTML.

Implementation: [`src/lnvox/stages/s6_sync.py`](src/lnvox/stages/s6_sync.py),
CLI `lnvox s6 <book> [--epub PATH] [--title T]`.

Inputs: `03_directed/*.json` (beat texts) + `05_audio/<chapter>/manifest.json`
(per-beat durations) + the mix's `06_final/<title>.timings.json` (§2.7; the
newest unless `--title`) + original EPUB from `epubs/<series>/<vol>.epub` (the
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
  match_confidence}`), `images[]`, `visual_elements[]`, a top-level
  `match_confidence` histogram, the `silences` used
  (`intra/inter_scene/inter_chapter/lead_in_seconds`) and the `m4b` they
  describe. Timings are cumulative through the Stage-5 silence layout
  (intra/inter-scene/inter-chapter pads plus the per-beat lead-in).
  (Updated 2026-09-28: s6 used to rebuild that layout from its own flags,
  which had to match Stage 5's by hand. It now replays the mix's sidecar —
  pads, lead-in, the chapters actually mixed and in what order — and a pad
  flag only overrides the record, with a warning. Directed chapters not in
  the mix are skipped. Each chapter is re-anchored at the start s5 measured
  for it, so a chapter re-rendered after the mix can't shift the later ones;
  one whose replayed length is > 0.5 s off the mixed chapter gets a
  `[WARN]` to re-mix. A `--v2` (VibeVoice) mix is refused — no per-beat
  timing.)
- `images[]` (in the same manifest) — exactly one entry per picture embedded
  in the m4b, in embed order, cover first:
  `{src, xhtml, chapter_id, trigger_seconds, after_beat_id, before_beat_id}`
  (§23.3). Every occurrence of every picture is listed in `visual_elements[]`
  (`kind: "image"`, plus the lecture kinds, §13.5). Light-novel
  inserts/color/bonus pages are their own image-only spine items (skipped
  from text by Stage 0a); we walk the OPF **spine order** to place each
  between the preceding XHTML part's last beat and the following part's
  first beat — per-PART, so an insert between `chapterN.xhtml` and
  `chapterN_1.xhtml` triggers at the right mid-chapter beat (those marks also
  carry `spine_page`, `after_chapter`, `before_chapter`). Front matter
  (`after_beat_id: null`) triggers at the first following beat's start — 0 s
  ahead of the first chapter; end matter (`before_beat_id: null`) and a
  picture no page references have `trigger_seconds: null`.
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
     first, then the ladder: `exact` / `anchored`, `head-only`, `tail-only`,
     then fuzzy `SequenceMatcher` across the whole gap
     (≥`_FUZZY_MIN_RATIO=0.20`). Lenient matches claim ONLY the verified
     anchor/substring, never `cursor→anchor`. Repeated up to 3 rounds.
     (Updated 2026-09-28: a `backtrack` rung — searching up to 1500 chars
     before the cursor — was removed: it could never fire, since Pass 2
     searches each gap slice from its start and the backward slack already
     covers that window. Its histogram key stays, always 0.)

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

**Tuning knobs** (`s6_sync.py`): `_MAX_FORWARD_JUMP` (8000),
`HEAD_ANCHOR_LEN`/`TAIL_ANCHOR_LEN` (30/30), `_FUZZY_MIN_RATIO` (0.20),
`_FUZZY_WINDOW_BASE` (3000), and `_PASS2_BACKWARD_SLACK` (2000, set inside
`run`).

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

(Updated 2026-09-28: the tree above is the original sketch. The package also
has `stages/{lecture,scenario,scenario_sync,s4_vibevoice,sample}.py`,
`ingest/{blocks,render,scenario}.py`, `llm/chunker.py`, `tts/` (Dramabox,
staged s4, VibeVoice, trim, the Studio's lab engine) and `config.py`. Each
stage's `run(...)` takes already-loaded inputs (pydantic models, an
`LLMClient`, output paths) rather than `(book_id, config)`; `cli.py` loads
the files and calls it, one command per stage, with no mtime-based skipping —
stages resume from their own per-chapter / content-hash caches (§5), and
`scripts/run_pipeline.sh` chains the commands (§9). The documentation site
under `docs/` has one page per module.)

## 4. Serving topology

- **LLM server** (one process) hosts Gemma 4 — `llama-server` by default
  (§14), vLLM with `--enable-prefix-caching`, or mlx-lm (§11). All LLM stages
  reuse it. Use the OpenAI-compatible endpoint so we can swap to
  Claude/Gemini for evals by changing one env var. (Updated 2026-09-28; v1
  planned vLLM only.)
  - **Endpoint.** The launcher resolves the URL once — `--vllm-url`, else
    `LNVOX_LLM__ENDPOINT`, else `LNVOX_LLM_ENDPOINT`, else
    `http://localhost:${LNVOX_LLM_PORT:-8000}/v1` (the port the serve scripts
    bind) — and exports it as `LNVOX_LLM__ENDPOINT`, so its readiness probe
    and every stage talk to the same server. The launcher reads the process
    environment only; the CLI's `Settings` also read `.env`.
  - **Context window.** Two consumers: the serve scripts size the server from
    `LNVOX_LLM_MAX_LEN`, the client clamps its per-call output budgets
    (`LLMClient.budget_for`) with `llm.max_model_len` (default 65536; read
    from `LNVOX_LLM__MAX_MODEL_LEN`, else `LNVOX_LLM_MAX_MODEL_LEN`, else
    `LNVOX_LLM_MAX_LEN`). The launcher's `--max-model-len N` exports all
    three. When the launcher itself starts `serve_vllm.sh` for a `*31B*` /
    `*27B*` model and no context length is set anywhere, it applies that
    script's 20480-token default and exports it the same way (not with
    `--vllm-url`, whose window it can't know). mlx-lm has no such knob.
- **Dramabox** runs as a separate Python process — keeps the audio model
  isolated from the LLM's VRAM and lets us schedule it after all LLM stages
  finish (or on a second GPU in parallel).
- **No queue/broker in v1.** Stages are local function calls. If we later need
  parallel chapter rendering, drop in `concurrent.futures.ProcessPoolExecutor`
  for stage 4.
- **Apple Silicon variant.** vLLM is swapped for Apple's `mlx_lm server`
  (same OpenAI endpoint contract, so `LLMClient` is unchanged) and Dramabox
  runs on the MPS device, staged only, with a per-component dtype map and
  quantization + `torch.compile` disabled. Full details and known
  limitations live in §11. (Updated 2026-09-28: mlx is opt-in via
  `--llm-backend mlx`; llama-server is the default on Macs too.)
- **GGUF / llama-server variant** (§14). A third backend, native
  `llama-server` from llama.cpp, runs on either platform and shares the
  OpenAI endpoint contract. Introduced for when an MLX-quantized build of
  the desired model isn't available, or as the macOS fallback if mlx-lm
  doesn't work on your hardware; since 2026-07-25 it is the default backend
  on every platform (§14).

## 5. Storage & idempotency

- Only two caches are content-keyed: the TTS cache below and the
  scenario-ingest LLM cache (§17.3). The LLM stages cache **per chapter by
  file**: an output that exists is reloaded, not re-keyed on its inputs —
  s1's `01_characters_per_chapter/`, s2's `02_scenes/`, and `03_directed/`
  for s3 / lecture / scenario. Those last three do check what a cached
  chapter was written for (the speakers' voice descriptors, the normalize
  setting) and re-voice or rebuild it (§2.5, §13.3, §17.3); any other input
  change means deleting that chapter's file. There is no orchestrator: the
  launcher re-runs every stage and relies on these caches. (Updated
  2026-09-28; the v1 plan was content-hashing every stage output.)
- TTS cache lives at `cache/tts/<key>.wav` (key = the first 16 hex digits of
  the sha256 of prompt + ref-clip filename + model version, §2.6) — survives
  book deletions so you can re-run the same beat with a different ref clip
  cheaply.
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

Only ranked choices whose clip id is one of the candidates actually offered
are kept — an invented id used to pass here and fail only at s4. The top-1
of those is committed to `04_voice_assignments.json`; the others are kept so
a human reviewer can swap in a different pick without re-running the LLM. If
none survives, the first candidate in filter order (accent matches first,
then manifest order) is committed with an `audit_note` saying why, and
`voice cast` flags the line. (Updated 2026-09-28.)

### 6.3 Manual narrator selection

The narrator is voiced over a *huge* portion of the book (~60-70% of beats
in third-person novels). Auto-casting it usually produces something
acceptable but rarely something great. The pipeline launcher exposes a
`--narrator-clip <clip_id>` flag so the user picks the narrator by ear after
seeding the voicebank.

If `--narrator-clip` is omitted, a Narrator locked in this volume's casting
is kept, else a prior volume's Narrator is reused (§6.4). A first cast skips
target inference: the target is fixed at `female / adult` with `england` as
an accent *preference* (the soft filter puts those clips first; nothing
excludes other accents), relaxed to any-age female and then to any clip only
if the bank has none; the LLM ranks the candidates (a lone candidate is taken
as is) and the descriptor is derived from the chosen clip
(`descriptor_from_clip`). (Updated 2026-09-28; the narrator used to be
auto-cast like any other character, from demographics guessed out of a
voice descriptor.)

A `--narrator-clip` id not in the bank exits 2 before any LLM call, with
close-match suggestions; against a locked Narrator the flag is ignored with a
warning.

### 6.4 Cross-volume continuity

Voice continuity across a series is non-negotiable for listeners.
When a non-first volume of a series is processed, the pipeline:

- Auto-detects prior volume artifacts: sibling directories of
  `artifacts/<series>/` whose name has the same prefix as the current one
  (case-insensitive) and a smaller trailing number, ordered numerically
  (`volume-9` before `volume-10`, padded and unpadded mixed freely); hidden
  and non-matching dirs (`extras/`) are ignored ([series.py](src/lnvox/series.py)).
- Folds **every** prior `04_voice_assignments.json` into one index, oldest →
  newest (newer wins), keyed by each character's canonical name, its aliases
  (from that volume's `01_characters.json`) and token-sorted variants
  ("Misaka Mikoto" = "Mikoto Misaka"). A key two characters of one volume
  share is dropped. So a character who skips a volume, or whom s1 renames
  ("Kamijou Touma" → "Kamijou"), keeps the voice. (Updated 2026-09-28; it
  used to read only the latest volume, by exact canonical name.)
- For every character in the current volume that matches a prior
  assignment, **reuses the prior clip** verbatim (no LLM call). A match
  through an alias on either side also requires the genders to agree, and a
  stored clip that is gone from the bank, or contradicts both genders, is not
  propagated.
- For characters new to this volume, runs the standard two-call matching.
- For the Narrator, the prior clip is reused **unless** `--narrator-clip`
  is explicitly passed (allowing intentional narrator changes between
  volumes if desired).

This volume's own existing `04_voice_assignments.json` is matched the same
way and forms two layers. An entry marked `"locked": true` (a deliberate hand
edit — the Studio's Casting tab sets it, §12.1) beats every rule: prior
volumes, `--narrator-clip` and `--fresh`; only a locked clip missing from the
bank is re-cast, with an `audit_note`. An unlocked entry is the weakest
layer: it keeps its clip only when no prior volume knows the character, so a
re-run is idempotent without overriding continuity. `--fresh` drops the
unlocked entries. A locked entry is kept whole; otherwise, whenever the
chosen clip is the entry's own, its `audit_note` and `voice_descriptor` carry
over (a descriptor written for a different clip never does).

### 6.5 YouTube ref clips (deferred to v2)

Not yet implemented. When added, will sit alongside `common_voice` as a
source type with `license: "personal_use_only"` flag that propagates into
final-output gating.

## 7. Configuration

(Updated 2026-09-28: the per-book `config.yaml` below was never implemented.
Configuration is `LNVOX_*` environment variables — also read from `.env` —
parsed by `lnvox.config.Settings` (artifacts dir, voicebank, LLM endpoint /
model / context / timeouts), the `LNVOX_S4_*` render knobs, and per-command
CLI flags; the launcher's options are the per-book knobs. The sketch is kept
for the design record.)

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

1. **LLM phase** (LLM server up, Dramabox down):
   - Ingest
   - s1 (per-volume cast — series voices carry over at voice cast, §6.4)
   - s2 (scenes)
   - Voice cast (with `--narrator-clip` override + cross-volume reuse)
   - Casting audit (report-only by default; §20.5)
   - s3 (director, using assigned clip metadata)
2. **GPU handoff**: stop the LLM server, free VRAM.
3. **TTS phase** (Dramabox up):
   - s4 (TTS) — the staged path by default (`LNVOX_S4_STAGED`, default 1,
     §15), whose driver owns crash retries. `s4_retry.sh` wraps only the
     monolithic path (`LNVOX_S4_STAGED=0`) and VibeVoice, and stops at once
     on exit 2; if it fails the launcher aborts rather than mix a partial
     render. (Updated 2026-09-28; s4 used to always run under `s4_retry.sh`.)
4. **No-GPU phase**:
   - s5 (mix + m4b)
   - s6 (sync), when the source EPUB is present

The launcher starts the selected LLM server itself, waits for it, and stops
it before s4; `--vllm-url` points it at an already-running server it leaves
alone. It can be re-run safely: stages resume from their per-chapter /
per-beat caches (§5). A failed step is retried (`--step-retries`, default 3),
except on exit 2 — lnvox's status for a deterministic refusal — which aborts
at once. (Updated 2026-09-28; the first design had the user stop and start
the servers at "STOP vLLM NOW" / "STOP Dramabox NOW" prompts.)

The backend is not picked by OS: `llama-server` is the default everywhere
(§14), and `--llm-backend mlx` runs `scripts/serve_mlx.sh` on Apple Silicon.
The launcher passes no `--device` — `lnvox s4` auto-detects MPS — and on
Darwin it wraps the whole run in `caffeinate`. No other orchestration changes
(see §11). (Updated 2026-09-28; this used to read "Darwin → serve_mlx.sh +
`--device mps`".)

## 10. Dependencies (actual)

Core (`pyproject.toml`):
- `openai`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `jinja2`,
  `beautifulsoup4` (EPUB parsing)

`serve` extra (vLLM phase, Linux/CUDA):
- `vllm>=0.22.0` (nightly cu130 wheel index), `transformers>=5.10.1`,
  `torch` (cu130 wheels via `[tool.uv.sources]`), all marker-gated
  to `sys_platform == 'linux'` so `uv sync` doesn't try to fetch CUDA wheels
  on macOS — see §11.

`mlx` extra (Apple Silicon LLM phase):
- `mlx-lm` (provides the `mlx_lm server` subcommand, OpenAI-compatible).
  Marker-gated to `sys_platform == 'darwin'`, and git-pinned to a commit in
  `[tool.uv.sources]`: the default 12B QAT model's `gemma4_unified` type needs
  an upstream fix no PyPI release carries yet (MAC_TEST_REPORT.md F7). See
  §11 for the serving topology.

`render` extra (lecture mode, §13.2c): `pygments`, `playwright`.

`voice` extra (voicebank seeding):
- `soundfile`, `librosa`, `tqdm`
- System: `ffmpeg` (MP3 decode via librosa)

`tts` extra (Dramabox phase):
- `soundfile`, `huggingface-hub`, `soxr` (VibeVoice resampling, §16.4)
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
                        │   → mlx_lm server :8000  │
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
                        │ lnvox s4 (staged on MPS) │   device auto-detected
                        │   device="cuda" | "mps"  │   (--device overrides)
                        └──────────────────────────┘
```

The only stage that has a device of its own is s4. Every other stage is
either pure-Python (ingest, mix, sync) or talks to the LLM over HTTP — so
the *only* code that knows whether we're on CUDA or MPS is s4's: the CLI's
device auto-detect, the staged planner and phases (`staged.py`) with their
per-device knobs (the `s4_effective_*` helpers in `dramabox_client.py`), and
`DramaboxClient`'s refusal of MPS (§11.3). (Updated 2026-09-28; this used to
be "the s4 client factory" alone.)

(llama-server, §14, is the default LLM backend on Macs too since
2026-07-25; this diagram is the `--llm-backend mlx` path.)

### 11.2 LLM side — `mlx_lm server`

- `mlx_lm server` (the subcommand; the `python -m mlx_lm.server` module
  form is deprecated) ships an OpenAI-compatible `/v1/chat/completions`
  endpoint. `LLMClient` ([client.py:109-113](src/lnvox/llm/client.py#L109-L113))
  doesn't care which backend is behind that URL — beyond switching Gemma's
  thinking channel off per request for mlx and llama, keyed on
  `LNVOX_LLM_BACKEND`, which the launcher exports — so the only artifact
  needed on the MPS path is a new launcher: `scripts/serve_mlx.sh`
  (model + host + port + temperature — much smaller surface than the vLLM
  script; mlx-lm takes no context-length flag).
- Models are pulled from `mlx-community/*` (pre-converted MLX weights). The
  default is `mlx-community/gemma-4-12B-it-qat-4bit` (~7 GB), the MLX analog
  of the llama default. Its `gemma4_unified` model type needs the git-pinned
  mlx-lm (§10; PyPI releases raise "not supported" and then hang the request,
  MAC_TEST_REPORT.md F7). Alternates: the 31B QAT 4-bit (32 GB+ Macs, ~3×
  slower) and E4B (16 GB Macs; too weak for s2, F6). The server runs with
  `--temp 0.2`: mlx-lm's batched decode ignores per-request sampling, so at
  its greedy default every validation retry would replay the same invalid
  completion (F5). (Updated 2026-09-28.)

**Known limitations** of this path that the design accepts rather than
papers over:

1. **No `guided_json` enforcement.** vLLM honours
   `extra_body={"guided_json": schema}` (used in
   [client.py:204](src/lnvox/llm/client.py#L204)) so the model's output is
   constrained to the pydantic schema server-side. `mlx_lm server` ignores
   this field today. The client already validates + retries via
   `structured()`, so behaviour degrades from "guaranteed structural match"
   to "validate-and-retry"; expect a higher first-attempt parse-fail rate
   on Mac. Track upstream MLX structured-output work and revisit.
2. **No `repetition_penalty` knob.** The vLLM-specific
   `extra_body["repetition_penalty"]` lever
   ([client.py:205-212](src/lnvox/llm/client.py#L205-L212)) isn't honoured
   either. The runaway-loop escape hatch documented in
   [config.py:19-24](src/lnvox/config.py#L19-L24) goes away; the
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
DramaBox's source**. Anything beyond what our wrapper and planner do
([dramabox_client.py](src/lnvox/tts/dramabox_client.py),
[staged.py](src/lnvox/tts/staged.py)) is out of scope for v1.

(Updated 2026-09-28, from the Mac validation — MAC_TEST_REPORT.md F8–F11.)
The original plan was one table of `DramaboxClient.__init__` overrides: fp16
everywhere, `bnb_4bit` and `torch.compile` off. Followed as written, it
rendered silence with exit code 0. `TTSServer` builds every component in ONE dtype and no single dtype
works on MPS: fp16 Gemma encodings are all-NaN (F10) and the vocoder needs
fp32 (F9). So `DramaboxClient` — the monolithic path — refuses MPS outright
(`lnvox s4` exits 2 before loading anything), and MPS renders only through
the staged path (§15), which sets the dtype per component:

| Component (staged phase)            | CUDA default                            | MPS                                   | Why (MPS)                                                                                                   |
|-------------------------------------|-----------------------------------------|---------------------------------------|-------------------------------------------------------------------------------------------------------------|
| VAE encoder / RE-USE refs (`refs`)  | bf16                                    | fp16                                  | Works as-is.                                                                                                |
| Gemma prompt encoder (`ctx`)        | bf16, unquantized (`bnb4` on request)   | bf16, unquantized — always            | fp16 overflows to all-NaN (F10); bitsandbytes is CUDA-only and the pre-quantized checkpoint can't load without it (F8), so `unsloth/gemma-3-12b-it` is fetched. |
| DiT (`denoise`)                     | bf16, `torch.compile`                   | fp16, eager                           | Works on short chunks; `torch.compile` is flaky on MPS for diffusion models.                                 |
| Vocoder / decoder (`decode`)        | fp32 (`auto` → bf16)                    | fp32 — always                         | fp16 hits a float-vs-half conv clash (F9).                                                                  |
| Chunk cap                           | 22 s (`none` → 45 s)                    | 22 s; `none` can't lift it (a number still wins) | The DiT's (T, T) mask hits a torch-MPS boolean-indexing bug on beats ≳25 s (F11).                |

The map lives in `staged._knobs()` and the phases, with the `s4_effective_*`
helpers in `dramabox_client.py` as the single source of truth (they also
build the cache tag, §2.6). The same recipe later won a blind A/B on CUDA and
became the staged default there too (2026-08-04).

**Known limitations** the design accepts:

1. **No DramaBox source patches.** `torch.cuda.empty_cache()` and
   `torch.cuda.memory_allocated()` are called unconditionally inside
   DramaBox ([blocks.py:433,486](external/DramaBox/ltx2/ltx_pipelines/utils/blocks.py)).
   On MPS these raise / return zero. If a particular call site errors,
   the TTS run dies — we surface the trace rather than monkey-patch from
   our wrapper. Upstreaming or forking DramaBox is explicitly out of
   scope for v1.
2. **2× Gemma-encoder memory.** Without bnb the prompt-encoder Gemma (3 12B)
   loads unquantized in bf16: ≈ 24 GB unified memory for the encoder alone
   (plus a one-time ~23 GB download). The staged path loads one model per
   phase, so each gets the whole pool; a 36 GB+ Mac is still the
   recommendation.
3. **Throughput.** DramaBox is diffusion-based and unoptimised for MPS.
   Measured on an M4 Max (staged, final config): denoise ~69 s per ≤22 s
   chunk, about 3.6× real time — roughly 4 h of render per hour of audio,
   better than the 5–10× first expected (vs. ~1–2× on a 4090). The s4 cache
   and the staged driver's resume remain the right primitives. (Updated
   2026-09-28.)
4. **No RE-USE ref denoising on macOS** (`mamba-ssm` has no wheels): refs
   are used un-denoised, with a warning.

### 11.4 Code surface affected (design-only listing — no code yet)

| Area                                          | Change                                                                                                                                                       |
|-----------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `pyproject.toml`                              | Make `[tool.uv.sources].torch` Linux-only via a `sys_platform` marker; add an `mlx` optional extra with `mlx-lm; sys_platform == 'darwin'`, git-pinned in `[tool.uv.sources]` (F7). |
| `scripts/serve_mlx.sh` (new)                  | Apple Silicon analog to `serve_vllm.sh`. Picks an `mlx-community/*` Gemma checkpoint by `LNVOX_LLM_MODEL` (default `mlx-community/gemma-4-12B-it-qat-4bit`); launches `python -m mlx_lm server --model … --host … --port … --temp …`. |
| `src/lnvox/tts/dramabox_client.py`            | `DramaboxClient` refuses MPS (single-dtype `TTSServer`, §11.3); the render-knob readers and `s4_effective_*` per-device helpers live here. CUDA default behaviour preserved exactly. |
| `src/lnvox/tts/staged.py`                     | The per-component MPS dtype map and the 22 s MPS chunk cap with a word-split fallback (§11.3).                                                              |
| `src/lnvox/cli.py` (`stage4 --device`)        | Default flips from hard-coded `"cuda"` to an auto-detect helper (`mps` if `torch.backends.mps.is_available()`, else `cuda`). The flag stays a manual override. Monolithic s4 on MPS exits 2.|
| `scripts/run_pipeline.sh`                     | `serve_mlx.sh` with `--llm-backend mlx` (llama-server is the default on every OS, §14). Passes no `--device` (s4 auto-detects); wraps the run in `caffeinate` on Darwin. No stage ordering changes. |

(All implemented; rows updated 2026-09-28.)

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
   `BookCasting` back via `model_dump_json` — `target`, `ranked` and
   `candidates_considered` are round-tripped untouched. A reassignment also
   sets `locked: true`, so `voice cast` and `voice audit --fix` keep it
   (§6.4, §20.4), and replaces a stored `voice_descriptor` that described the
   old clip: re-derived from the new clip for the Narrator, cleared for
   anyone else so s3 composes one. (Updated 2026-09-28; it used to change
   `assigned_clip_id` only.) This is the by-ear counterpart to the
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
vLLM→Dramabox GPU handoff. With `--no-normalize` (§13.6) **and**
`--narrator-clip` lecture mode has **no LLM phase at all**: the whole vLLM half
of the launcher is skipped and the run is ingest → cast → split → TTS → mix.
(Without `--narrator-clip`, voice cast still auto-casts the Narrator with an
LLM ranking call, so the server is started.)

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
   `<table>` → `table`; `<figure>`/`<figcaption>` → `figure`; MathML `<math>`
   / display-equation blocks → `equation`. Footnotes: an `<aside>` typed as a
   note (`footnote`/`endnote`/`rearnote`/`note`), ANY element typed
   `footnote`/`endnote`/`rearnote` (a `<div>`/`<li>` note is one block),
   every block inside a notes container (`<section epub:type="endnotes">`),
   and a list whose text all sits in such notes → `footnote`. Whole spine
   pages are dropped (see (b)) by a boilerplate file stem, a drop-set
   `epub:type` landmark, a Project Gutenberg header/footer wrapper, a nav
   document with no prose, a leading heading whose *whole* text names a
   contents/index page ("Contents", "Index", "Glossary", "List of
   Figures"…), or a link-dense page with no paragraphs. The stem rule looks
   at the XHTML file's own stem, never heading text — so a chapter titled
   "Index Funds…" is kept — and extends the regex Stage 0a already applies
   (`cover/toc/copyright/signup/insert\d+/…`) with
   `toc|nav|index|bibliography|biblio|glossary|copyright|titlepage|halftitle…`.
   A landmark counts page-wide only on `<body>` or on top-level
   `<section>`/`<nav>`/`<article>`s holding all of the page's text; nested in
   a chapter (a chapter-end bibliography, a mini-TOC) it drops only its own
   blocks. (Updated 2026-09-28; the NCX is not consulted for dropping — the
   nav/NCX only rescue numeral-only titles, §2.0.)
2. **LLM fallback (only for ambiguous blocks).** Publishers vary wildly — some
   render code as monospace-styled `<p>` and ship boilerplate with no
   `epub:type`. A `<p>`/`<blockquote>` whose class or inline style hints at
   code (`code`, `listing`, `monospace`, a Courier font…) gets a single cheap
   LLM call on its first 2000 chars of markup, returning
   `{prose | code | table | figure | footnote | equation | drop}` + a
   one-line reason. Every other block is decided by the rules — an untagged
   plain `<p>` is prose — so most books make few or zero calls, and a book
   with clean semantic markup makes **none**. An unusable answer falls to
   prose; a transport error fails the ingest. (`--ingest-classifier none`
   forces rules-only: ambiguous ⇒ treated as prose.)

This is the same "deterministic clustering, LLM refine" division of labour as
§2.2 — fast, reproducible, debuggable, LLM only for the judgment rules can't make.

**(b) Drop list.** Dropped spine items / blocks are excised from
`00_text.jsonl` entirely (not rendered, not narrated, not a visual element).
Default drop set (`DEFAULT_DROP_TYPES` in
[blocks.py](src/lnvox/ingest/blocks.py); the planned `config.yaml` override
was never built — updated 2026-09-28):

- `toc` / `landmarks` — table of contents (a list of page numbers; never
  narrate).
- `copyright-page` / `titlepage` — copyright, ISBN/publisher boilerplate,
  "also by this author" ad pages.
- `index` / `bibliography` — back-of-book index and reference lists.
- `loi` / `lot` — lists of illustrations / tables; and, by stem or heading
  only, `glossary`.

**Kept and narrated:** the narrative body, plus `preface` / `foreword` /
`introduction`, and — deliberately *not* in the default drop set —
`dedication` / `acknowledgments` / `colophon` (short, and some listeners want
them; with no `config.yaml` there is no per-book switch to drop them).

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
(rasterizing those is a v2 thread, §13.9). The exception is a caption-only
figure whose image can't be resolved: it is rendered like a table.

**(d) Visual-element record.** Each kept-but-not-narrated block is recorded
alongside the existing `images[]` with the anchoring fields s6 already uses:

```json
{
  "kind": "code | table | figure | footnote | equation",
  "src": "images/code_3a8f1c9e04b2.png", // rendered PNG (code/table) or extracted asset (figure); "" if none
  "html": "<pre>…</pre>",               // verbatim block markup (reader fallback / footnotes/equations)
  "language": "python",                 // code-language hint, "" otherwise
  "chapter_id": "03",
  "after_paragraph": 41,                // chapter-global paragraph it follows (-1 = before the first)
  "spine_page": "chapter3"
}
```

(There is no `xhtml` key; the spine page's stem is `spine_page`. Updated
2026-09-28.)

s6 later converts `after_paragraph` → `after_beat_id` / `before_beat_id` /
`trigger_seconds` (§13.5), identical to how it places an inline image.

For **`.txt`** sources there is no structure to detect, so everything is prose
and nothing is dropped or rendered (literal read falls out naturally). The
planned **`.md`** pass (fenced code blocks and `![]()` figures as non-prose)
was never built — there is no `.md` parser at all (§2.1). **EPUB is the
realistic input for this mode** and gets the full ladder.

`lnvox ingest --mode lecture` also writes a **cast stub** `01_characters.json`
— `{"characters": []}`, and only when no cast file exists — so `voice cast`
(§13.4) runs completely unchanged, no s1 needed: `cast_book` synthesises the
Narrator itself for a cast that lacks one. (Updated 2026-09-28; the stub was
planned as a single `Character` named `Narrator`.)

### 13.3 Stage L — the lecture stage (s1+s2+s3 replacement)

`lnvox lecture <book>` ([`src/lnvox/stages/lecture.py`](src/lnvox/stages/lecture.py),
new). Input: `00_text.jsonl` + `04_voice_assignments.json` (for the narrator's
voice descriptor). Output: `03_directed/<chapter_id>.json`
(`ChapterDirected`, the **same schema s4 already consumes** — see
[schemas.py:226](src/lnvox/llm/schemas.py#L226)). Two sub-steps, neither of
which finds characters or scenes:

1. **Deterministic beat split.** Split each chapter into paragraphs with
   `split_paragraphs` ([chunker.py:95](src/lnvox/llm/chunker.py#L95)), then
   sentence-group each paragraph into beats ≤ `MAX_MERGED_BEAT_CHARS` (375
   chars / ~30 s) by **reusing `_split_long_text`**
   ([s3_director.py:405](src/lnvox/stages/s3_director.py#L405)) — the exact
   length policy Dramabox wants (§2.6). Every beat is `type:"narration"`,
   `speaker:"Narrator"`, and its `source_span` is the **verbatim** contiguous
   source slice (whitespace-collapsed only); `source_paragraph` records the
   chapter-global paragraph it came from (§13.5). Scenes are a thin grouping
   (one `DirectedScene` per chapter, `<chapter_id>_s1`) carried only so the
   downstream silence layout (§2.7) and `scene_id`-based beat ids stay valid.

2. **LLM speech-normalize** (the lecture stage's *only* LLM call, §13.6). For
   each beat, `text` = a speech-friendly rendering of `source_span` (numbers →
   words, units/currency/percent verbalized, common abbreviations expanded,
   symbols spoken). **`source_span` is never touched** — it remains the
   verbatim sync anchor. Beats go to the model in numbered batches of 25 (not
   one call per beat) for prompt efficiency; a batch whose answer is unusable
   keeps its beats verbatim, while a transport error fails the step.
   `--no-normalize` sets `text = source_span` verbatim and skips the LLM
   entirely.

   Cached per chapter in `03_directed/`: each chapter records whether it was
   normalized (`normalized`) and the narrator descriptor it was voiced with
   (`voice_descriptors`). Toggling `--no-normalize` rebuilds a cached
   chapter; a changed narrator descriptor (a recast) only re-voices its
   prompts, with no LLM call. (Updated 2026-09-28; the design said
   "per-paragraph-group".)

The `prompt` field is built by `format_prompt` — `(<narrator descriptor>)
"<text>"`, the same Dramabox shape s3 produces (§2.5) — where the narrator descriptor comes
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
  "prompt": "(adult - british - male - clear measured reading voice) \"By nineteen seventy-three, ARPANET linked forty hosts, see Figure three.\"",
  "source_span": "By 1973, ARPANET linked 40 hosts (see Fig. 3)."
}
```

Note `text` (spoken) ≠ `source_span` (verbatim) exactly where normalization
fired — and s6 anchors on `source_span`, so the synced EPUB still highlights
the original `"By 1973, ARPANET linked 40 hosts (see Fig. 3)."`.

### 13.4 Stage V — voice cast (Narrator only)

Unchanged CLI ([`voice cast`](src/lnvox/cli.py)). With the empty cast stub
from §13.2, it casts exactly one voice — the Narrator it synthesises. `--narrator-clip <id>` is the
expected path here (the narrator is 100% of a lecture audiobook — §6.3 applies
even more strongly than in narration mode); auto-cast falls back to
`DEFAULT_NARRATOR_*`. Cross-volume narrator reuse (§6.4) works as-is. Because
casting runs *before* the lecture stage builds prompts, the narrator descriptor
is available to step 1's `prompt` assembly — same "cast before you write the
descriptor prefix" ordering rationale as §2.4.

### 13.5 Stages 4 / 5 / 6 — unchanged code

- **s4 (TTS)** and **s5 (Mix)**: byte-identical code paths. One speaker → the
  content-hash cache (§2.6) and the crash retries (§15.4, `s4_retry.sh`)
  behave exactly as in narration mode. (Throughput is the same per-beat; a lecture book is usually shorter
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
batch's beats fall back to `text = source_span` (verbatim) — so a flaky
normalize call degrades to literal reading, never to a dropped beat (an
unreachable server fails the step instead). On the Apple-Silicon path
(§11.2) this is the *only* stage exposed to the "no `guided_json`" limitation,
and it degrades the same graceful way.

### 13.7 Code surface (implemented)

| Area | Change |
|------|--------|
| `src/lnvox/ingest/epub.py` | `--mode lecture` branch: run the §13.2a classification ladder; apply the §13.2b drop list; emit `visual_elements` (code/table/figure/footnote/equation) into `.epub_meta.json` alongside `images`. Narration mode unchanged. |
| `src/lnvox/ingest/blocks.py` (new) | Block classifier: deterministic rules (DOM tags + `epub:type` landmarks + file-stem / heading / link-density page rules) → `{prose,code,table,figure,footnote,equation,drop}`, with the LLM fallback for code-hinted blocks. |
| `src/lnvox/ingest/render.py` (new) | HTML→PNG renderer. Pygments-highlights code / styles tables → minimal HTML → Playwright (headless Chromium) → content-hash-named PNG in `images/`. Imports Playwright lazily so core install works without the `render` extra. |
| `src/lnvox/cli.py` (`ingest --mode lecture`) | Writes the `{"characters": []}` cast stub (§13.2) when no cast exists. (Planned for `ingest/text.py`.) |
| `src/lnvox/stages/lecture.py` (new) | The §13.3 stage: deterministic split (reusing `split_paragraphs` + `_split_long_text`) + normalize pass in batches of 25 → `03_directed/*.json`. Idempotent/cached per chapter. |
| `src/lnvox/llm/prompts/lecture_normalize.jinja` (new) | The §13.6 normalize prompt. |
| `src/lnvox/llm/prompts/block_classify.jinja` (new) | The §13.2a LLM-fallback classifier prompt (ambiguous, code-hinted blocks only). |
| `src/lnvox/llm/schemas.py` | Small `NormalizedBeats` + `BlockClass` schemas for the two new LLM calls. `DirectedBeat`/`ChapterDirected` reused unchanged. |
| `src/lnvox/stages/s6_sync.py` | Generalize `images[]` → `visual_elements[]` (add `kind` + the non-prose kinds); reuse the existing after/before-beat placement walk. Back-compat: images keep `kind:"image"`. (`images[]` itself later became one entry per embedded m4b picture, §23.3.) |
| `src/lnvox/cli.py` | New `lnvox lecture <book>` command; `--mode` + `--ingest-classifier {fallback,none}` + `--no-render` plumbed where ingest needs it. |
| `scripts/run_pipeline.sh` | `--mode lecture`: route ingest → voice cast → `lecture` → s4 → s5 → s6, skipping s1/s2/s3. Shrink (or with `--no-normalize`, drop) the vLLM phase. |
| `pyproject.toml` | New optional `render` extra: `pygments`, `playwright` (+ a `playwright install chromium` setup step). Lazily imported — only needed when rendering code/tables. |
| `config.yaml` | *Not built.* Planned: `mode: narration\|lecture`, `lecture: { normalize: true }`, `ingest: { drop: […] }`. No `config.yaml` is read; these are the CLI / launcher flags `--mode`, `--no-normalize`, and the fixed `DEFAULT_DROP_TYPES`. |

Nothing about the voicebank subsystem (§6), the Voicebank Studio (§12), the
TTS cache/idempotency (§5), or the s4/s5 stage contracts changes.

### 13.8 Known limitations the design accepts

1. **Normalize is best-effort, not exhaustive.** Domain math (LaTeX-dense
   passages), chemistry, dense tabular prose left inline, etc. will read
   imperfectly. The fallback is always literal `source_span`, so the worst case
   is "TTS does its raw best", never a crash or a dropped beat.
2. **Structure detection is EPUB-grade.** `.txt` carries no structure (all
   prose); `.md` has no parser (§2.1). A figure caption that an EPUB inlined as a
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
LNVOX_LLM_BACKEND=llama  → scripts/serve_llama.sh  (either platform; DEFAULT everywhere)
LNVOX_LLM_BACKEND=vllm   → scripts/serve_vllm.sh   (Linux + CUDA)
LNVOX_LLM_BACKEND=mlx    → scripts/serve_mlx.sh    (Apple Silicon)
```

(Updated 2026-09-28; this block read vllm on Linux, mlx on Darwin, llama
opt-in, before the 2026-07-25 flip.)

All three expose the same OpenAI-compatible `/v1/chat/completions`
endpoint on `:${LNVOX_LLM_PORT:-8000}`, so `LLMClient`
([client.py:109-113](src/lnvox/llm/client.py#L109-L113)) is unchanged. The
pipeline launcher's `--llm-backend` flag (or the env var above) picks one;
everything downstream is identical. The launcher resolves the endpoint and
context window the same way for every backend (§4): `--vllm-url` >
`LNVOX_LLM__ENDPOINT` > `LNVOX_LLM_ENDPOINT` > `http://localhost:${LNVOX_LLM_PORT:-8000}/v1`,
exported as `LNVOX_LLM__ENDPOINT`; `--max-model-len` exported as
`LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN`.

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
  `google/gemma-4-12B-it-qat-q4_0-gguf` (~8 GB) — Google's official
  QAT-quantized GGUF, validated across narration, lecture and scenario runs;
  `google/gemma-4-31B-it-qat-q4_0-gguf` is the heavier pick. (Updated
  2026-09-28; the default was the E4B GGUF before 2026-07-25.) Append
  `:Q4_0` etc. to pick a specific quant inside a multi-file repo.
- A local GGUF file path: `/path/to/gemma-4-31B-it-qat-q4_0.gguf`.

The serve script `-hf`'s the first form and `-m`'s the second, and also takes
`--llm-model` / `--port` / `--host` / `--max-model-len` / `--n-gpu-layers`
flags when run standalone. The launcher's `--llm-model` wins, else
`LNVOX_LLM__MODEL`, else `LNVOX_LLM_MODEL`, else the backend's default; it
exports the result under both names, so the client names the model the
server loaded.

### 14.4 Limitations vs vLLM

- **No `extra_body["guided_json"]` enforcement** — same loss as mlx-lm
  (§11.2.1). llama.cpp DOES support schema-constrained generation, but via
  `response_format: {"type": "json_schema", ...}` rather than the
  `guided_json` shape `LLMClient` currently sends
  ([client.py:204](src/lnvox/llm/client.py#L204)). The retry loop handles
  parse failures; first-attempt fidelity is below vLLM. Future change:
  thread a `response_format` override through `LLMClient` (benefits
  mlx-lm and llama-server equally).
- **`repetition_penalty` IS honored** (unlike mlx-lm). llama.cpp accepts
  `repeat_penalty` natively and the standard `frequency_penalty` /
  `presence_penalty` knobs. The runaway-loop escape hatch in
  [config.py:19-24](src/lnvox/config.py#L19-L24) keeps working on this
  path: with `LNVOX_LLM_BACKEND=llama` (or unset) the client sends the value
  under both spellings.
- **Thinking channel.** The Gemma 4 GGUF chat template enables thinking by
  default, and a structured call would spend its whole budget in the thought
  block. The client sends `chat_template_kwargs: {"enable_thinking": false}`
  whenever `LNVOX_LLM_BACKEND` is `llama` (or unset) or `mlx`, and falls
  back to `reasoning_content` when `content` comes back empty;
  `serve_llama.sh` runs `--jinja --reasoning-format auto`.
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
| `scripts/run_pipeline.sh`                  | New `LNVOX_LLM_BACKEND` env var + `--llm-backend {vllm,mlx,llama}` flag. Default: `llama` on every platform (since 2026-07-25; originally Darwin→`mlx`, Linux→`vllm`). `prepare_llm_env` and `start_vllm` dispatch on it; the backend is exported for the client. |
| `pyproject.toml`                           | **No change** — llama-server is a native binary, not a Python package.                                                                                                                                       |
| `src/lnvox/{llm,tts,stages,cli}/*`         | **No change** — `LLMClient` already speaks the OpenAI dialect llama-server provides. (Later: the thinking-channel switch and `repeat_penalty`, §14.4, keyed on `LNVOX_LLM_BACKEND`; `LLMConfig.model` defaults to the llama model.) |

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
  (Updated 2026-09-28: since the 2026-08-04 recipe flip, §2.6, the staged
  default caps chunks at 22 s (target 18 s) with a word-boundary fallback
  split, and its cache keys carry the recipe tag — so it shares entries with
  a monolithic run only when both render the same recipe. Item ids embed a
  hash of the chunk text (`{cache_key}_cNN-<sha8>`), so a chunk-policy change
  can't reuse intermediates rendered from different text (F12). P0 runs in
  the driver itself; only P1–P4 are subprocesses.)
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
  don't know staged exists). **Placement** (updated 2026-09-28): each beat's
  `05_audio/` WAV is copied from its cache entry unless it already is an
  exact `copy2` of the entry the manifest names — same size, mtime within
  2 s. "Exists → skip" left stale copies after a cache re-render (F13), and
  "cache is newer" missed reverting to an earlier render; the exact-copy test
  catches both and copies nothing on an unchanged re-run. (Placements made
  before this are re-copied once — no rendering.)

All writes are `tmp + os.replace` atomic; "done" = output file exists —
except for P4's outputs, which are done only when every placed WAV is an
exact copy of its cache entry and each chapter manifest lists exactly the
plan's beats and cache keys, with durations measured on the placed WAVs.

### 15.4 Crash isolation & resume

- The driver restarts a failed phase subprocess; completed items skip by
  file existence, so a crash costs one item's compute plus one
  single-model load (P3 restart ≈ DiT load only, vs the full four-model
  boot today). A phase counts as complete only when its pending count is
  zero after a clean exit. P4's count includes stale placements and
  manifests, and P4 always runs at least once — so a fully cached re-run
  still re-places and verifies, and a crash inside placement (every cache
  WAV already written) is retried instead of passing. (Updated 2026-09-28.)
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
  monolithic path. Staged is now the launcher's default (`LNVOX_S4_STAGED`,
  default 1); a staged s4 that exits 2 (a refused input) or fails is not
  retried by the launcher, which stops. (Updated 2026-09-28.)

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
| `src/lnvox/tts/staged_driver.py` (new) | Plan model, subprocess orchestration, attempts / stall-limit bookkeeping.                                                                    |
| `src/lnvox/cli.py`                | `lnvox s4 --staged` flag + hidden `lnvox s4-phase {refs,ctx,denoise,decode}` per-phase entry points (what the driver spawns; the plan is built in the driver). |
| `scripts/run_pipeline.sh`         | `--staged-tts` (or `LNVOX_S4_STAGED=1`) routes s4 through the driver and skips the outer retry loop for it. Now the default; `LNVOX_S4_STAGED=0` selects the monolithic path under `s4_retry.sh`. |
| `src/lnvox/stages/s4_tts.py`      | Untouched — monolithic path remains the default until staged is validated on a full book. (Validated since, incl. on MPS; the launcher defaults to staged, while a bare `lnvox s4` without `--staged` is still monolithic.) |

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
  (Updated 2026-09-28: validated end-to-end on an M4 Max in 2026-08, and
  now the *only* MPS path — monolithic is refused there, §11.3.)
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
- `--staged` + vibevoice is a **hard error** (launcher and CLI; the
  launcher's own staged default, §9, is simply not applied to VibeVoice): §15
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

**Cache**: key = `hash(script, ordered ref filenames, model version)`,
same `cache/tts/<key>.wav` pool as Dramabox (keys can't collide across
engines — the model version differs). The version includes the DDPM step
count (`vibevoice-large-cfg1.3-ddpm10-48k` at the default 10 steps), so a
render with other steps can't reuse default-step audio; s4 refuses a client
whose version doesn't match the one the keys were computed with. (Updated
2026-09-28.) Manifest entries carry
`scene_id`, `speaker` = the joined speaker list, `type` = `dialogue` if
any beat is dialogue else `narration`.

### 16.4 Client & generation

New `src/lnvox/tts/vibevoice_client.py`. Unlike Dramabox it's a real
package: env prep does `uv pip install -e external/VibeVoice`, no
`sys.path` games.

```python
class VibeVoiceClient:
    DEFAULT_PARAMS = {"cfg_scale": 1.3, "ddpm_steps": 10, "seed": 42}
    _MODEL_VERSION_FMT = "vibevoice-large-cfg1.3-ddpm{steps}-48k"
    MODEL_VERSION = _MODEL_VERSION_FMT.format(steps=10)   # default-steps key

    @classmethod
    def model_version_for(cls, ddpm_steps=None) -> str: ...
    def __init__(self, device="cuda", model_path=None, ddpm_steps=None): ...
    def generate_session(self, *, script, voice_refs, output_path,
                         seed=None, cfg_scale=None): ...
```

- Loading: `VibeVoiceProcessor.from_pretrained(model_path)` +
  `VibeVoiceForConditionalGenerationInference.from_pretrained(...)` with
  the §16.5 dtype/attention knobs, then `set_ddpm_inference_steps(steps)`
  (10 by default); the instance's `model_version` records the steps.
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

`_default_tts_device()` ([cli.py:719](src/lnvox/cli.py#L719)) is reused
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
| Ingest + structure | new (`ingest/scenario.py` + `scenario_structure.jinja`) | `00_script.json` — scenes of ordered items `{type: dialogue\|staging\|cue, speaker?, text}` + the characters roster (name, bio, actor) when the script has one; also `00_text.jsonl` (one chapter per scene) so s5 picks up scene titles for chapter markers. A script with no recognizable scene header is ONE scene (title `""`): everything but its cast list |
| Characters | s1 schemas reused | `01_characters.json` — roster is script-given; speaker-label variants (spacing/typos/parenthesized first names) are merged deterministically and an LLM pass fills gender/age/description gaps for casting (group labels are never cast) |
| Casting | `voice cast` **unchanged** | `04_voice_assignments.json`. Role-collapsing onto one actor's voice is done by hand in the Voicebank Studio's Casting tab — no `--by-actor` flag (decision 2026-07-24) |
| Direction | s3 machinery reused; new prompt | `03_voice_profiles.json` + `03_directed/*.json` — `DirectedBeat` per line, + new `emotion` field (the §16.7 7-enum), `direction` = short cue **in the script's language** (for humans/sync), Dramabox `prompt` composed in English as `(<descriptor> - <emotion>) "<text>"` (§2.5). `text` is the line with parenthetical acting cues stripped (`(haughtily) Sir!` → `Sir!`, unless the line is nothing else); `00_script.json` and the sync file keep the line verbatim |
| TTS | s4 **unchanged** (both engines, `--tts-backend`) | `05_audio/<scene>/…` |
| Mix | s5 **unchanged** | `06_final/<id>.m4b` with scene chapter markers |
| Sync | new emitter | `07_sync/<scene>.json` + `07_sync/play.json` (+ `.srt` export) |

**Ingest is content-cached** (same philosophy as the s4 beat cache,
§2.6): every structuring/roster/characters LLM call is cached under
`cache/scenario/` keyed on the hash of the schema name + *system prompt* +
*rendered prompt* + model — so a re-run or crash-resume only pays for scenes
whose text actually changed, and editing a prompt template or switching
models re-keys automatically (stale entries can't survive by construction).
`--no-cache` skips the reads to re-roll the structuring but still writes the
fresh results, so the next cached run returns the re-roll rather than the
result it replaced. (Updated 2026-09-28: the system prompt joined the key,
so entries from before miss once.) Every LLM pass runs before anything is
written; `00_text.jsonl` and `01_characters.json` are written next, and
`00_script.json` last and atomically — the launcher treats `00_script.json`
+ `01_characters.json` as "ingest done", so a crash can't leave a script
without its cast.

The direction pass is **casting-aware like s3** (§2.5): the voice profiles
follow the casting (only recast characters get a new profile;
`--regen-profiles` regenerates all), and a cached scene whose speakers'
descriptors changed is re-voiced — prompts rebuilt from the stored emotion,
no LLM call.

Mapping onto existing stage shapes: one `ChapterDirected` per script
scene/sequence; within it, one `DirectedScene` per staging-delimited
run of dialogue — so s5's existing inter-scene pad lands exactly where
a staged action happens, and becomes the "staging pause" with zero new
s5 code. One beat per spoken line; the s3 375-char split still applies
for TTS *rendering* (long monologues), with the beat's
`source_paragraph` field (already on the schema for lecture mode)
carrying the dialogue-line number so the sync emitter re-groups split
chunks into **one entry per script line**.

### 17.4 The sync file

Deterministic timing — the same cursor math s5/s6 already use (beat WAV
durations + the mix's pad values), not audio probing. The pads come from
the mix's `06_final/<title>.timings.json` (§2.7; newest unless `--title`):
s5 intra → between lines of a run, inter-scene → the staging pause,
inter-chapter → between script scenes, lead-in → per line; a flag only
overrides. The play covers exactly the scenes that mix contains, in its
order (an `s5 --chapters` mix starts its first scene at 0:00). Without a
sidecar the scenario constants apply (0.25 / 1.0 / 0.75 / 0.35 s) and every
scene is emitted. (Updated 2026-09-28; the pads used to be passed to both
stages by hand.) Example (invented content):

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

- **dialogue**: timed span of the line's rendered audio, *including its
  lead-in* (below): `start` marks the beginning of the lead-in silence,
  speech begins `lead_in` later, `end` is where speech ends.
- **staging** (didascalies): a configurable pause on the timeline
  (default = s5's inter-scene pad, 1.0 s; the action takes stage time)
  and an entry in the sync file; never spoken.
- **cue** (sound/light/other): zero-duration marker at its position —
  the régie timeline for free.
- **Between script scenes**: 0.75 s (`SCENARIO_INTER_SCENE`), not the
  2 s book default — user-tested 2026-07-24: 2 s reads right between
  novel chapters but drags between play scenes. The launcher passes the
  matching `--inter-chapter 0.75` to s5 in scenario mode; books keep 2 s.
- **Per-line lead-in**: 0.35 s (`SCENARIO_LEAD_IN`, s5 `--lead-in`)
  prepended to every beat in the scenario mix. The 2reply reader app
  polls playback position only every ~0.300 s on older devices; since a
  dialogue entry's `start` points at the lead-in (not at speech), the
  poll always fires while the line is still silent — the highlight can
  never arrive after the words. Especially needed post-trim (§2.6):
  trimmed beats start speaking immediately. Books keep 0.
- The m4b and the sync file are computed from the same plan and pad
  values, so they agree by construction (same stance as s6, §2.8).

### 17.5 French voicebank

The French Common Voice corpus now lives under `data/` alongside the
English one. Seeding stays the existing `voice seed-cv` flow pointed at
the French locale dir; the bank lands in a **separate voicebank
directory** so English and French casts don't mix. Plumbing:
`_voicebank_dir()` honors a new `LNVOX_VOICEBANK` env var (default
`voicebank`), read by `voice *`, `s4`, and the Studio (which already
takes `--voicebank`). A French project sets `LNVOX_VOICEBANK=voicebank-fr`
once; no per-command flags. (Updated 2026-09-28: it is a `Settings` field
(`Settings().voicebank`), so a `.env` entry works too — for every `lnvox`
command that reads the bank (`voice *`, s3, `scenario`, s4) and for the
Studio's `--voicebank` default. The launcher itself reads only the process
environment.)

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

---

## 18. Chapter splitting inside a spine page (`--split-chapters`)

### 18.1 The problem

Stage 0a assumes **one spine item ≈ one chapter** (§2, `_chapter_base`
merges `chapter1_1` into `chapter1`). That holds for publisher EPUBs and for
the Standard-Ebooks-style Gutenberg conversions, but not for Gutenberg's
`Ebookmaker` output, which packs an entire book into a handful of XHTML
files. Measured on the illustrated editions:

| EPUB | spine XHTML | chapters ingested | largest chapter |
|---|---|---|---|
| Gutenberg #45506 (Flatland, illustrated) | 5 | 2 | 24,498 words |
| Gutenberg #43936 (Oz, illustrated) | 7 | 4 | 17,811 words |
| Gutenberg #52521 (Grimm) | 57 | 55 | fine as-is |
| Gutenberg #76391 (Fairy Dreams) | 11 | 9 | fine as-is |

A 24k-word "chapter" is not a correctness failure — s1/s2 chunk internally —
but it destroys the two things the m4b and ln-reader are *for*: chapter
markers and chapter-relative scrubbing. A 4.7 h audiobook with two markers is
not navigable.

### 18.2 Why not the TOC

The obvious source of boundaries — `toc.ncx` / `nav.xhtml` fragment links
into the spine page — is unreliable on exactly these files:

- #43936 declares **4** TOC entries for a 24-chapter book.
- #52521 and #76391 declare 40+ anchors on a single page, nearly all of them
  *illustration* and *page-number* anchors, not chapters.

Splitting on TOC anchors would under-split one book and shatter another. The
TOC is descriptive of the reading order, not of chapter structure.

### 18.3 The rule

Split a spine page at **chapter-start markers**, defined as either:

1. a heading element (`<h1>`–`<h3>`), or
2. any element whose `id` matches `^(?:chap|chapter)[_\-]?[0-9ivxlcdm]+$`
   (case-insensitive) — Gutenberg's `id="Chapter_XI"` convention, used by
   illustrated editions whose chapter titles are *images* and therefore carry
   no heading text at all.

A marker nested inside another marker is the same chapter start, not a
second one: Gutenberg writes `<h2><a id="chap01"/>CHAPTER I.</h2>`, which
used to count twice (an empty chapter, then the real text under a bogus
title). The outer element is kept.

A page splits only when it holds **≥ 2** such markers; text before the first
marker opens the page's first chapter. Each resulting section becomes its
own chapter part (a section with no prose — a heading alone — produces
none), titled from the marker's text; a marker without text falls back to
the text of a heading it wraps, then to the `alt`/`title` of the first `<img>`
after it (the illustrated case — the chapter-head image follows its anchor),
and then to the anchor id. (Updated 2026-09-28: nested markers, the wrapped
heading, and the image *after* the marker rather than before it.)

### 18.4 Opt-in, not automatic

The flag is **off by default**. Auto-detection was rejected: the rule is a
heuristic, and eight series already in `novels/` were ingested under the
one-page-one-chapter assumption. A Standard Ebooks page with three `<h3>`
subheadings would silently re-chapter a book that is already correct, and
re-chaptering changes `00_text.jsonl` chapter ids — which invalidates every
downstream artifact, including prior-volume voice continuity (§6). Opt-in
keeps every existing book byte-identical.

    lnvox ingest-epub <epub> <out> --split-chapters

### 18.5 What it must not break

- `.epub_meta.json` keeps a `source_parts` entry per chapter, now carrying
  the anchor id alongside the XHTML stem (`43936-h-2#Chapter_XI`), so Stage 6
  can still re-align a beat to the original markup. Only a page that was
  actually split gets `stem#<marker id>` keys (`stem#secNNN` for a marker
  with no id); a page with fewer than two markers keeps its bare stem and
  the `_chapter_base` multi-part grouping. Because nested markers now count
  once (§18.3), a page such as `<section id="chapter01"><h1>…` no longer
  splits — its part changes from `chapter01#sec001` to `chapter01` — so
  re-extracting such a book with `--split-chapters` can regroup and
  renumber its chapters, with the downstream cost §18.4 describes.
  (Updated 2026-09-28.)
- Image extraction and `meta.images` ordering are untouched — they walk the
  spine independently of chapter grouping.
- Lecture mode is unaffected in this revision (its block classifier already
  drops and restructures pages; splitting interacts with `visual_elements`
  placement and is deferred until a non-fiction book actually needs it).

---

## 19. Quality-ranked voicebank seeding (`--rank-by-quality`)

### 19.1 The problem

`seed_from_common_voice` iterates `sorted(by_speaker)` and stops at
`max_speakers`. The sort is over Common Voice `client_id` hashes, so the bank
is an arbitrary — effectively random — slice of the eligible pool, and
nothing in the selection looks at how the recording *sounds*. Measured on the
400-speaker US+England bank seeded 2026-08-30, against 9,782 eligible
speakers:

| | count |
|---|---|
| effective bandwidth < 8 kHz (phone / heavily compressed capture) | 274 / 400 |
| SNR < 30 dB against real room tone | 123 / 400 |
| both wide-band (≥ 9 kHz) and clean (≥ 35 dB) | **52 / 400** |

87% of the bank is material that clones muffled or noisy. This is not only a
narrator problem: a 55-tale Grimm and a 24-chapter Oz cast dozens of
supporting characters out of the same bank.

Manifest metadata (`accent`, `gender`, `age_band`) says nothing about any of
this, and neither does `duration_seconds` — ranking the bank by clip length
produced a candidate set whose median quality rank was 182/400, where random
is 200.

### 19.2 What is measured

Per candidate, on one representative utterance (highest net votes):

- **Effective bandwidth** — highest FFT bin still within 40 dB of the
  spectral peak, measured over *loud* frames only. Separates full-band
  capture from 8 kHz phone/codec capture. The dominant term.
- **SNR** — 75th-percentile frame level minus 5th-percentile, both in dB
  relative to peak.
- **Rumble** — energy below 80 Hz as a share of total (hum, handling noise).
- **Hiss** — high-band share of the energy in *room-tone* frames.
- **Clipping** — fraction of samples at ≥ 98% of peak.

**Floor estimates must exclude digital silence.** `build_speaker_clip`
joins utterances with 0.25 s of true zeros; measured naively, those frames
become the "noise floor" and every clip scores 90+ dB SNR. Every percentile
here is taken over frames more than 90 dB below peak only. This bit the
first implementation and is the single easiest thing to get wrong.

### 19.3 Two passes

Building a reference clip is expensive (decode + trim + concatenate ~12 s of
audio per speaker); probing one utterance is not. So:

1. **Probe** up to `max_candidates` speakers (default `max_speakers × 6`),
   decoding a single utterance each, and score it.
2. **Build** full reference clips only for the winners.

Cost scales with the probe, not the pool: ~2,500 probes ≈ 10 min, against
~25 min to build 400 clips.

### 19.4 Bucket quotas

Ranking on quality alone would inherit — and sharpen — Common Voice's
male/young-adult skew, because that majority also supplies most of the good
recordings. The casting matcher selects on `(gender, age_band)`, so a bank
with four clean female elders casts every witch, godmother and queen in
Grimm to the same voice.

Quota per bucket is therefore **proportional to the candidate pool, with a
floor of `max_speakers // 20`** (5%, so 20 of 400) wherever the pool can fill
it. Within each bucket, highest score wins. Buckets whose pool is smaller
than the floor contribute everything they have.

### 19.5 Opt-in

Off by default: it changes which speakers a given `--max-speakers` selects,
so an existing bank would silently re-populate and every casting made
against it would drift. Existing banks are unaffected until re-seeded.

    lnvox voice seed-cv <cv-root> --max-speakers 400 \
        --accent us --accent england --rank-by-quality

Scoring weights live in `quality_score()` and are deliberately blunt —
bandwidth and SNR carry the decision. The score predicts *recording*
quality, not whether someone reads like a narrator; picking among the clean
survivors is still done by ear.

---

## 20. Casting audit (`lnvox voice audit`)

### 20.1 The problem

The voice matcher (§6) selects per character, on demographics: gender, age
band, accent, timbre keywords. It never asks whether two characters who
**talk to each other** ended up sounding the same. Nothing downstream asks
either, so the first time anyone finds out is by listening to the finished
m4b.

Observed on the first demo run, all three found only by hand:

| Book | Pair | Separation | Consequence |
|---|---|---|---|
| Flatland | Narrator / The Sphere | **0.6 semitones** | the book's central 88-beat dialogue in two indistinguishable voices |
| Flatland | The Square / The Monarch | same clip | the Lineland two-hander in one voice |
| Fairy Dreams | Brigitta / Fritzchen | 0.4 semitones | — |
| Fairy Dreams | Father Franz / Fredrich von Rosenberg | 0.8 semitones | — |

Flatland's two cost a re-render because they were caught after s4. Fairy
Dreams' three were caught before s4 and cost nothing — which is the whole
argument for making this a stage rather than a habit.

### 20.2 What it measures

Two characters are a problem when they **share scenes** and their voices are
**not separable**:

- **Co-occurrence weight** — for each scene, `min(beats_a, beats_b)` summed
  over the book. This is deliberately not "appears in the same book": a pair
  that never exchanges lines can share a voice harmlessly, and a book with 46
  characters has far too many incidental pairs to look at. Pairs below
  `--min-shared` (default 3) are ignored. Narration beats count as the
  Narrator's whatever their `speaker`: on a first run the audit reads
  `02_scenes/` (it prefers `03_directed/` once that exists), where narration
  has `speaker: null`, and without this the Narrator paired with nobody until
  the casting was already baked into s3's descriptors. (Updated 2026-09-28.)
- **Separation** — absolute distance in semitones between the two assigned
  clips' median f0. Identical clips are separation zero and always flagged.

Pitch is a crude proxy for "sounds different" — it says nothing about timbre
or delivery — but it is cheap, and it reliably catches the failure that
actually matters: two same-gender, same-age-band clips landing within a
semitone of each other.

### 20.3 Acoustic index

Clip metrics live in `<voicebank>/acoustics.json`, keyed by clip id, built
lazily and incrementally: a bank seeded before this existed gets indexed on
first audit, and re-seeding only pays for new clips. It is a cache, not a
source of truth — deleting it costs one rebuild.

### 20.4 Repair

`--fix` reassigns the **lower-speaking** character of a flagged pair: the
re-render is cheaper, and the dominant voice is the one the listener has
already learned. A replacement must be unused in this book, match the
original's gender, clear `--min-semitones` against *every* scene partner of
the character being moved (not just the one flagged — a partner being anyone
it shares at least `--min-shared` weight with, the detection threshold), and
meet a quality floor. The search degrades in the order that costs least:
the exact age band at the full floor (bandwidth ≥ 10 kHz, SNR ≥ 38 dB), then
an adjacent age band at the same floor, then an adjacent band at a relaxed
floor (≥ 9 kHz, ≥ 32 dB) — a slightly duller voice the listener can tell
apart beats a pristine one they can't. The best-scoring candidate wins. A
pair an earlier repair already separated is skipped, and a character is
never sent back to a clip it left in the same run. The moved entry gets an
`audit_note` and an **empty `voice_descriptor`** — a stored one describes the
old clip and s3 would use it verbatim — and stays unlocked. (Updated
2026-09-28: adjacent bands and the relaxed floor are new; the replacement
used to have to match the age band exactly.)

Two things are never auto-recast:

- **The Narrator**, whose clip is a deliberate `--narrator-clip` choice.
- Any casting with **`locked: true`** — set by hand when an assignment is
  intentional. Flatland needs this: "The Square" is the narrator's own
  pseudonym, so sharing the narrator's clip is correct, and an unguarded
  auto-fix would helpfully break it. The Studio's Casting tab sets the lock
  on every reassignment (§12.1), and `voice cast` keeps locked entries too
  (§6.4), so a hand fix survives the launcher re-running cast and audit.

When no candidate satisfies every constraint the pair is reported unfixed
rather than being moved to something marginal. Widening the age band beyond
its neighbours or lowering `--min-semitones` is a judgment call, so it stays
manual.

### 20.5 Placement

Runs after Stage V (voice cast) and before Stage 3, where `02_scenes` already
supplies co-occurrence (narration counted as the Narrator's, §20.2) and a
casting change is still free. The launcher runs it in report-only mode by
default; `--audit-cast fix` opts into repair, `--audit-cast off` skips it,
and `--stop-after-audit` stops the run there so the casting can be fixed by
hand (in the Studio, which locks the edit) before continuing.

---

## 21. Synced-EPUB output defects (reader-reported, 2026-08-31)

Two bugs the pipeline could not see, because both are only visible in the
reader. Stage 6 reported success in each case.

### 21.1 Beat spans lost when chapters share a spine file

`--split-chapters` (§18) puts several chapters in one XHTML. Stage 6 built its
soup by re-reading that file from the zip **per chapter** and assigning
`modifications[arcname]`, so each chapter overwrote the previous one's wraps
and only the last survived:

| Book | chapters | unique XHTML | spans kept | matched beats |
|---|---|---|---|---|
| Oz | 27 | 4 | 102 | 1396 |
| Flatland | 28 | 2 | 23 | 687 |
| Alice | 18 | 18 | 1056 | 1192 |
| Fairy Dreams | 9 | 9 | 637 | 667 |

The correlation is exact: only books whose chapters share a file were hit,
which is why the one-chapter-per-file books hid it through the whole demo run.
The manifest was always complete — the loss was purely in the EPUB — so
auto-follow had timings for beats whose spans did not exist.

Fix: one soup per XHTML, cached across chapters, so wraps accumulate into a
single DOM. Chapters cover disjoint regions of a shared file, so accumulation
is safe. Guarded by `tests/test_s6_split_chapters.py`, which fails on the old
code.

### 21.2 Horizontal overflow on fixed-pixel illustrations

Gutenberg's Ebookmaker sizes images absolutely (`style="width: 600px"`) and
ships no `max-width` rule. In the illustrated editions the chapter headings
*are* images, so they clipped at the right edge and the page scrolled
sideways. Measured: Oz had 327 pixel-sized elements (146 ≥ 300px) and 0
max-width rules; Flatland, Fairy Dreams and Alice ship their own responsive
CSS and were unaffected.

Stage 6 now injects a containment style into **every** XHTML of the synced
EPUB, including pages carrying no beats — an illustration page that scrolls
sideways is as broken as a chapter that does:

    img,svg{max-width:100%!important;height:auto!important}
    table,pre{max-width:100%;overflow-x:auto}
    body{overflow-wrap:break-word}

`!important` is required because an inline `style` attribute otherwise wins.
`height:auto` must accompany it, or capping width against a fixed pixel height
distorts every illustration. Injection is idempotent (marked with
`data-lnvox`), skips pages with no `<head>`, and leaves non-XHTML entries —
including the STORED `mimetype` that must stay first — byte-identical.

### 21.3 Why neither was caught

Stage 6's success metric is *beats matched*, computed against the shadow text.
Both bugs happen after matching: one when writing the DOM back, one in how a
reader lays the result out. A span count taken from the written EPUB, compared
against the manifest, would have caught 21.1 immediately — worth adding to the
stage's own summary output.

---

## 22. Demo samples (`lnvox sample`)

### 22.1 Purpose

A finished book is 150–220 MB of audio. To let someone try ln-reader from a
public repo we need a slice small enough to commit that still exercises every
reader feature: **text-highlight auto-follow**, **chapter-relative scrubbing**,
**the chapter transition**, and **inline images**.

### 22.2 What a sample must contain

Those four features constrain the slice more than duration does:

- Auto-follow needs beats whose `data-beat-id` spans exist in the EPUB, with
  timings rebased so the sample's audio starts at 0.
- Scrubbing *relative to a chapter* needs **two** chapters, not one.
- The chapter-end transition needs the **boundary between them** inside the
  window — not merely two chapters listed in the metadata.
- Images need visual elements whose `trigger_seconds` fall in the window.

So the window straddles a chapter boundary: the tail of one chapter and the
head of the next. Taking two whole chapters would be correct but far too big —
the best-scoring pairs run 22–63 minutes, and Oz's would be ~45 MB on its own.

### 22.3 Choosing the chapters

Scored per consecutive pair as `3 × distinct speakers + min(images, 40)`,
which favours multi-voice dialogue while still rewarding illustration. On the
demo books this picks, without any hand-tuning:

| Book | Pair | Speakers | Images |
|---|---|---|---|
| Oz | XI + XII — Emerald City / Search for the Wicked Witch | 14 | 124 |
| Alice | VI + VII — Pig and Pepper / **A Mad Tea-Party** | 15 | 6 |
| Flatland | §14 + §15 — the Sphere's arrival | 6 | 16 |
| Fairy Dreams | Under the Sea / The Castle on the Cliff | 14 | 6 |

### 22.4 What ships, and what does not

- **Audio** is `-c copy` sliced — no re-encode, so the sample is the real
  render at full quality, and cutting costs a second rather than an hour.
  `06_final/` can hold several mixes, so the source is chosen in order: the
  m4b named by `--title` (if it exists), the one Stage 6 timed the manifest
  against (its `m4b` key), the newest mix's (by its timings sidecar, §2.7),
  then the newest m4b; a pick other than the manifest's own is warned about.
  (Updated 2026-09-28; the first glob hit used to be sliced — often a stale
  mix.)
- **Chapter markers** are rewritten via ffmetadata, rebased to the slice.
- **The manifest** keeps only beats and visual elements inside the window,
  with every timestamp rebased.
- **The EPUB ships whole.** It is the small half (0.4–11 MB against 150–220 MB
  of audio), and trimming it would mean rewriting the OPF spine and nav for no
  real saving. The reader shows the complete book; only the audio is a sample.
  Beats outside the window simply have no timing, which is what a sample is.

---

## 23. Embedded pictures and the manifest `images` contract

### 23.1 The contract

ln-reader (`ln-reader/DESIGN.md`, "images[]") pairs `sync_manifest.images[i]`
with the **i-th picture in the m4b's `covr` atom** — by array position,
because MP4 stores attached pictures without per-picture names. The first
picture is the cover. Every scrubber marker in the image viewer is
`images[i].trigger_seconds` pointing at embedded picture `i`.

ln-vox never honoured it. Stage 5 embeds one picture per unique image file,
cover first; Stage 6 wrote one entry per `<img>` **occurrence** in the EPUB,
in walk order. Measured on the four demo books, before this change:

| Book | embedded | manifest `images` | at the right position |
|---|---|---|---|
| Oz | 148 | 1,453 | 0 |
| Alice | 45 | 45 | 0 |
| Fairy Dreams | 19 | 19 | 0 |
| Flatland | 12 | 200 | 0 |

Even where the counts matched, the manifest did not start with the cover, so
every marker showed the neighbouring picture. The demo samples then lost the
pictures entirely (23.4), which is how it surfaced.

### 23.2 Pictures on a shared page belong to one chapter

`--split-chapters` (§18) puts many chapters in one XHTML, and each chapter
claimed every picture on it — the same "chapter owns the whole file" mistake
as §21.1. Each copy snapped to that chapter's first beat, which is why Oz's
148 pictures appeared 1,453 times, most of them at the wrong chapter start.

Each `<img>` is now attributed to the section that holds it, using the ingest
split's own marker rule and key format (`chapter_markers` / `section_key` in
`ingest/epub.py`, shared rather than re-derived): preamble joins the first
section, and a heading-only section — no prose, so no chapter — hands its
pictures to the chapter that heading introduces. Result: Oz 148 occurrences
for 148 pictures, Flatland 12 for 12.

### 23.3 Alignment

- `s5_mix.resolve_book_images` + `embedded_image_order` are the single
  definition of what an m4b embeds and in what order; verified against the
  existing renders stream by stream (picture dimensions, then byte hashes of
  the `covr` payloads). (Updated 2026-09-28: Stage 5 now records the result
  as `embedded_images` in its timings sidecar (§2.7), `--cover` /
  `--images-dir` overrides included, and Stage 6 aligns to that recorded
  list; it re-resolves — without overrides — only for a sidecar that
  predates the field, or with no mix at all. `lnvox sample` works from the
  manifest's `images`, which are in that order.)
- Stage 6 writes `images` as **exactly one entry per embedded picture**, in
  that order, taking each picture's first timed occurrence. A picture no beat
  reaches keeps its slot with `trigger_seconds: null` — dropping it would
  shift every later picture by one. The per-occurrence list is unchanged in
  `visual_elements`.
- A **tailpiece** — after its chapter's last matched beat — used to get no
  time, which the reader places at 0:00: the start of the *book*. It now
  triggers as that last beat ends. Eight of Oz's chapter-end illustrations
  and five of Fairy Dreams' were affected.

After: every picture byte-identical at its position in all four books, and
every picture timed except Alice's cover.

### 23.4 Samples dropped every picture

An attached picture is a single packet at pts 0. The sample cut used an
output seek (`-ss` after `-i`), which discards everything before the cut —
including all pictures, cover included. The cut is now two `-c copy` passes:
an exact audio-only slice, then a mux of that audio with the chosen pictures
read from an **unseeked** copy of the source. A sample embeds the cover plus
the pictures whose playback time falls in its window, and its `images` array
mirrors exactly those, in the same order.

### 23.5 Choosing sample chapters once the data was right

With the phantom duplicates gone, Flatland — 12 pictures in the whole book —
has no 12-minute window holding three. The old fallback then maximised
pictures outright and chose a two-voice window over the six-voice arrival of
the Sphere. Selection is now tiered: pairs whose window has ≥ 3 pictures,
else ≥ 1, else any — ranked by distinct voices within the tier.

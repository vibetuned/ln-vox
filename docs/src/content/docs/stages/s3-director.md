---
title: Stage 3 — director
description: lnvox s3 writes clip-anchored voice profiles and per-scene performance cues, and composes the Dramabox prompt for every beat into 03_directed/.
sidebar:
  order: 8
  label: 3 · director
---

Stage 3 turns s2's scenes into the directed beats that s4 renders. First it writes a **voice descriptor** for every character, consistent with the reference clip [voice cast](/stages/voice-cast/) assigned them. Then, one LLM call per scene, it adds a short performance cue to every dialogue line. Finally it composes each beat's Dramabox prompt, `(<descriptor - cue>) "<text>"`. Along the way it merges short same-speaker beats and splits anything over 375 characters. It runs only in narration mode. Lecture and scenario mode write the same `03_directed/` files with their own stages ([lecture](/stages/lecture/), [scenario](/stages/scenario/)).

## Command

```bash
uv run lnvox s3 <book_id> [--chapters 01,02] [--regen-profiles]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `BOOK_ID` (argument) | required | Book id. Reads and writes `artifacts/<book_id>/` |
| `--chapters` | all chapters | Comma-separated chapter ids. It filters the `02_scenes/*.json` files by `chapter_id` |
| `--regen-profiles` | off | Force-regenerate **every** voice profile. Not needed after a recast: recast characters' profiles regenerate on their own. Every chapter whose descriptors change is re-voiced, so s4 re-renders it |

The command exits 1 if `01_characters.json`, `02_scenes/` or `04_voice_assignments.json` is missing. The last one prints `Run lnvox voice cast <book> first`: casting must come before s3. The voicebank is loaded from `$LNVOX_VOICEBANK` (default `voicebank`). If its `manifest.json` is missing, the voicebank is empty and descriptors are silently left un-anchored to clips.

## In the launcher

```bash title="scripts/run_pipeline.sh"
    if [ "$MODE" = "narration" ]; then
        banner "Stage 3: director (uses voice cast metadata)"
        run_step ensure_vllm "Stage 3 (director)" uv run lnvox s3 "$BOOK_ID"
```

It runs in narration mode after Stage V (voice cast), the casting audit (Stage A) and the `--stop-after-audit` checkpoint. It is skipped with `--skip-llm`. `run_step ensure_vllm` gives the step up to `--step-retries` attempts (default 3), and relaunches a dead managed LLM server before each retry.

### Casting changes between runs

Profiles are derived from the casting. Every profile's `gender`, `age_band` and `accent` are snapped to the assigned clip. A character whose casting entry carries a `voice_descriptor`, such as the Narrator from `--narrator-clip`, auto-cast or prior-volume reuse, gets that descriptor verbatim. On every launcher run the casting can change right before s3:

- `voice cast` runs again;
- `voice audit --fix` may reassign clips;
- you may have edited `04_voice_assignments.json` by hand during a `--stop-after-audit` pause.

s3's caches follow the casting, so the launcher runs it without flags:

- Each profile records the clip and locked descriptor it was written for. A profile is reused while both still match `04_voice_assignments.json`. Otherwise only that character's profile is regenerated.
- Each `03_directed/` chapter records the descriptor of every speaker in it. When a speaker's descriptor changed, the chapter's prompts are **re-voiced**: the new descriptor replaces the old one and the performance cues are kept, with no LLM call. s4 then re-renders only those beats, which it would anyway for the new clip.
- A retry reuses the profiles written by the failed attempt, so chapters directed before and after a failure share the same descriptors.

A recast after s3 therefore needs no manual cleanup: re-run the launcher (or `lnvox s3`). `--regen-profiles` is the explicit "rewrite every descriptor" switch. It changes every chapter's prompts, so s4 re-renders the whole book.

:::note[Books directed before these records existed]
A chapter file without a record is treated as current, so upgrading doesn't re-pay the LLM for a whole book. It is dated from the profiles file as it was on disk before the run, the one the latest chapters were written with, so a character recast since then is still re-voiced. A legacy profile is reused unless its snapped gender, age band or accent (or its locked descriptor) contradicts the character's current clip.
:::

`lnvox voice cast` also reads an existing `03_voice_profiles.json` as a hint, for Narrator demographics and target inference. So a previous run's profiles influence the next casting before s3 refreshes them.

## Inputs and outputs

| Path | Direction | Contents |
| --- | --- | --- |
| `artifacts/<book_id>/01_characters.json` | read | Cast. Descriptions feed the profile LLM, names and aliases feed speaker canonicalisation |
| `artifacts/<book_id>/02_scenes/*.json` | read | Scenes and beats from s2, sorted by filename |
| `artifacts/<book_id>/04_voice_assignments.json` | read | `BookCasting`: assigned clip ids and locked `voice_descriptor`s |
| `$LNVOX_VOICEBANK/manifest.json` | read | Clip metadata (gender, age band, accent) |
| `artifacts/<book_id>/03_voice_profiles.json` | read/write (cache) | One `VoiceProfile` per character, plus `Narrator`, each recording the clip and locked descriptor it was written for |
| `artifacts/<book_id>/03_directed/<chapter_id>.json` | read/write (cache) | `ChapterDirected`, the s4 input, recording the descriptor of each speaker (`voice_descriptors`) |

JSON examples are on the [library page](/library/stages/s3-director/#artifacts).

## How it works

`cli.stage3` → [`s3_director.run`](/library/stages/s3-director/#run) → [`canonicalize_scene_speakers`](/library/stages/s3-director/#canonicalize_scene_speakers) → [`refresh_voice_profiles`](/library/stages/s3-director/#refresh_voice_profiles) → [`direct_chapter`](/library/stages/s3-director/#direct_chapter) → [`direct_scene`](/library/stages/s3-director/#direct_scene) → [`_merge_same_speaker`](/library/stages/s3-director/#_merge_same_speaker) → [`format_prompt`](/library/stages/s3-director/#format_prompt).

1. **Canonicalise speakers.** s2 tags use whatever the prose calls someone. Tags are folded onto cast names, ignoring case, whitespace and a leading `the`/`a`/`an`, and matching aliases too. `the narrator` becomes `Narrator`. s4 finds reference clips by exact cast name, so an unfolded tag like `The Square`, for a cast entry `A SQUARE`, would render without the character's voice. Unresolvable tags are kept as-is.
2. **Voice profiles** (one LLM call for the characters that are new or recast since the cached profiles; none if every one is current or locked). Each character is sent with their s1 description and the assigned clip's `{id, gender, age_band, accent}`, and asked for `"<age band> <accent adjective> <gender>, <pitch/timbre>, <habitual manner>"`. Gender, age band and accent are then overwritten with the clip's values. Placeholder answers (`n/a`, `unknown`, …) are replaced with a clip-derived descriptor.
3. **Per scene:**
   - **Merge.** Adjacent narration beats fuse. Adjacent dialogue from the same named speaker fuses, but `Unknown` lines never do. Merging stops at 375 characters combined. Any beat still over 375 characters is split at sentence ends (`.`/`!`/`?` then whitespace), and `source_span` is split in parallel.
   - **Cue call** (one per scene with dialogue). The model reads the scene in order and returns a 2–8-word cue per numbered dialogue line (`"soft, faintly amused"`, `"low and dangerous"`).
   - **Compose.** Narration gets `speaker = "Narrator"` and `direction =` the Narrator descriptor, with no cue. Dialogue gets `direction = "<descriptor>, <cue>"`. `prompt = format_prompt(direction, text)`: the direction is lowercased, commas become ` - `, and the result is parenthesised, then followed by the quoted text.

The 375-character cap (`MAX_MERGED_BEAT_CHARS`, about 30 s of audio) keeps Dramabox within its quality range. Past that, noise rises and speech can slur. The split is also a memory guard: inputs of about 3000 characters make Dramabox allocate more than 24 GB of VRAM.

## Debugging directions

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| A character's lines render in a generic voice | Their speaker tag didn't resolve to a cast name. The prompt contains `voice unknown` and s4 finds no reference clip | `grep -l "voice unknown" artifacts/<book>/03_directed/*.json`. Add the spelling as an alias in s1's per-chapter cache, or fix the tag in `02_scenes` |
| Descriptor contradicts the voice (says "male" over a female clip) | The LLM's descriptor text drifted. Only the structured fields are snapped, not the words | Set `voice_descriptor` on that character's entry in `04_voice_assignments.json` and re-run s3. It uses the descriptor verbatim, as long as the clip is in the voicebank, and re-voices that character's chapters. Voice cast keeps it on later runs because it reuses existing assignments, unless you pass `--fresh` or a prior volume's assignment for the character overrides it |
| Dialogue in a chapter has no cues | The cue answer for that scene was unusable (bad or unparseable JSON), which is swallowed | Delete that chapter's `03_directed` file and re-run |
| A single beat is far over 375 characters | No `.`/`!`/`?` + whitespace boundary. `…said." Then` isn't one, because the quote comes before the space | Expected for run-on sentences and long quoted monologues |
| Direction text read aloud by Dramabox | Prompt not in the parenthesised format, for example hand-written | Always build prompts with `format_prompt` |

## Re-running and caching

- `03_voice_profiles.json`: a profile is reused while the clip and locked descriptor it was written for match the casting. Recast or new characters are regenerated, one LLM call for all of them. `--regen-profiles` regenerates every profile.
- `03_directed/<chapter_id>.json`: reused while its recorded speaker descriptors match the current profiles. On a mismatch its prompts are re-voiced without an LLM call, or, if the chapter's speakers changed (s2 re-run), it is re-directed. A file that doesn't parse is re-directed. To force fresh cues, delete the chapter file.
- Changed `prompt` strings re-key the s4 TTS cache for those beats (`cache/tts/<sha256>.wav`). Expect re-renders for exactly the re-voiced beats.

## Failure modes

- **Server down or dying mid-stage.** Profile generation and cue calls both re-raise transport errors (connection refused or reset, timeout, 5xx), so the step fails and `run_step ensure_vllm` relaunches the server before the retry. Chapters finished before the crash are cached and reused.
- **Unusable cue answer.** A bad or unparseable response costs that scene its cues: its dialogue gets descriptor-only directions, cached like any other chapter.
- **Truncated profile list.** It is salvaged entry by entry. Characters that didn't make it get no profile, which means `voice unknown` for their dialogue, until the next run: a cast member without a profile is asked for again, and their chapters are re-voiced.

Design notes: DESIGN.md §2.5.

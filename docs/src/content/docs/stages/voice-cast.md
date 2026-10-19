---
title: Stage V — voice cast
description: lnvox voice cast assigns every character and the Narrator a voicebank clip, reusing prior-volume voices, and writes 04_voice_assignments.json.
sidebar:
  order: 6
  label: V · voice cast
---

Stage V picks a reference clip from the voicebank for every character in `01_characters.json` and for the Narrator, and writes `artifacts/<book_id>/04_voice_assignments.json`. It runs **before** Stage 3 so the director can write descriptors that match the actual reference voice ("elder Scottish male") instead of fighting it during cloning (DESIGN.md §2.4). In a series, voices already established in earlier volumes are reused without an LLM call, so a character keeps their voice across the whole series.

## Command

```bash
uv run lnvox voice cast <book_id> [--top-n 3] [--narrator-clip <clip_id>] [--fresh] [--seed <text>]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` | required | `<series>/<volume-NN>` or a flat id. |
| `--top-n` | `3` | Number of candidates the ranking LLM returns per character. The first that no other character has is assigned (else the first); all are kept in `ranked` for hand review. |
| `--narrator-clip` | none | Voicebank clip id for the Narrator. Overrides both the auto-cast and any prior-volume Narrator, but not a Narrator marked `"locked": true` in this volume's `04_voice_assignments.json` (a warning says the flag was ignored). Checked against the bank before anything is cast. |
| `--fresh` | off | Drop this volume's **unlocked** existing assignments: characters without a prior-volume match are re-cast by the LLM instead of keeping their clip. Locked entries are still kept, and prior-volume reuse still applies. |
| `--seed <text>` | `$LNVOX_CAST_SEED`, else none | Varies the order in which a fresh cast sees the voicebank — another draw of voices. Without it the order is fixed per book and character, so a re-cast repeats itself. Use with `--fresh` to re-roll a volume's first appearances. |

Environment: `LNVOX_VOICEBANK` selects the bank (default `voicebank`), `LNVOX_ARTIFACTS_DIR` the artifacts root, and the `LNVOX_LLM_*` variables the LLM endpoint (see [environment](/reference/environment/)).

### Exit codes

| Code | When |
|---|---|
| `0` | Casting written. |
| `1` | The voicebank is empty ("Run `lnvox voice seed-cv` first") or `01_characters.json` is missing. |
| `2` | `--narrator-clip` names an id that is not in the bank. Checked first, before the LLM client is even created, so nothing is cast or written; the message suggests close ids ("Did you mean: …?"). A deterministic error: retrying the same command cannot fix it. |
| non-zero | LLM failures (`structured` raising) propagate. |

## In the launcher

Stage V runs in every mode (narration, lecture, scenario) inside the LLM phase, right after s2 (narration) or ingest (lecture, scenario):

```bash title="scripts/run_pipeline.sh"
    banner "Stage V: voice cast"
    # Narrator handling:
    #   - --narrator-clip given           → use it (overrides any prior).
    #   - Not given AND prior volume      → matcher inherits prior Narrator clip.
    #   - Not given AND no prior volume   → matcher auto-casts the Narrator.
    if [ -n "$NARRATOR_CLIP" ]; then
        run_step "$VC_HOOK" "Stage V (voice cast)" uv run lnvox voice cast "$BOOK_ID" --narrator-clip "$NARRATOR_CLIP"
    else
        run_step "$VC_HOOK" "Stage V (voice cast)" uv run lnvox voice cast "$BOOK_ID"
    fi
```

- `--narrator-clip <id>` on the launcher is passed straight through. It is optional when a prior volume already has a Narrator.
- The retry hook is `$VC_HOOK`: `ensure_vllm` normally, so a crashed LLM server is relaunched before each retry (up to `--step-retries`, default 3). It is `-` (no hook) only in lecture mode with `--no-normalize` **and** `--narrator-clip`, where no LLM server is started, because the Narrator-only cast with an override makes zero LLM calls.
- The whole stage is skipped with `--skip-llm`.
- The launcher does not pass `--fresh` or `--top-n`, so every launcher run reuses this volume's existing assignments. Resuming after `--stop-after-audit` re-runs Stage V: entries you locked at the checkpoint come through unchanged, even when the resumed command still carries `--narrator-clip` (see [the checkpoint](/stages/voice-audit/#hand-casting-checkpoint)).

Stage A (the [casting audit](/stages/voice-audit/)) follows immediately. See [run_pipeline.sh](/launcher/run-pipeline/) and [launcher functions](/launcher/functions/) for `run_step` / `ensure_vllm`.

## Inputs and outputs

| Path | Role |
|---|---|
| `$LNVOX_VOICEBANK/manifest.json` | Required, non-empty. Every clip id is resolved against it. |
| `artifacts/<book_id>/01_characters.json` | Required. From [s1](/stages/s1-characters/), the lecture-mode stub written by `lnvox ingest --mode lecture` (an empty list, so only the Narrator is cast), or `ingest-scenario`. |
| `artifacts/<book_id>/03_voice_profiles.json` | Optional. If present (from an earlier s3 run), supplies each character's `voice_descriptor` and `accent` to target inference. |
| `artifacts/<book_id>/04_voice_assignments.json` | Optional input and the output, overwritten in full. As input, its locked entries win over everything and its unlocked entries are the weakest reuse layer (dropped by `--fresh`). |
| `artifacts/<series>/<earlier sibling>/04_voice_assignments.json` + `01_characters.json` | Prior volumes' castings, plus their casts for aliases. |

Output format: [`BookCasting`](/library/voices/schema/#bookcasting). See also [artifacts](/reference/artifacts/).

## How it works

`cli.voice_cast` → [`manifest.load`](/library/voices/manifest/#load) → checks `--narrator-clip` against the bank → reads this volume's own `04_voice_assignments.json` (only its locked entries under `--fresh`) → builds a [`PriorCastIndex`](/library/voices/matcher/#priorcastindex) from [`find_prior_volumes`](/library/series/) → [`matcher.cast_book`](/library/voices/matcher/#cast_book) (own casting passed as `existing`) → [`infer_target`](/library/voices/matcher/#infer_target) / [`rank_candidates`](/library/voices/matcher/#rank_candidates) via [LLMClient](/library/llm/client/) → writes `04_voice_assignments.json` and prints a table (character, gender, age, assigned clip, candidates).

For each character, the first rule that applies decides (details on the [matcher page](/library/voices/matcher/#per-character-decision-order)):

1. A **locked** entry in this volume's own casting whose clip is in the bank → kept exactly as it is, no LLM call.
2. Narrator with `--narrator-clip` → that clip, with a descriptor built from its metadata.
3. A usable match in a prior volume → the stored clip, no LLM call.
4. A usable **unlocked** entry in this volume's own casting → kept, no LLM call.
5. Gender `nonbinary` / `unknown` → left unassigned (s4 renders descriptor-only).
6. Narrator otherwise → fixed `female / adult` target with `england` clips first, one ranking call at most.
7. Everyone else → LLM target inference (gender, age band, accent / timbre / manner keywords) → hard filter on gender + age band → [the free clips in this character's own order](#variety) → accent-first ordering, capped at 24 candidates → LLM ranking → the best-ranked pick **among the candidates offered** that no other character has. Invented ids are dropped; if none is left, the first candidate is taken and `audit_note` says so.

### Variety

The ranking LLM knows a clip only by its accent, one sample sentence and its length. It has favourites — clips whose sentence suits "a man in his forties" wherever they sit in the list — and among the rest it goes by position. With every character seeing the bank in manifest order, cut to the first 60, the same few voices won again and again, a favourite for four characters of one book. Now each fresh cast (the auto-cast Narrator included):

- **leaves out the clips another character keeps** — prior volumes', this volume's current casting, `--narrator-clip`, and each clip assigned earlier in the run — while at least `--top-n` free ones remain. Past that it offers them after the free ones, but only those of earlier volumes' characters who are not in this volume. Clips of this volume's characters (including a recurring character's inherited clip, cast later in the run) are offered last, and only when nothing else is left. It takes the best-ranked clip in that order: free first, then one from an earlier volume;
- **shuffles** what's left in an order of its own — seeded by `--seed`, the book id and the character's name, so a re-run repeats it and another seed draws again — and offers a random **24** of them (accent matches first), so a favourite is often simply not on the list.

A clip is shared only when the bank has no free one of that gender and age. A long series reaches that point: every cast character keeps a clip, minor ones included, so a busy gender and age band runs out after a few volumes. Then a new character shares a voice with someone from an earlier volume rather than with someone the listener meets in the same book. Adding clips to the voicebank puts that off. Several characters **can** still have one voice on purpose: lock the same clip on each (the Studio, ComfyUI's Set Voice, or `"locked": true` in the JSON) — locks, `--narrator-clip` and prior-volume matches are never moved. Characters who share scenes and still sound alike are what the [casting audit](/stages/voice-audit/) reports, and `--audit-cast fix` repairs.

### Cross-volume reuse

Three sources can supply a character's clip without an LLM call. They are matched **per identity key** (a character's normalised name, its token-sorted form, and its aliases; see [lookup](/library/voices/matcher/#lookup)) and ranked, strongest first:

1. **Locked entries of this volume's `04_voice_assignments.json`.** `"locked": true` marks a deliberate hand edit (set in the JSON, or by the ComfyUI Set Voice node). It beats prior volumes, `--narrator-clip`, `--fresh` and the gender sanity check below. This is what makes a hand re-cast of a recurring character, or of the Narrator, survive the next `voice cast`. The only exception is a locked clip that is no longer in the bank: that character is re-cast and its `audit_note` says `voice cast: locked clip <id> is not in the voicebank; re-cast`.
2. **Every prior volume, newest wins.** Priors are the sibling directories under `artifacts/<series>/` that share the current leaf's prefix (case-insensitive) and have a smaller trailing number, and contain a `04_voice_assignments.json`. They are ordered **numerically**, so `volume-9` comes before `volume-10` with or without zero-padding; hidden directories are ignored and stray ones (`extras`, `covers`) are never taken for volumes. A leaf with no trailing number falls back to siblings whose name sorts before it (see [series](/library/series/)). Priors are folded oldest → newest, so characters who skip a volume still get their voice back. Canonical-name matches are trusted; matches through an alias also require the same gender. `--narrator-clip` beats this layer for the Narrator.
3. **Unlocked entries of this volume's `04_voice_assignments.json`** (weakest). Makes a re-run idempotent: a character first cast on the last run keeps its clip, so audio already rendered stays valid. `--fresh` drops this layer.

A clip from layers 2 or 3 is dropped (and the character re-cast) if it no longer exists in the current bank or its gender contradicts both the character and the stored target.

**What a kept entry keeps.** An entry taken from this volume's own file is copied whole: `target`, `ranked`, `locked`, `audit_note` and `voice_descriptor`. When a stronger layer assigns a clip, the entry's `audit_note` and non-empty `voice_descriptor` are still kept if that clip is the one the entry already had, and dropped if it is a different clip: a descriptor written for one voice must not follow the character onto another. A kept Narrator with an empty descriptor (the Set Voice node empties it) gets one rebuilt from the clip.

The console reports what it found, for example `Existing casting: 14 entries (2 locked). Locked entries are kept; …` and `Reusing assignments from prior volume(s) volume-01, volume-02 (412 identity keys)`, then tags each character `(locked, kept)`, `(reused from prior volume)` or `(kept from existing casting)`. A matcher fallback (invented ranking ids, a vanished locked clip) is printed in yellow with its `audit_note`.

:::note[Changing a recurring character's voice]
An unlocked edit to a character who also appears in an earlier volume is undone by the next `voice cast`: the continuity chain wins. That includes a move made by `voice audit --fix`, which never sets `locked`. To keep a different voice in this volume, set `"locked": true` on the entry. To change the voice for the rest of the series instead, change it in the newest earlier volume that has the character; later volumes then inherit it.
:::

### Narrator handling

- **`--narrator-clip`** is the recommended path, especially for lecture mode where the Narrator is the entire audiobook (DESIGN.md §6.3, §13.4). Pick the clip by ear in the [Voicebank Studio](/guides/voicebank-studio/). A mistyped id stops the command with exit `2` before anything is cast.
- **Locked.** A Narrator locked in this volume's own casting is kept even when `--narrator-clip` names another clip. The command warns (`The Narrator is locked to … --narrator-clip … is ignored`); set `"locked": false` on it to apply the flag.
- **Inherited.** Without the flag, a Narrator cast in any earlier volume is reused.
- **Auto-cast.** First volume, no flag: target `female / adult`, with `england` clips ranked first (accent is an ordering preference, never a filter). If the bank has no female adult clips, the matcher relaxes to any female clip, then any clip. The descriptor is always built from the chosen clip (for example `adult, British, female, warm clear voice, measured and engaging storyteller`), and Stage 3 uses it verbatim.

## Re-running and caching

- No LLM response cache: fresh castings are new LLM calls. Re-running is still cheap because of the own-assignments layer: previously cast characters make no calls, and only new or previously unmatched characters are cast.
- Re-running rewrites the whole file, but entries kept from it keep their `locked`, `audit_note` and `voice_descriptor` (see [what a kept entry keeps](#cross-volume-reuse)). Locks set at the [hand-casting checkpoint](/stages/voice-audit/#hand-casting-checkpoint) survive the resumed run.
- After any casting change, re-run s3 (the launcher runs it after Stage V): it regenerates the recast characters' profiles and re-voices the cached chapters they speak in. If s4 has already rendered them, the next s4 re-renders only those beats — the new clip changes their cache key, so no purge is needed.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| `Voicebank is empty. Run lnvox voice seed-cv first.` | Wrong `LNVOX_VOICEBANK`, or the bank has not been built ([voicebank](/stages/voicebank/)). |
| `· Name: no match (0 candidate(s))` | The inferred `(gender, age_band)` bucket is empty. Seed more speakers (check with `lnvox voice list`) or assign by hand. |
| `--narrator-clip '…' is not in the voicebank at …` (exit 2) | Typo, or the clip lives in a different bank (`LNVOX_VOICEBANK`). Nothing was cast or written. |
| `voice cast: none of the N ranked clip id(s) was among the M offered; fell back to the first candidate` | The ranking LLM returned only ids it was never shown. The character got the first candidate in filter order; listen to it, or pick another by hand. |
| s4 refuses the casting ("reference clip ids that don't exist") | Casting made against a different bank, or a hand edit to an id that is not in it. Fix `LNVOX_VOICEBANK` or the entry. |
| A recurring character reverts to an old voice | Its entry in this volume is unlocked, so the earlier volume's clip wins. Lock it (see the note above). |
| `The Narrator is locked to … --narrator-clip … is ignored` | Expected when resuming with the same launcher command after locking a different Narrator. Unlock it to apply the flag. |

For casting strategy (when to lock, how to audition), see the [casting guide](/guides/casting/) and [series guide](/guides/series/).

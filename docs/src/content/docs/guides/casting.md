---
title: Casting by hand
description: Pick the narrator, stop at the casting checkpoint, fix collisions by ear, lock your choices, re-audit and resume.
sidebar:
  order: 5
---

The matcher casts each character independently from demographics, so two people who talk to each other can land on near-identical clips, and anyone Stage 1 left `gender: unknown` (talking animals, mostly) gets no clip at all and renders descriptor-only. Both are worth fixing by ear. The cheapest moment is after voice cast, before anything is rendered:

| Changed casting… | Costs |
| --- | --- |
| before s3 | nothing |
| after s3 | the recast characters' voice profiles (one small LLM call); s3 re-voices the cached chapters they speak in without re-directing them |
| after s4 | the above plus re-rendering that character's beats — automatic, because the reference clip is part of the TTS cache key |

## 1. Pick the narrator first

The narrator voices 60–70 % of a third-person novel and 100 % of a lecture. Browse the bank by gender, age and accent (`lnvox voice list`, the Studio's Voicebank tab, or the ComfyUI Inspect → voicebank view), audition a few, and pass the winner as `--narrator-clip` on the first volume. Later volumes inherit it.

## 2. Stop at the checkpoint

```bash
./scripts/run_pipeline.sh <book> --narrator-clip cv_… --stop-after-audit
```

Runs ingest → s1 → s2 → voice cast → [casting audit](/stages/voice-audit/) (report only) and exits 0. The audit lists every pair of characters who **share scenes** — the narrator included — and whose clips are identical or closer than 2.5 semitones in pitch, with how much dialogue they share. Pairs who never speak together aren't flagged, even if they sound the same.

## 3. Fix by ear, and lock what you choose

Each tool edits `artifacts/<book>/04_voice_assignments.json`:

- **Voicebank Studio** — the [Casting tab](/guides/voicebank-studio/#casting-tab) shows line counts per character, the matcher's ranked candidates with reasons, and the whole bank, each playable. Re-casting there locks the entry and refreshes its descriptor for you:

  ```bash
  LNVOX_VOICEBANK=voicebank uv run python scripts/voicebank_studio.py \
      --casting artifacts/<book>/04_voice_assignments.json
  ```

- **ComfyUI** — the *hand casting* example workflow: browse (Inspect → voicebank with a filter like `female adult england`), audition (Load Audio → voicebank_clip), then write `Name = clip_id` lines into **Set Voice**, which locks and refreshes descriptors the same way. See [ComfyUI](/guides/comfyui/).
- **By hand** — set `assigned_clip_id` and `"locked": true`, and empty `voice_descriptor` (s3 uses a non-empty one verbatim, and it describes the old clip; `voice cast` rebuilds the Narrator's from its clip).

**Locking is what makes an edit stick.** A locked casting comes through every later `voice cast` unchanged — over prior volumes, over `--narrator-clip` (a locked Narrator wins, with a warning) and over `--fresh` — and `voice audit --fix` never moves it. An *unlocked* edit keeps its clip only while no earlier volume of the series knows the character; otherwise the earlier volume's voice comes back. Lock a deliberate share too (a narrator who is also a character), so the audit stops trying to separate them. The details are on [voice cast](/stages/voice-cast/#cross-volume-reuse).

## 4. Re-check, then resume

```bash
uv run lnvox voice audit <book>              # by the numbers
uv run lnvox voice audit <book> --fix        # optional: reassign the lower-speaking side of what's left
./scripts/run_pipeline.sh <book> --narrator-clip cv_…   # continue: s1/s2 are cached, locks are kept
```

Resuming re-runs voice cast and the audit; your locked castings come through both unchanged, and s3 writes descriptors for the voices you chose.

## After rendering

To recast a character in a finished book: set and lock the new clip, then re-run the launcher (or `lnvox s3` → s4 → s5 → s6). s3 regenerates that character's profile and re-voices the chapters they speak in; s4 re-renders only their beats, because the new clip changes their cache key. `lnvox audio purge --speaker "<name>"` is only for a clip file replaced *in place* under the same id, or to re-roll takes ([utilities](/stages/utilities/)).

:::tip[Force every descriptor]
`lnvox s3 --regen-profiles` rewrites every character's descriptor, not just the recast ones — every chapter is re-voiced and s4 re-renders the whole book. Use it only when you changed the descriptor prompt itself.
:::

---
title: Series and continuity
description: How later volumes inherit voices from earlier ones, how prior volumes are detected, and how to change a recurring voice on purpose.
sidebar:
  order: 4
---

Listeners notice immediately when a character's voice changes between volumes. ln-vox keeps voices stable across a series automatically, as long as the volumes share a series directory.

## Layout

```text
epubs/my-novel/volume-01.epub     novels/my-novel/volume-01/     artifacts/my-novel/volume-01/
epubs/my-novel/volume-02.epub     novels/my-novel/volume-02/     artifacts/my-novel/volume-02/
```

Run the volumes in order; each one reads the finished artifacts of the ones before it:

```bash
./scripts/run_pipeline.sh my-novel/volume-01 --narrator-clip cv_… --book-title "My Novel 1"
./scripts/run_pipeline.sh my-novel/volume-02 --book-title "My Novel 2"   # no narrator flag
```

## What carries over

Prior volumes are found by [`find_prior_volumes`](/library/series/): sibling directories under `artifacts/<series>/` with the same name prefix and a smaller trailing number — `volume-9` comes before `volume-10`, and stray folders (`extras`, `.cache`) are ignored. Then [voice cast](/stages/voice-cast/) decides each character's clip, first rule that applies:

1. **A locked entry in this volume's own `04_voice_assignments.json`** — a deliberate hand edit; kept as is.
2. **`--narrator-clip`**, for the Narrator.
3. **A match in a prior volume** — every prior volume is folded in, newest wins.
4. **An unlocked entry in this volume's own file** (unless `--fresh`) — so re-casting a volume is idempotent and doesn't re-roll voices already rendered.
5. **A fresh LLM cast.**

A character matches a prior entry by canonical name, by alias, or by name with word order flipped (alias matches also check gender), so s1 renaming "Kamijou Touma" to "Kamijou" or a character skipping a volume doesn't lose the voice. Matched characters keep their clip with no LLM call.

s1 itself does **not** feed prior casts into the LLM — continuity is decided at voice cast, from names and aliases.

## Changing a recurring voice on purpose

- **The narrator**: pass `--narrator-clip` on the volume where the change starts (and unlock the Narrator there if you had locked it — a locked Narrator wins over the flag).
- **A character**: set the new clip in the volume where the change starts and **lock** it (the Studio and the ComfyUI Set Voice node lock for you). A lock beats the prior-volume match, so the change sticks; later volumes then inherit the new voice from this one, the newest prior volume. Re-run from s3: descriptors follow the new clip and s4 re-renders only that character's beats.

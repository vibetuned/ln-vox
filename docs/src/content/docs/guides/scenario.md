---
title: Scenario mode (theater scripts)
description: Turn a theater script into a timed sync file — who says what, when, with which emotion — plus the full-cast audio it is measured against.
sidebar:
  order: 3
---

Scenario mode's input is a troupe's working script in Markdown, and its main deliverable isn't the audiobook but a **sync file**: one timed entry per script item — `{start, end, speaker, text, direction, emotion}` for dialogue, timed pauses for staging, zero-duration markers for sound and light cues. Use it to learn lines, rehearse against cast voices, or drive the régie. Design: DESIGN.md §17.

## What's different

- **The LLM structures, it never rewrites.** [`ingest-scenario`](/stages/ingest-scenario/) classifies every line as dialogue, staging (didascalies) or cue. Each dialogue line must be found in the source (case-insensitive substring check, validated in code); a line that fails is kept as staging with a loud warning, never paraphrased or dropped.
- **Formats are tolerated.** Speaker labels as `**Name** - line`, `NAME: line` or bold caps on their own line; cast sections titled `PERSONNAGES` / `CHARACTERS` / `CAST`; scene headers like `Séquence 3`, `SCENE 1`, `ACT II` — or none (a one-act). Label variants and typos are canonicalized into one character.
- **Per-line acting direction.** [`lnvox scenario`](/stages/scenario/) adds an `emotion` (calm, joy, sadness, anger, fear, surprise, disgust) and a short cue in the script's own language. A parenthetical cue inside a line (`(whispering) …`) is kept in the script and sync but not spoken.
- **Play pacing.** The mix uses 0.75 s between scenes and a 0.35 s lead-in before every line (so the reader app's ~0.3 s playback poll lands in silence); [`scenario-sync`](/stages/scenario-sync/) reproduces the same layout, so the sync total equals the `.m4b` duration exactly.
- **Group lines** (`Tous :` / `All:`) render with the narrator's voice; the sync file keeps the group label.

## Script format

Anything close to this works:

```markdown
CHARACTERS

- GUARD
- TRAVELLER

SCENE 1

[A city gate, at night. A lantern swings in the wind.]

GUARD: Who goes there?

TRAVELLER: (whispering) It's me. Open up.
```

## Run

```bash
./scripts/run_pipeline.sh myplay --mode scenario \
    --scenario-file "scenarios/My Play.md" --book-title "My Play"
```

The launcher runs ingest-scenario → voice cast → scenario → s4 → s5 → scenario-sync. There's no casting audit in scenario mode — check the casting by ear in the [Studio](/guides/voicebank-studio/) (the matcher can give two characters who share scenes the same clip). EPUB sync (s6) doesn't apply.

For a French cast, seed a French bank from the French Common Voice tarball and select it for every command:

```bash
LNVOX_VOICEBANK=voicebank-fr ./scripts/run_pipeline.sh myplay --mode scenario --scenario-file "…"
```

A casting made against one bank refuses to render against another.

## Outputs

| File | Contents |
| --- | --- |
| `artifacts/<id>/06_final/<title>.m4b` | Full-cast performance, one chapter marker per scene |
| `artifacts/<id>/07_sync/play.json` | Every line / staging / cue with absolute timestamps |
| `artifacts/<id>/07_sync/<scene>.json` | The same, per scene |
| `artifacts/<id>/07_sync/play.srt` | The timeline as subtitles: dialogue `SPEAKER: text`, staging `[…]`, cues `[[…]]` |

Editing the script and re-running re-pays only what changed: ingest is content-cached under `cache/scenario/`, and the TTS cache re-renders only changed lines, so the timing of everything else is preserved.

:::note[Privacy and backends]
`scenarios/` and `data/` are gitignored; script text only goes to the local LLM and TTS. Scenario sync needs per-line timing, so it requires the Dramabox backend — VibeVoice renders audio but its session WAVs have no per-line boundaries.
:::

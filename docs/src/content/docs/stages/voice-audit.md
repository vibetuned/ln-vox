---
title: Stage A — casting audit
description: lnvox voice audit flags characters who share scenes but got the same or near-identical voices, and can reassign the quieter one before s3.
sidebar:
  order: 7
  label: A · casting audit
---

Stage A checks the casting Stage V just produced. The matcher casts every character independently on demographics, so two people who talk to each other can land on the same clip or two clips a fraction of a semitone apart, and nothing else in the pipeline would notice until someone listens to the finished m4b. The audit finds those pairs from the scene files and, with `--fix`, reassigns the lower-speaking side. It runs after Stage V and before Stage 3, where a casting change still costs nothing; after s4 it costs a re-render (DESIGN.md §20).

## Command

```bash
uv run lnvox voice audit <book_id> [--fix] [--min-semitones 2.5] [--min-shared 3]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` | required | Book whose `04_voice_assignments.json` is audited. |
| `--fix` | off | Reassign the lower-speaking side of each collision and rewrite the casting. |
| `--min-semitones` | `2.5` (`audit.MIN_SEMITONES`) | Flag scene-sharing pairs whose clips' median f0 are closer than this. Repair uses the same value for its "clear every partner" rule. |
| `--min-shared` | `3` (`audit.MIN_SHARED`) | Ignore pairs with less shared-scene weight than this. Also defines who counts as a scene partner during repair. |

`LNVOX_VOICEBANK` selects the bank (and therefore its `acoustics.json`). The command needs the `voice` extra (`librosa`, `soundfile`) to build the acoustic index.

### Exit codes

| Code | When |
|---|---|
| `0` | No collisions. |
| `0` | `--fix` given, whether or not every collision could be repaired. |
| `1` | Collisions found and `--fix` not given (report mode). |
| `1` | No `04_voice_assignments.json` ("run `lnvox voice cast` first"), or neither `03_directed/` nor `02_scenes/` holds any `*.json` ("run s2 first"). |
| non-zero | Uncaught errors, e.g. the voice extra is missing or a JSON file is malformed. |

A non-zero exit in report mode means "found something", not "broke". That is why the launcher never lets it stop the pipeline.

### Output

```text
14 speaking character(s), 15/16 with a reference clip, co-occurrence from 02_scenes/
                      Voice collisions (2)
 kind       characters                  shared   apart
 same clip  Aldric Vane (212) + Mira (41)   18   0.0 st
 too close  Brann (60) + Sera Kalt (95)      9   0.7 st
Re-run with --fix to reassign the lower-speaking side.
```

(Simplified: the real table is drawn with box characters.) The numbers in parentheses are each character's total beats. With `--fix`, the table is followed by one line per repair (`✓ Mira: cv_… → cv_… (231 Hz)`), one warning per unfixed pair (`no separated clip available (locked, narrator, or bank exhausted) — recast by hand.`) and a reminder to re-run s3. The table shows the collisions found **before** repair; run the audit again to confirm the result.

## In the launcher

```bash title="scripts/run_pipeline.sh"
    # Casting audit (DESIGN.md §20). Here, not later: 02_scenes already gives
    # co-occurrence, and a casting change is free before s3 — after s4 it costs
    # a re-render. Never fatal; a collision is a quality warning, not a broken
    # pipeline, and `voice audit` exits non-zero purely to report findings.
    if [ "$MODE" != "scenario" ] && [ "$AUDIT_CAST" != "off" ]; then
        banner "Stage A: casting audit ($AUDIT_CAST)"
        if [ "$AUDIT_CAST" = "fix" ]; then
            uv run lnvox voice audit "$BOOK_ID" --fix || true
        else
            uv run lnvox voice audit "$BOOK_ID" || true
        fi
    fi
```

- **Mode.** `AUDIT_CAST="${LNVOX_AUDIT_CAST:-report}"`, overridden by `--audit-cast <mode>`:
  - `report` (default): list collisions, change nothing.
  - `fix`: run with `--fix`.
  - `off`: skip the stage.

  The value is not validated; anything other than `fix` or `off` behaves like `report`.
- **`|| true`.** Report mode exits `1` whenever it finds a collision, and a deliberate, locked share keeps it that way on every run. Swallowing the code keeps a quality warning from aborting the run. The side effect is that real errors (missing voice extra, no scene files) are swallowed too; read the Stage A output.
- **No `run_step`.** The audit is not retried and has no `ensure_vllm` hook; it makes no LLM calls.
- **Skipped** in scenario mode, and with `--skip-llm` (it lives inside the LLM phase). In lecture mode it runs but has nothing to compare: only the Narrator is cast, and on a first run there are no scene files yet, so it prints "No 02_scenes/ or 03_directed/" and the launcher moves on.
- **Thresholds.** The launcher always uses the defaults. For other thresholds, run the command by hand at the checkpoint below.

### Hand-casting checkpoint

`--stop-after-audit` stops the launcher right after Stage A, before s3 bakes a voice descriptor per character from the casting and before anything is rendered:

```bash title="scripts/run_pipeline.sh"
    if [ "$STOP_AFTER_AUDIT" -eq 1 ]; then
        stop_vllm
        banner "Stopped after the casting audit (--stop-after-audit)"
        echo "Casting:  $BOOK_ART/04_voice_assignments.json"
        echo ""
        echo "Fix it by hand, then re-run WITHOUT --stop-after-audit to continue:"
        echo "  LNVOX_VOICEBANK=${LNVOX_VOICEBANK:-voicebank} uv run python scripts/voicebank_studio.py \\"
        echo "      --casting $BOOK_ART/04_voice_assignments.json"
        echo "  LNVOX_VOICEBANK=${LNVOX_VOICEBANK:-voicebank} uv run lnvox voice audit $BOOK_ID   # re-check"
        echo ""
        echo "Set \"locked\": true on an assignment you want --audit-cast fix to leave alone."
        exit 0
    fi
```

It exits `0` after stopping the LLM server. The check does not depend on `--audit-cast`: with `--audit-cast off` it still stops, just without an audit. Pass `--audit-cast fix` alongside it to repair first and then review.

:::note[Locks survive the resumed run]
Resuming the launcher re-runs Stage V, and Stage V keeps every entry marked `"locked": true` exactly as you left it, with its `audit_note` and `voice_descriptor`, over prior volumes and over a `--narrator-clip` still on the command line ([Stage V](/stages/voice-cast/#cross-volume-reuse)). The resumed Stage A then honours the same locks, so `--audit-cast fix` never moves them. The flow:

1. Stop with `--stop-after-audit` and fix clips by hand (Studio, ComfyUI Set Voice, or JSON).
2. Set `"locked": true` on every entry you changed that must stay as it is (the ComfyUI Set Voice node does this by default). Unlocked edits to a first-appearance character also survive, but an unlocked edit is replaced by an earlier volume's clip for a recurring character, loses to a `--narrator-clip` still on the command line for the Narrator, and may be moved again by `--audit-cast fix`.
3. Optionally re-check with `uv run lnvox voice audit <book_id>`, then resume with the same command minus `--stop-after-audit`.
:::

## Inputs and outputs

| Path | Role |
|---|---|
| `artifacts/<book_id>/04_voice_assignments.json` | Read; rewritten in place with `--fix` when at least one repair was applied. |
| `artifacts/<book_id>/03_directed/*.json` | Co-occurrence source when the directory exists and has JSON files. |
| `artifacts/<book_id>/02_scenes/*.json` | Fallback co-occurrence source (the normal case on a first run). |
| `$LNVOX_VOICEBANK/manifest.json` | Clip demographics for the replacement search. |
| `$LNVOX_VOICEBANK/acoustics.json` | Acoustic cache; built or extended on first use ([acoustics](/library/voices/acoustics/)). |

:::note[Which scene directory, and why it matters]
`03_directed/` is preferred over `02_scenes/`. On a first launcher run Stage A comes before s3, so it reads `02_scenes/`. Narration beats there have `speaker: null`, but every beat of type `narration` is counted as the `Narrator`'s (the speaker s3 gives it), so Narrator collisions are found on the first run too. The Narrator's beat count is higher in `02_scenes/` (s3 fuses consecutive narration beats), but a pair's weight is the lesser side's count, which for a Narrator pair is almost always the other character's, so the collisions found are the same. `03_directed/` may be stale relative to a re-run s2.
:::

## How it works

`cli.voice_audit` → `BookCasting.model_validate_json` → [`manifest.load`](/library/voices/manifest/#load) → [`load_acoustics`](/library/voices/acoustics/#load_acoustics) → [`scene_cooccurrence`](/library/voices/audit/#scene_cooccurrence) → [`find_collisions`](/library/voices/audit/#find_collisions) → (with `--fix`) [`repair_collisions`](/library/voices/audit/#repair_collisions) → write casting.

1. **Weight pairs.** Per scene, count each speaker's beats (narration beats count as `Narrator`'s); each pair of speakers present gets `min(beats_a, beats_b)`, summed over the book.
2. **Detect.** For each pair with weight ≥ `--min-shared` where both have a clip: same clip → `same clip`; otherwise, if both clips have a measured median f0 and the gap is below `--min-semitones` → `too close`. Unmeasured pitches are skipped, not guessed. Collisions are ordered by weight, then by gap.
3. **Repair** (`--fix`), in that order. Re-check the pair against the current casting, since an earlier move may have resolved it. Move the side with fewer beats, unless it is the `Narrator` or `locked`, in which case move the other side; if both are protected, report it unfixed. The replacement must be unused in the book, match the current clip's gender, never have been held by this character earlier in the run, and sit at least `--min-semitones` from **every** scene partner's pitch. Search the same age band at bandwidth ≥ 10 kHz / SNR ≥ 38 dB, then adjacent age bands, then adjacent bands at 9 kHz / 32 dB. Take the best-quality candidate, record the move in `audit_note`, and empty the moved character's `voice_descriptor` (it described the old clip; s3 writes a new one).

The full rules, including tie-breaking, are on the [library page](/library/voices/audit/).

## Re-running and caching

- The audit is read-only without `--fix` and idempotent with it: a second `--fix` run finds only what is still unresolved.
- The acoustic index is incremental. The first audit against a bank measures every clip; later runs only measure new ids. Delete `acoustics.json` after rebuilding clips under the same ids.
- After a repair, re-run s3: the moved characters' profiles regenerate and the chapters they speak in are re-voiced on their own (the launcher runs s3 next). If s4 already ran, the next s4 re-renders just those beats, because their reference clip changed the cache key.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| Same pair reported unfixed every run | Both sides are protected (Narrator and/or `locked`): expected for deliberate shares. Otherwise the bank has no clean, unused, same-gender clip far enough from all partners. Seed more speakers, lower `--min-semitones`, or recast by hand. |
| `N/M with a reference clip` lower than expected | Characters the matcher could not cast are invisible to the audit. |
| Stage A output shows a traceback but the run continued | Swallowed by `\|\| true`, most often the missing voice extra (`uv sync --extra voice`). |
| A fix is undone on the next launcher run | The moved character appears in an earlier volume, and Stage V restored that volume's clip: audit moves are not locked, and an earlier volume beats an unlocked entry. Set `"locked": true` on the moved entry to keep it. |

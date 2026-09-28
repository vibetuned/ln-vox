---
title: Stage S — scenario direction
description: lnvox scenario adds per-line emotion and script-language cues to a structured theater script and writes 03_directed/ with staging pauses as scene breaks.
sidebar:
  order: 10
  label: S · scenario direction
---

Stage S is scenario mode's direction pass, playing the role s3 plays for novels. It reads the structured, verbatim script `00_script.json`. For each dialogue line it asks the LLM for one of 7 emotions and a short cue **in the script's language**, then writes TTS-ready `03_directed/<scene_id>.json` files that s4 and s5 consume unchanged. Each script scene becomes one "chapter". Each run of dialogue between staging directions becomes one directed scene, so the mix's inter-scene pad turns into a "staging pause" exactly where the action happens. See the [scenario guide](/guides/scenario/) for the full workflow.

## Command

```bash
uv run lnvox scenario <book_id> [--regen-profiles]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `BOOK_ID` (argument) | required | Scenario id, as passed to `ingest-scenario --id`. Reads and writes `artifacts/<book_id>/` |
| `--regen-profiles` | off | Force-regenerate **every** voice profile. Not needed after a recast: recast characters' profiles regenerate on their own. Every scene whose descriptors change is re-voiced, so s4 re-renders it |

The command:

- exits 1 if `00_script.json` is missing (`Run lnvox ingest-scenario`);
- uses an empty cast if `01_characters.json` is missing;
- prints a warning and leaves descriptors un-anchored to clips if `04_voice_assignments.json` is missing;
- loads the voicebank (`$LNVOX_VOICEBANK`, e.g. `voicebank-fr`) only when the casting exists **and** `<voicebank>/manifest.json` exists;
- always creates an `LLMClient`, so the server must be up;
- prints `✓ scene <id>: N beat(s), G staging-delimited group(s)` per scene.

## In the launcher

```bash title="scripts/run_pipeline.sh"
    elif [ "$MODE" = "scenario" ]; then
        banner "Stage S: scenario direction (emotion + cues → directed beats)"
        run_step ensure_vllm "Stage S (scenario)" uv run lnvox scenario "$BOOK_ID"
```

With `--mode scenario`, the order is:

1. `ingest-scenario` (itself an LLM pass, run only when `--scenario-file` is given);
2. voice cast;
3. Stage S;
4. s4;
5. s5 with play pacing;
6. [scenario-sync](/stages/scenario-sync/).

The casting audit is skipped in scenario mode. `--stop-after-audit` still stops before Stage S, which is the moment to collapse several roles onto one actor's voice by hand in the [Voicebank Studio](/guides/voicebank-studio/). `run_step ensure_vllm` gives the step up to `--step-retries` attempts (default 3), and relaunches a dead managed server before each retry. It is skipped with `--skip-llm`.

The pauses Stage S's structure produces are sized in the mix step:

```bash title="scripts/run_pipeline.sh"
    if [ "$MODE" = "scenario" ]; then
        MIX_ARGS+=(--inter-chapter 0.75 --lead-in 0.35)
    fi
```

| Boundary in `03_directed/` | s5 pad | Value in the launcher | scenario-sync option (default: the mix's recorded value) |
| --- | --- | --- | --- |
| Between script scenes (chapters) | `--inter-chapter` | 0.75 s | `--inter-scene` (default 0.75) |
| Between staging-delimited runs (directed scenes) | `--inter-scene` | 1.0 s (s5 default, not passed) | `--staging-pause` (default 1.0) |
| Between lines of one run | `--intra` | 0.25 s (s5 default, not passed) | `--intra` (default 0.25) |
| Before every line | `--lead-in` | 0.35 s | `--lead-in` (default 0.35) |

## Inputs and outputs

| Path | Direction | Contents |
| --- | --- | --- |
| `artifacts/<id>/00_script.json` | read | `ScenarioScript`: scenes of ordered `dialogue` / `staging` / `cue` items, verbatim |
| `artifacts/<id>/01_characters.json` | read (optional) | Cast from ingest-scenario (roster plus speaker-label merge) |
| `artifacts/<id>/04_voice_assignments.json` | read (optional) | Clip assignments that anchor descriptors |
| `$LNVOX_VOICEBANK/manifest.json` | read (optional) | Clip metadata |
| `artifacts/<id>/03_voice_profiles.json` | read/write (cache) | Voice profiles, from s3's `refresh_voice_profiles`, each recording the clip and locked descriptor it was written for |
| `artifacts/<id>/03_directed/<scene_id>.json` | read/write (cache) | One `ChapterDirected` per script scene. `chapter_id` equals `scene_id` (`"01"`, `"02"`, …). Records each speaker's descriptor (`voice_descriptors`) |

There is a worked input and output example on the [library page](/library/stages/scenario/#artifacts).

## How it works

`cli.stage_scenario` → [`scenario.run`](/library/stages/scenario/#run) → [`refresh_voice_profiles`](/library/stages/s3-director/#refresh_voice_profiles) → [`direct_scene`](/library/stages/scenario/#direct_scene) → [`group_dialogue_runs`](/library/stages/scenario/#group_dialogue_runs) / [`spoken_text`](/library/stages/scenario/#spoken_text) → [`format_prompt`](/library/stages/s3-director/#format_prompt).

1. **Profiles.** The same clip-anchored, casting-aware cache as s3: a cached profile is reused while its character's clip and locked descriptor are unchanged, and only new or recast characters go to the LLM.
2. **One LLM call per script scene.** The model reads the scene in order, with dialogue as `[L<n>] [speaker] "text"` and staging as `(didascalie) text`. Cues are omitted. For every numbered line it returns `emotion` (`calm`, `joy`, `sadness`, `anger`, `fear`, `surprise`, `disgust`) and a 2–6-word `cue` in the script's language (`"méfiant, voix basse"`). The prompt is English. The answers follow the script.
3. **Runs.** Dialogue items are grouped into runs that `staging` items close. Cues don't split a run, and leading staging opens nothing. Run `g` becomes `DirectedScene` `<scene_id>_g<NN>`.
4. **Beats**, one or more per line:
   - **Spoken text.** Parenthetical acting cues (`(haughtily) Sir!`) are stripped, and the rest is split at 375 characters if needed.
   - **Speaker.** Group labels (`Tous`, `Ensemble`, `All`, `Choeur`, …) render as `Narrator` with the narrator descriptor. Any other speaker without a profile also gets the narrator descriptor.
   - **Fields.** `direction` = the script-language cue, for humans and the sync file. `emotion` = the enum value. `prompt = format_prompt("<descriptor>, <emotion>", text)`, for example `(elder male - hoarse low baritone - slow and wary - fear) "Qui va là ?"`. `source_paragraph` = the line's 1-based index in the scene, which lets scenario-sync merge split chunks back into one entry.

`staging` and `cue` items produce no audio. Staging becomes a pause, and scenario-sync writes both as timeline entries.

## Re-running and caching

The caches follow the casting, as in [s3](/stages/s3-director/#casting-changes-between-runs):

- `03_voice_profiles.json`: a profile is reused while its clip and locked descriptor match `04_voice_assignments.json`. A recast character's profile is regenerated. Running `voice cast` on a script that was directed uncast re-anchors every profile. `--regen-profiles` regenerates all of them.
- `03_directed/<scene_id>.json`: reused while its recorded speaker descriptors match the current profiles. When a descriptor changed, the scene's prompts are re-voiced, keeping each line's emotion and cue, with no LLM call. New prompts re-key the s4 cache for exactly those lines. Delete a file to get fresh cues for that scene.
- Re-running `ingest-scenario` with changed script text doesn't invalidate `03_directed/` unless a scene's set of speakers changed. Delete the files for the scenes you edited.
- A scene file from before the records existed is treated as current, and stamped with the descriptors of the profiles file as it was before the run.

## Failure modes

- **Server down or dying mid-stage.** Cue calls re-raise transport errors, so the step fails and `run_step ensure_vllm` relaunches the server before the retry. Scenes finished before the crash are reused.
- **Unusable cue answer** (bad JSON, or an emotion outside the enum that survives the client's retries). It fails silently. Every line in that scene gets `emotion: "calm"` and an empty `direction`, and the result is cached. If `direction` is `""` throughout a scene file, delete it and re-run.
- **A speaker label with no profile** (a label variant ingest didn't merge). The line renders with the narrator descriptor. s4 finds no clip for that name, so it renders descriptor-only. Fix the label or cast in ingest and voice cast.
- **A scene with no dialogue** yields a `ChapterDirected` with `scenes: []`.

Design: DESIGN.md §17.3 (artifact mapping), §17.4 (pauses and lead-in), §17.6 (group lines, direction language).

---
title: Stage Y — scenario sync
description: Emit timed sync files for a theater script — every dialogue line, staging pause and cue — computed from the same plan as the s5 mix.
sidebar:
  order: 14
  label: Y · scenario sync
---

In scenario mode the deliverable is two things: the full-cast m4b, and a timeline of the play that says when each line is spoken, where the staging pauses fall and where every sound or light cue sits. `lnvox scenario-sync` produces that timeline as one JSON file per scene, a whole-play `play.json`, and a `play.srt` for any player that takes subtitles. It replaces Stage 6 for scripts: there is no EPUB to align against, and the structured script already gives the exact line order (DESIGN.md §17.4).

## Command

```bash
uv run lnvox scenario-sync <scenario_id> [--title T] [--intra S] [--staging-pause S] [--inter-scene S] [--lead-in S]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | Scenario id, the `--id` given to `ingest-scenario`. Artifacts come from `artifacts/<id>/`. |
| `--title` | the newest mix | Which mix to follow: the `lnvox s5 --title` of `06_final/<title>.m4b`. An unknown title exits 1. |
| `--intra` | recorded, else `0.25` | Pad between lines of one run (s5 `--intra`). |
| `--staging-pause` | recorded, else `1.0` | Pause where staging happens (s5 `--inter-scene`). |
| `--inter-scene` | recorded, else `0.75` (`SCENARIO_INTER_SCENE`) | Pad between script scenes (s5 `--inter-chapter`). |
| `--lead-in` | recorded, else `0.35` (`SCENARIO_LEAD_IN`) | Silence before every beat (s5 `--lead-in`). It covers the reader app's ~0.3 s playback poll. |

By default every pad comes from the mix's `06_final/<title>.timings.json`, and the play covers exactly the scenes that mix contains, in its order. A flag only overrides one recorded pad. With no sidecar, the fallbacks are the scenario pacing the launcher gives s5, which is **not** s5's own default: see [the layout contract](/stages/s5-mix/#the-layout-contract).

## In the launcher

```bash title="scripts/run_pipeline.sh"
elif [ "$MODE" = "scenario" ]; then
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        banner "Skipping scenario-sync (VibeVoice sessions have no per-line timing — DESIGN.md §17.4)"
    else
        banner "Scenario sync: timed sync files (DESIGN.md §17.4)"
        run_step - "Scenario sync" uv run lnvox scenario-sync "$BOOK_ID"
    fi
```

- It runs as the last step of `--mode scenario`, after s5 has been given `--inter-chapter 0.75 --lead-in 0.35`. No flags are passed, so it follows the sidecar that s5 step has just written.
- It is skipped with `--skip-sync`, or with `--tts-backend vibevoice`.
- The step goes through `run_step -` (retries, no hook).

## Inputs and outputs

**Reads**

| Path | Used for |
|---|---|
| `artifacts/<id>/00_script.json` | The structured, verbatim script: scenes, then items of type `dialogue`, `staging` or `cue`. Written by [`ingest-scenario`](/stages/ingest-scenario/). |
| `artifacts/<id>/03_directed/<scene>.json` | Directed beats, grouped into staging-delimited runs `<scene>_gNN`. Each beat carries `source_paragraph` (the line index), `direction` and `emotion`. Written by [`lnvox scenario`](/stages/scenario/). |
| `artifacts/<id>/05_audio/<scene>/manifest.json` | Per-beat `duration_seconds` from the **per-beat** Dramabox render. |
| `artifacts/<id>/06_final/<title>.timings.json` | The mix's pads and scene list. The newest sidecar unless `--title` is given. |

**Writes** to `artifacts/<id>/07_sync/`:

| File | Content |
|---|---|
| `<scene_id>.json` | One `SceneSync` per script scene: `scene_id`, `title`, absolute `start` and `end`, and `entries`. |
| `play.json` | `PlaySync`: `scenario_id`, `title`, `total_duration_seconds`, the four pad values, and every scene. |
| `play.srt` | The same timeline as subtitles: `SPEAKER: text`, `[staging]`, `((cue))`. |

A dialogue entry looks like this:

```json
{"start": 12.34, "end": 15.2, "type": "dialogue", "speaker": "Gardien",
 "text": "Qui va là ?", "direction": "méfiant, voix basse", "emotion": "fear"}
```

A full worked example with the arithmetic, `play.json` and `play.srt` is on the [library page](/library/stages/scenario-sync/#worked-example).

## How it works

`cli.stage_scenario_sync` → [`scenario_sync.run`](/library/stages/scenario-sync/#run) → [`build_play_sync`](/library/stages/scenario-sync/#build_play_sync) → [`build_scene_sync`](/library/stages/scenario-sync/#build_scene_sync) → [`_beat_times`](/library/stages/scenario-sync/#_beat_times), then [`to_srt`](/library/stages/scenario-sync/#to_srt).

1. For each script scene, the directed beats are zipped 1:1 with the rendered manifest.
2. `_beat_times` replays Stage 5's concat plan:
   - the intra pad between beats of one run, and the staging pause between runs;
   - the lead-in, then the beat's duration, for every beat.
3. A dialogue line's `start` is where its first chunk's lead-in begins, and its `end` is where its last chunk's speech ends. Lines split into chunks become one entry.
4. Staging items fill the pause window, from the end of the previous line to the start of the next. Cues are zero-duration markers at the end of the previous line.
5. Scenes are chained with the inter-scene pad, in the order the mix recorded (script order without a sidecar). The last scene's end is `total_duration_seconds`, which should equal the m4b's length.

## Re-running and caching

- It is pure arithmetic over JSON: instant, deterministic, and it overwrites every output.
- Re-run it after every `s4` or `s5` re-run. It reads the newest sidecar at the time it runs. Because Stage 4's content-hash cache only re-renders changed lines, editing a script and re-running `scenario`, `s4`, `s5` and `scenario-sync` stays cheap, and the timeline stays exact.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| `Missing …/00_script.json. Run lnvox ingest-scenario.` (exit 1) | Wrong id, or ingest has not run. |
| `scene 03: missing directed chapter or audio manifest — run lnvox scenario and lnvox s4 first.` (exit 1) | A scene of the mix (or of the script, without a sidecar) has no `03_directed/03.json` or no `05_audio/03/manifest.json`. With VibeVoice the audio lives in `05_audio_v2/`, which is never read. |
| `chapter 03: N directed beat(s) but M manifest entrie(s). scenario-sync needs per-beat rendering …` (exit 1) | The manifest does not correspond 1:1 to the beats, for example a VibeVoice session manifest. Re-render with the Dramabox backend. |
| `No mix titled 'X': …` (exit 1) | `--title` names no mix in `06_final/`. The message lists the mixes present. |
| `… was mixed from 05_audio_v2/ (VibeVoice sessions), which has no per-line timing` (exit 1) | The mix it would follow is a `--v2` mix. Pass `--title` of a Dramabox mix. |
| The highlight lands late, or the SRT drifts from the audio | Only possible without a sidecar, or with a flag overriding a recorded pad. Drop the flag, or re-run s5 so a sidecar exists. |
| The play sounds book-paced (2 s between scenes) | s5 was run by hand without the scenario pacing. The sync file matches it, but re-mix with `lnvox s5 <id> --inter-chapter 0.75 --lead-in 0.35` for play pacing. |

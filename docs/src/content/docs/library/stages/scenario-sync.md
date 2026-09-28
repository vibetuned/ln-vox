---
title: lnvox.stages.scenario_sync
description: Scenario-mode sync emitter — replays the s5 concat plan to time every dialogue line, staging pause and cue, and writes per-scene JSON, play.json and play.srt.
sidebar:
  order: 10
  label: scenario_sync
---

`lnvox.stages.scenario_sync` is the scenario-mode counterpart of [Stage 6](/library/stages/s6-sync/). There is no EPUB to align against, so it walks the structured script (`00_script.json`) alongside the directed beats and the rendered-audio manifests. For every script item it emits a timed entry `{start, end, type, speaker, text, direction, emotion}`. It is called by `lnvox scenario-sync` ([stage page](/stages/scenario-sync/)) after `lnvox s5` (DESIGN.md §17.4).

Timing is **deterministic**. It replays the same cursor arithmetic Stage 5 uses (beat `duration_seconds` from the manifests plus the same pad values) and never probes audio. [`run`](#run) takes the pad values and the scene list from the mix's `.timings.json` sidecar by default, so the sync file and the m4b agree by construction.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`SCENARIO_INTER_SCENE`](#scenario_inter_scene), [`SCENARIO_LEAD_IN`](#scenario_lead_in) | constants | `0.75` s between script scenes, `0.35` s before every beat. |
| [`SyncEntry`](#syncentry), [`SceneSync`](#scenesync), [`PlaySync`](#playsync) | Pydantic models | The output schema. |
| [`run`](#run) | function | Read the artifacts, write `07_sync/<scene>.json`, `play.json` and `play.srt`. |
| [`build_play_sync`](#build_play_sync) | function | Absolute timing for the whole play. |
| [`build_scene_sync`](#build_scene_sync) | function | Timed entries for one scene at a given offset. |
| [`to_srt`](#to_srt) | function | SRT export. |
| [`_beat_times`](#_beat_times) | private | Replays the s5 concat plan for one scene. |

## The per-item timing plan

In scenario mode each script scene is one Stage-4/5 "chapter" (`scene_id` `01`, `02`, …). The [`scenario`](/library/stages/scenario/) stage splits a scene's dialogue into **staging-delimited runs**, and each run becomes a directed scene `<scene>_gNN`. Stage 5 inserts its pads by comparing consecutive beats' `scene_id`s, so the plan is shared as follows:

| Boundary | s5 pad | Sidecar key | scenario-sync parameter | Launcher value |
|---|---|---|---|---|
| consecutive beats of one run (including the chunks of a split line) | `--intra` | `intra_silence` | `intra` | `0.25` |
| run to run, where staging happens | `--inter-scene` | `inter_scene_silence` | `staging_pause` | `1.0` |
| script scene to script scene | `--inter-chapter` | `inter_chapter_silence` | `inter_scene` | `0.75` |
| before every beat | `--lead-in` | `lead_in_silence` | `lead_in` | `0.35` |

:::caution[Flag names collide]
s5's `--inter-scene` corresponds to scenario-sync's `--staging-pause`, **not** to scenario-sync's `--inter-scene`. The scene-to-scene pad is s5 `--inter-chapter`.
:::

## Constants

### `SCENARIO_INTER_SCENE`

```python
SCENARIO_INTER_SCENE = 0.75
```

The pad between script scenes, which is s5's `--inter-chapter` in scenario mode. The 2.0 s book default reads well between novel chapters but drags between play scenes (user-tested 2026-07-24). `scripts/run_pipeline.sh` passes the matching `--inter-chapter 0.75` to s5. [`run`](#run) uses it only when there is no sidecar.

### `SCENARIO_LEAD_IN`

```python
SCENARIO_LEAD_IN = 0.35
```

Extra silence placed before **every** beat of a scenario mix (s5 `--lead-in 0.35`). The 2reply reader app polls playback position only every ~0.3 s on older devices. A dialogue entry's `start` points at the beginning of the lead-in, so the poll always fires during silence and the highlight never lands after speech has begun.

## Models

### `SyncEntry`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `start` | `float` | required | Seconds from the start of the m4b. For dialogue this is where the first chunk's **lead-in** starts. |
| `end` | `float` | required | Dialogue: where speech ends (the last chunk's end). Staging: the next line's `start`. Cue: equal to `start`. |
| `type` | `str` | required | `dialogue`, `staging` or `cue`. |
| `speaker` | `str` | `""` | The script label as printed. Group labels such as `Tous` are kept, even though they render in the narrator's voice. Empty for staging and cues. |
| `text` | `str` | required | Verbatim item text. |
| `direction` | `str` | `""` | Short performance cue in the script's language (dialogue only). |
| `emotion` | `str` | `""` | One of `calm`, `joy`, `sadness`, `anger`, `fear`, `surprise`, `disgust` for dialogue; `""` otherwise. |

### `SceneSync`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `scene_id` | `str` | required | Script scene id (`01`, `02`, …). |
| `title` | `str` | `""` | Scene title from the script. |
| `start` | `float` | `0.0` | Absolute offset of the scene in the m4b. |
| `end` | `float` | `0.0` | End of the scene's last beat. |
| `entries` | `list[SyncEntry]` | `[]` | One entry per script item, in script order. |

### `PlaySync`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `scenario_id` | `str` | required | From `00_script.json`. |
| `title` | `str` | `""` | Play title. |
| `total_duration_seconds` | `float` | `0.0` | End of the last scene, which should equal the m4b duration. |
| `intra_silence` | `float` | `0.25` | Pad value used. |
| `staging_pause` | `float` | `1.0` | Pad value used. |
| `inter_scene_silence` | `float` | `SCENARIO_INTER_SCENE` | Pad value used. |
| `lead_in_silence` | `float` | `0.0` | Lead-in used: the mix's, or `0.35` without a sidecar. |
| `scenes` | `list[SceneSync]` | `[]` | In mix order, which is script order unless the mix reordered or left out scenes. |

## API

### `run`

```python
def run(
    book_dir: Path,
    *,
    intra: float | None = None,
    staging_pause: float | None = None,
    inter_scene: float | None = None,
    lead_in: float | None = None,
    mix_title: str | None = None,
    progress: Callable[[str], None] = print,
) -> PlaySync
```

Reads the scenario's artifacts, builds the play-level sync and writes it out. The mix to follow is loaded with [`s5_mix.load_mix_layout`](/library/stages/s5-mix/#load_mix_layout).

**Parameters**

- `book_dir`: `artifacts/<scenario-id>/`.
- `intra`, `staging_pause`, `inter_scene`, `lead_in`: `None` (the default) takes the mix's recorded value (the sidecar keys in [the table above](#the-per-item-timing-plan)). Without a sidecar the fallbacks are `0.25`, `1.0`, [`SCENARIO_INTER_SCENE`](#scenario_inter_scene) and [`SCENARIO_LEAD_IN`](#scenario_lead_in). A number overrides the recorded value.
- `mix_title`: the `lnvox s5 --title` of the mix to follow. `None` means the newest sidecar in `06_final/`.
- `progress`: receives `following the mix in <sidecar>` and the summary line, for example `✓ 12 scene sync file(s), 684 entrie(s), 41.3 min — artifacts/<id>/07_sync/`.

With a sidecar, the play covers only the scenes the mix contains, in its order. A `lnvox s5 --chapters 03` mix gives a one-scene play that starts at 0:00. Without one, every script scene is emitted in script order.

**Reads**

- `00_script.json` ([`ScenarioScript`](/library/llm/schemas/)).
- `06_final/<title>.timings.json`, when present ([sidecar format](/library/stages/s5-mix/#timingsjson-sidecar)).
- `03_directed/<scene_id>.json` ([`ChapterDirected`](/library/llm/schemas/)).
- `05_audio/<scene_id>/manifest.json` ([`ChapterAudio`](/library/tts/schema/)). This is always `05_audio`, never `05_audio_v2`.

**Writes** `07_sync/<scene_id>.json` (one [`SceneSync`](#scenesync) per scene), `07_sync/play.json` ([`PlaySync`](#playsync)) and `07_sync/play.srt`.

**Raises**

- `FileNotFoundError` if `00_script.json` is missing, if `mix_title` names no sidecar, or if any scene to emit lacks its directed file or audio manifest (`run lnvox scenario and lnvox s4 first`).
- `ValueError` if the mix was made from `05_audio_v2/` (VibeVoice sessions).
- `ValueError` from [`_beat_times`](#_beat_times) when the beat counts do not match.
- Pydantic `ValidationError` on malformed JSON.

### `build_play_sync`

```python
def build_play_sync(
    script: ScenarioScript,
    chapters: dict[str, ChapterDirected],
    manifests: dict[str, ChapterAudio],
    *,
    intra: float = 0.25,
    staging_pause: float = 1.0,
    inter_scene: float = SCENARIO_INTER_SCENE,
    lead_in: float = 0.0,
) -> PlaySync
```

Walks `script.scenes` in order. Each scene is built at the running cursor, and the next scene starts at `scene.end + inter_scene`. `total_duration_seconds` is the last scene's `end`, so there is no trailing pad. Raises `FileNotFoundError` for a scene missing from `chapters` or `manifests`.

Note that the `lead_in` default here is `0.0`, while [`run`](#run) defaults to `0.35`.

### `build_scene_sync`

```python
def build_scene_sync(
    scene: ScriptScene,
    chapter: ChapterDirected,
    manifest: ChapterAudio,
    *,
    offset: float = 0.0,
    intra: float = 0.25,
    staging_pause: float = 1.0,
    lead_in: float = 0.0,
) -> SceneSync
```

Timed entries for one script scene starting at absolute time `offset`.

- **Dialogue.** Items are numbered 1, 2, … in script order. The *n*-th dialogue item takes line *n*'s `(start, end)` from [`_beat_times`](#_beat_times). It also takes `direction` and `emotion` from that line's first directed chunk, defaulting to `("", "calm")`.
- **Staging.** Starts at the end of the previous dialogue line (or at `offset` for a leading staging item) and ends at the `start` of the next dialogue line (or at the scene end). Every staging item in a run shares the same pause window. Leading and trailing staging items are zero-width, because s5 inserts no pad before the first beat or after the last.
- **Cue.** A zero-duration marker at the end of the previous dialogue line. A staging item between the line and the cue does not move it.

All times are rounded to milliseconds.

### `_beat_times`

```python
def _beat_times(
    chapter: ChapterDirected,
    manifest: ChapterAudio,
    *,
    offset: float,
    intra: float,
    staging_pause: float,
    lead_in: float = 0.0,
) -> tuple[dict[int, tuple[float, float]], float]
```

Replays s5's concat plan for one scene. It returns `{dialogue line index: (start, end)}` and the scene's end time.

- Directed beats are flattened in order and zipped 1:1 with `manifest.beats`.
- Before each beat except the first, the cursor advances by `intra` if the beat's directed group is the same as the previous beat's, else by `staging_pause`.
- The beat's start is the cursor, which is where its lead-in starts. The cursor then advances by `lead_in + duration_seconds`.
- A long line that Stage 3 split into chunks shares one `source_paragraph` (the scenario line index). Its entry spans from the first chunk's start to the last chunk's end.

**Raises** `ValueError` when the number of directed beats is not equal to the number of manifest entries. That happens with VibeVoice session manifests, which have one entry per multi-line session and carry no per-line timing. The message tells you to re-run `lnvox s4` with the dramabox backend.

### `to_srt`

```python
def to_srt(play: PlaySync, *, min_display: float = 1.5) -> str
```

One SRT block per entry, numbered across the whole play:

- dialogue becomes `SPEAKER: text`;
- staging becomes `[text]`;
- cues become `((text))`.

Zero-width entries (cues, leading or trailing staging) are shown for `min_display` seconds. Times are formatted as `HH:MM:SS,mmm`.

## Worked example

The example uses a two-scene play mixed with the launcher's scenario pacing: `intra=0.25`, `staging_pause=1.0`, `inter_scene=0.75`, `lead_in=0.35`.

Scene `01` has three lines. The rendered durations are 1.80 s, 1.10 s and 1.25 s. Lines 1 and 2 form run `01_g00`, and line 3 forms `01_g01`.

- Line 1: `start` 0.000; the cursor advances by 0.35 + 1.80, so `end` is 2.150.
- Line 2: +0.25 intra gives `start` 2.400; `end` is 2.400 + 0.35 + 1.10 = 3.850.
- The staging item fills the 1.0 s pause from 3.850 to 4.850. The cue sits at 3.850.
- Line 3: `start` 4.850; `end` is 4.850 + 0.35 + 1.25 = 6.450, which is also the scene end.

Scene `02` starts at 6.450 + 0.75 = 7.200.

```json title="artifacts/la-lanterne/07_sync/01.json"
{
  "scene_id": "01",
  "title": "Scène 1 — La porte",
  "start": 0.0,
  "end": 6.45,
  "entries": [
    {"start": 0.0, "end": 2.15, "type": "dialogue", "speaker": "Gardien",
     "text": "Qui va là ?", "direction": "méfiant, voix basse", "emotion": "fear"},
    {"start": 2.4, "end": 3.85, "type": "dialogue", "speaker": "Voyageur",
     "text": "Un ami.", "direction": "las, apaisant", "emotion": "calm"},
    {"start": 3.85, "end": 4.85, "type": "staging", "speaker": "",
     "text": "Il lève sa lanterne vers la porte.", "direction": "", "emotion": ""},
    {"start": 3.85, "end": 3.85, "type": "cue", "speaker": "",
     "text": "SON 3", "direction": "", "emotion": ""},
    {"start": 4.85, "end": 6.45, "type": "dialogue", "speaker": "Gardien",
     "text": "Approchez.", "direction": "sec", "emotion": "calm"}
  ]
}
```

`play.json` wraps the scenes with the pacing values. The `model_dump_json(indent=2)` output is shown compacted here, and scene `01`'s entries are left out because they are identical to `01.json`:

```json title="artifacts/la-lanterne/07_sync/play.json"
{
  "scenario_id": "la-lanterne",
  "title": "La Lanterne",
  "total_duration_seconds": 8.45,
  "intra_silence": 0.25,
  "staging_pause": 1.0,
  "inter_scene_silence": 0.75,
  "lead_in_silence": 0.35,
  "scenes": [
    {"scene_id": "01", "title": "Scène 1 — La porte", "start": 0.0, "end": 6.45,
     "entries": ["… same five entries as 01.json …"]},
    {"scene_id": "02", "title": "Scène 2 — La nuit", "start": 7.2, "end": 8.45,
     "entries": [
       {"start": 7.2, "end": 7.2, "type": "staging", "speaker": "",
        "text": "La nuit tombe.", "direction": "", "emotion": ""},
       {"start": 7.2, "end": 8.45, "type": "dialogue", "speaker": "Tous",
        "text": "Silence !", "direction": "en chœur", "emotion": "surprise"}
     ]}
  ]
}
```

```text title="artifacts/la-lanterne/07_sync/play.srt"
1
00:00:00,000 --> 00:00:02,150
Gardien: Qui va là ?

2
00:00:02,400 --> 00:00:03,850
Voyageur: Un ami.

3
00:00:03,850 --> 00:00:04,850
[Il lève sa lanterne vers la porte.]

4
00:00:03,850 --> 00:00:05,350
((SON 3))

5
00:00:04,850 --> 00:00:06,450
Gardien: Approchez.

6
00:00:07,200 --> 00:00:08,700
[La nuit tombe.]

7
00:00:07,200 --> 00:00:08,450
Tous: Silence !
```

## Notes

- **Agreement with the m4b.** The m4b's duration and `total_duration_seconds` match when all four pad values and the scene list match, which is the default now that `run` reads them from the sidecar. A book-paced hand mix (`lnvox s5 <id>` without `--inter-chapter 0.75 --lead-in 0.35`) is described correctly too, but it is not play-paced.
- **Scene order.** With a sidecar the scenes follow the mix order, so they match the m4b whatever the ids. Without one, script order is used. It equals s5's sorted directory order because scene ids are zero-padded (`01`, `02`, …), up to 99 scenes.
- **No re-anchoring.** Unlike Stage 6, scenario-sync does not start each scene at the offset s5 measured for it. It replays its own arithmetic, which agrees with s5 to within millisecond rounding.
- **Edits are cheap.** Stage 4's content-hash cache re-renders only changed lines, and this emitter recomputes everything from the manifests. Re-running after a script edit is quick and keeps the file consistent.
- **Tests.** `tests/test_scenario.py` covers intra and staging pads, zero-width cues, lead-in shifting speech but not `start`, split-line regrouping, scene offsets, the per-beat mismatch guard and SRT output. `tests/test_mix_layout.py` covers following the sidecar's pads and scene list, and flag overrides.

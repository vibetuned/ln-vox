---
title: lnvox.stages.scenario
description: Scenario mode Stage S. Per-line emotion and script-language cues, parenthetical stripping, staging-delimited DirectedScenes, group lines on the Narrator voice.
sidebar:
  order: 5
  label: scenario
---

Stage S is the direction pass for theater scripts. It reads the structured script (`00_script.json`, from [ingest-scenario](/library/ingest/scenario/)) and writes the same `03_directed/*.json` files the novel pipeline produces, so [s4](/library/stages/s4-tts/) and [s5](/library/stages/s5-mix/) run unchanged. Each script scene becomes one `ChapterDirected`. Inside it, each run of dialogue between staging directions becomes one `DirectedScene`. s5's inter-scene pad therefore lands exactly where a staged action happens. `lnvox scenario` ([stage page](/stages/scenario/)) calls `run()`, and [scenario-sync](/library/stages/scenario-sync/) consumes the output.

## At a glance

| Symbol | Kind | Purpose |
| --- | --- | --- |
| `run` | function | Stage entry point: refresh profiles, direct each scene (cached, casting-aware) |
| `direct_scene` | function | One script scene becomes one `ChapterDirected`, with one LLM call |
| `scene_voice_descriptors` | function | Render speaker → descriptor for a scene: the record a cached scene is checked against |
| `revoice_scene` | function | Rebuild a cached scene's prompts for changed descriptors, keeping emotion and cue (no LLM) |
| `group_dialogue_runs` | function | Dialogue item indices, grouped into staging-delimited runs |
| `spoken_text` | function | Strip parenthetical acting cues from the text sent to TTS |
| `_scene_context` | function | The interleaved scene text and numbered line list sent to the LLM |

It reuses these from [s3_director](/library/stages/s3-director/): `refresh_voice_profiles`, `_profile_lookup`, `_split_long_text`, `format_prompt`, `DIRECTION_SYSTEM`, `NARRATOR_NAME` and `DEFAULT_NARRATOR_DESCRIPTOR`. `is_group_label` comes from [lnvox.ingest.scenario](/library/ingest/scenario/). Schemas (`ScenarioScript`, `ScriptScene`, `ScriptItem`, `ScenarioDirections`, `ScenarioCue`, `Emotion`) are in [lnvox.llm.schemas](/library/llm/schemas/).

## API

### `run`

```python
def run(
    script: ScenarioScript,
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    regen_profiles: bool = False,
    on_scene_done: Callable[[ScriptScene, ChapterDirected], None] | None = None,
) -> list[ChapterDirected]
```

The same casting-aware caching as [s3's `run`](/library/stages/s3-director/#run):

1. **Profiles.** [`refresh_voice_profiles`](/library/stages/s3-director/#refresh_voice_profiles) loads `03_voice_profiles.json` and regenerates only the profiles whose clip or locked descriptor changed in the casting (all of them with `regen_profiles=True`, which is `lnvox scenario --regen-profiles`). A first run, or a run after `voice cast` anchored a previously uncast script, generates them.
2. **Scenes.** For each `script.scenes` entry, it reloads `03_directed/<scene_id>.json` if present and parseable, then compares its `voice_descriptors` record with `scene_voice_descriptors(scene, current profiles)`. Same: reused as-is. A descriptor changed: `revoice_scene` rebuilds those beats' prompts, keeping `emotion` and `cue`, with no LLM call (`[scenario] <scene>: voice descriptor changed — re-voiced prompts`). The speaker set changed (the script was re-ingested): re-directed. No record (a file from before the record existed): treated as current and stamped with the descriptors of the profiles file as it was before the run. A missing or unparseable file is directed with `direct_scene`. `on_scene_done(scene, result)` follows each scene.

**Writes**: `03_voice_profiles.json` and `03_directed/<scene_id>.json`, each only when its content changes.

### `direct_scene`

```python
def direct_scene(
    client: LLMClient, scene: ScriptScene, profiles: VoiceProfileList
) -> ChapterDirected
```

**One LLM call per script scene**, made only if the scene has at least one dialogue item:

- The prompt is `scenario_directions.jinja`, with `scene_id`, `scene_title`, the context from `_scene_context` and the numbered dialogue list.
- The schema is `ScenarioDirections`, which is `[{line, emotion, cue}]`:
  - `emotion` is one of the 7 values `calm`, `joy`, `sadness`, `anger`, `fear`, `surprise`, `disgust`;
  - `cue` is 2–6 words **in the script's language**. The prompt itself stays in English.
- The budget is `budget_for(desired=max(2048, 50 * lines + 512), floor=2048)`.
- **A bad or unparseable answer falls back to `("calm", "")` for every line** in the scene. `emotion` is a strict `Literal`, so a single out-of-enum value that survives the client's 3 attempts discards the whole scene's cues. A transport error (`is_transport_error`: server unreachable, timeout, 5xx) is re-raised instead, so the step fails and the launcher's `ensure_vllm` retry can restart the server.

Then, for each run from `group_dialogue_runs(scene)` (run index `g`, from 0), and for each dialogue item in it, the line counter `line_no` advances. It is 1-based across the whole scene and matches the LLM's numbering.

- `emotion, cue = cues.get(line_no, ("calm", ""))`.
- The render speaker and descriptor come from `_voice_for(item.speaker, profile_map)`:
- **Group lines.** If `is_group_label(item.speaker)` holds, the beat renders as `speaker = "Narrator"` with the narrator descriptor. The label stems are `tous`, `toutes`, `tous ensemble`, `ensemble`, `all` and `choeur`. Choral audio is out of scope (DESIGN.md §17.6). The sync file keeps the original label from `00_script.json`.
- **Otherwise** `speaker = item.speaker`, and the descriptor is the speaker's profile or, failing that, the **narrator descriptor**. (s3 would use `"voice unknown"`.)
- The spoken text is `spoken_text(item.text)`, split with `_split_long_text` into chunks of at most 375 characters. Each chunk becomes a beat:

| Field | Value |
| --- | --- |
| `type` | `"dialogue"` (scenario beats are never narration) |
| `text`, `source_span` | The chunk, with parenthetical cues already stripped |
| `speaker` | As above |
| `direction` | The script-language `cue`, meant for humans and the sync file. Not sent to TTS |
| `emotion` | The enum value |
| `prompt` | `format_prompt(f"{descriptor}, {emotion}", chunk)`, English descriptor plus the emotion word |
| `source_paragraph` | `line_no`, which lets scenario-sync regroup the split chunks of one line into one sync entry |

A run that produces beats becomes `DirectedScene(scene_id=f"{scene.scene_id}_g{g:02d}", location_hint=scene.title)`. The result is `ChapterDirected(chapter_id=scene.scene_id, voice_descriptors=scene_voice_descriptors(scene, profile_map), …)`. A scene with no dialogue yields `scenes: []`.

### `scene_voice_descriptors`

```python
def scene_voice_descriptors(
    scene: ScriptScene, profile_map: dict[str, str]
) -> dict[str, str]
```

`{render speaker: descriptor}` for every dialogue item, sorted by speaker, resolved exactly as `direct_scene` resolves them: group labels become `"Narrator"` with the narrator descriptor, and a speaker without a profile keeps its label with the narrator descriptor.

### `revoice_scene`

```python
def revoice_scene(
    chapter: ChapterDirected, current: dict[str, str]
) -> ChapterDirected | None
```

Rebuilds `prompt = format_prompt(f"{descriptor}, {beat.emotion}", beat.text)` for every beat whose speaker's descriptor changed. `direction` (the script-language cue) and `emotion` are stored on the beat and don't depend on the voice, so they are kept. Returns `None` when the recorded and current speaker sets differ, or a beat's speaker isn't in `current`; `run` then re-directs the scene.

### `group_dialogue_runs`

```python
def group_dialogue_runs(scene: ScriptScene) -> list[list[int]]
```

Returns the indices of `scene.items` that are dialogue, grouped into runs:

- a `staging` item **closes** the current run, if there is one;
- consecutive staging items close it only once, and staging before any dialogue opens nothing;
- `cue` items never split a run. Cues are instantaneous and take no stage time.

```python
# items: [staging, d"un", d"deux", cue, d"trois", staging, staging, d"quatre"]
group_dialogue_runs(scene)  # [[1, 2, 4], [7]]
```

Each gap between runs becomes an s5 **inter-scene** pause, the "staging pause", with no new s5 code. Its length is `lnvox s5 --inter-scene`, 1.0 s by default. `lnvox scenario-sync --staging-pause` (also 1.0 s by default) must equal it, or the m4b and the sync file disagree. Note the naming: scenario-sync's own `--inter-scene` is the gap between script **scenes**, and it pairs with s5's `--inter-chapter`.

### `spoken_text`

```python
def spoken_text(text: str) -> str
```

Removes parenthetical acting cues, `\s*\([^()]*\)\s*`, from a dialogue line and collapses whitespace. English-play convention puts directions inside the line (`(haughtily) Sir!`), and TTS must not read them aloud. A line that is **nothing but** a parenthetical is returned unchanged, not emptied. The pattern matches one level of non-nested parentheses in a single pass.

```python
spoken_text("(haughtily, turning) Sir! Are you addressing me?")  # "Sir! Are you addressing me?"
spoken_text("Ye look it. (Taking his arm kindly) See here, me lad.")  # "Ye look it. See here, me lad."
spoken_text("(un long silence)")  # "(un long silence)"
```

The stripping applies only to what gets rendered. The direction LLM still sees the full line in its context, and `00_script.json` and the sync file keep it verbatim.

### `_scene_context`

```python
def _scene_context(scene: ScriptScene) -> tuple[str, str, int]
```

Returns `(context, numbered_dialogue, dialogue_count)`. In the context, dialogue appears as `[L<n>] [<speaker>] "<text>"` (raw text, parentheticals included) and staging as `(didascalie) <text>`. Cue items are omitted because they carry no performance information.

## Artifacts

Input `00_script.json` (abridged, invented content):

```json
{
  "scenario_id": "ma-piece",
  "title": "Ma pièce",
  "language": "fr",
  "roster": [{"name": "Gardien", "description": "Vieux veilleur de nuit.", "actor": ""}],
  "scenes": [
    {
      "scene_id": "01",
      "title": "Séquence 1 – La porte",
      "items": [
        {"type": "staging", "speaker": "", "text": "Nuit. Une lanterne."},
        {"type": "dialogue", "speaker": "Gardien", "text": "(à voix basse) Qui va là ?"},
        {"type": "dialogue", "speaker": "Tous", "text": "Personne !"},
        {"type": "staging", "speaker": "", "text": "Il lève sa lanterne vers la porte."},
        {"type": "cue", "speaker": "", "text": "SON 3"},
        {"type": "dialogue", "speaker": "Gardien", "text": "Montrez-vous."}
      ]
    }
  ]
}
```

Output `03_directed/01.json`. The leading staging opens nothing, the staging after line 2 closes run `g00`, and the cue doesn't split anything.

```json
{
  "chapter_id": "01",
  "voice_descriptors": {
    "Gardien": "elder male, hoarse low baritone, slow and wary",
    "Narrator": "adult, female, warm clear voice, measured and engaging storyteller"
  },
  "normalized": null,
  "scenes": [
    {
      "scene_id": "01_g00",
      "location_hint": "Séquence 1 – La porte",
      "beats": [
        {
          "type": "dialogue",
          "text": "Qui va là ?",
          "speaker": "Gardien",
          "direction": "méfiant, voix basse",
          "prompt": "(elder male - hoarse low baritone - slow and wary - fear) \"Qui va là ?\"",
          "source_span": "Qui va là ?",
          "source_paragraph": 1,
          "emotion": "fear"
        },
        {
          "type": "dialogue",
          "text": "Personne !",
          "speaker": "Narrator",
          "direction": "en chœur, trop vite",
          "prompt": "(adult - female - warm clear voice - measured and engaging storyteller - surprise) \"Personne !\"",
          "source_span": "Personne !",
          "source_paragraph": 2,
          "emotion": "surprise"
        }
      ]
    },
    {
      "scene_id": "01_g01",
      "location_hint": "Séquence 1 – La porte",
      "beats": [
        {
          "type": "dialogue",
          "text": "Montrez-vous.",
          "speaker": "Gardien",
          "direction": "ferme, sans trembler",
          "prompt": "(elder male - hoarse low baritone - slow and wary - anger) \"Montrez-vous.\"",
          "source_span": "Montrez-vous.",
          "source_paragraph": 3,
          "emotion": "anger"
        }
      ]
    }
  ]
}
```

## Notes

- **Not fully verbatim.** DESIGN.md §17.3 says `text` stays verbatim. In the code, `text` and `source_span` both have parenthetical cues stripped. The verbatim line survives in `00_script.json`, and scenario-sync reads the line text from there.
- **Chapter id is the scene id.** s4 renders into `05_audio/<scene_id>/`, and s5 uses the scene titles from `00_text.jsonl` (written by ingest-scenario) as chapter markers.
- **Silent degradation.** A cue answer that is unusable (bad JSON, invalid emotion) is swallowed and cached as all-`calm` with empty cues. A dead server is not: it fails the step. If a scene's `direction` fields are all `""`, delete `03_directed/<scene_id>.json` and re-run.
- Design: DESIGN.md §17.3 (pipeline and artifact mapping), §17.6 (group lines, direction language).

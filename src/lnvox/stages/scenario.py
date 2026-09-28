"""Scenario mode direction pass (DESIGN.md §17.3).

Turns the structured verbatim script (`00_script.json`) into the same
`03_directed/*.json` artifacts the novel pipeline produces, so s4/s5 run
unchanged. One ChapterDirected per script scene; within it, one
DirectedScene per staging-delimited run of dialogue — s5's inter-scene pad
then lands exactly where a staged action happens (the "staging pause").

Per line the LLM adds an `emotion` (the §16.7 7-value enum) and a short
`cue` in the script's language. The English TTS prompt is
`format_prompt(f"{descriptor}, {emotion}", text)`, i.e.
`(<descriptor - emotion, lowercased, commas → " - ">) "text"`; the
human-readable French cue travels in `DirectedBeat.direction` and ends up in
the sync file. Text stays verbatim.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from lnvox.llm.client import LLMClient, is_transport_error
from lnvox.llm.schemas import (
    ChapterDirected,
    CharacterList,
    DirectedBeat,
    DirectedScene,
    ScenarioDirections,
    ScenarioScript,
    ScriptScene,
    VoiceProfileList,
)
from lnvox.stages.s3_director import (
    DEFAULT_NARRATOR_DESCRIPTOR,
    DIRECTION_SYSTEM,
    NARRATOR_NAME,
    _profile_lookup,
    _split_long_text,
    format_prompt,
    refresh_voice_profiles,
)
from lnvox.ingest.scenario import is_group_label
from lnvox.voices.schema import BookCasting, Voicebank


_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*")


def spoken_text(text: str) -> str:
    """Strip parenthetical acting cues from a dialogue line's SPOKEN text.

    English-play convention puts per-line directions in parentheses inside
    the line (`(haughtily) Sir!`) — they are cues for the actor, not speech,
    so the TTS must not read them. They stay verbatim in `00_script.json`
    and the sync file (same text-vs-source split lecture mode uses), and the
    direction LLM still sees them in its scene context. A line that is
    NOTHING BUT a parenthetical is returned unchanged rather than emptied.
    """
    cleaned = " ".join(_PARENTHETICAL.sub(" ", text).split())
    return cleaned or text


def group_dialogue_runs(scene: ScriptScene) -> list[list[int]]:
    """Item indices of dialogue, grouped into staging-delimited runs.

    Staging closes the current run (consecutive staging items close it once);
    cues never split — they are instantaneous and cost no stage time.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    for i, item in enumerate(scene.items):
        if item.type == "dialogue":
            current.append(i)
        elif item.type == "staging" and current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _scene_context(scene: ScriptScene) -> tuple[str, str, int]:
    """(interleaved context, numbered dialogue list, dialogue count)."""
    context: list[str] = []
    numbered: list[str] = []
    line_no = 0
    for item in scene.items:
        if item.type == "dialogue":
            line_no += 1
            context.append(f'[L{line_no}] [{item.speaker}] "{item.text}"')
            numbered.append(f'{line_no}. [{item.speaker}] "{item.text}"')
        elif item.type == "staging":
            context.append(f"(didascalie) {item.text}")
        # cues carry no performance information — omitted from context
    return "\n\n".join(context), "\n".join(numbered), line_no


def _voice_for(speaker: str, profile_map: dict[str, str]) -> tuple[str, str]:
    """(render speaker, voice descriptor) for one script speaker label."""
    narrator_descriptor = profile_map.get(NARRATOR_NAME, DEFAULT_NARRATOR_DESCRIPTOR)
    if is_group_label(speaker):
        # Group lines render with the Narrator voice (§17.6); the sync
        # file keeps the original label from 00_script.json.
        return NARRATOR_NAME, narrator_descriptor
    return speaker, profile_map.get(speaker) or narrator_descriptor


def scene_voice_descriptors(
    scene: ScriptScene, profile_map: dict[str, str]
) -> dict[str, str]:
    """Render speaker → descriptor for every voice in the scene — the record
    `ChapterDirected.voice_descriptors` is checked against on a re-run."""
    voices = dict(
        _voice_for(item.speaker, profile_map)
        for item in scene.items
        if item.type == "dialogue"
    )
    return dict(sorted(voices.items()))


def revoice_scene(
    chapter: ChapterDirected, current: dict[str, str]
) -> ChapterDirected | None:
    """Re-compose a directed scene's prompts for changed voice descriptors.

    Emotion and cue come from the script alone and are stored on each beat,
    so the prompt is rebuilt without an LLM call. None when the speaker set
    differs (the script changed) — the caller re-directs the scene.
    """
    recorded = chapter.voice_descriptors or {}
    if recorded.keys() != current.keys():
        return None
    scenes: list[DirectedScene] = []
    for s in chapter.scenes:
        beats: list[DirectedBeat] = []
        for b in s.beats:
            if b.speaker not in current:
                return None
            if current[b.speaker] == recorded[b.speaker]:
                beats.append(b)
                continue
            prompt = format_prompt(f"{current[b.speaker]}, {b.emotion}", b.text)
            beats.append(b.model_copy(update={"prompt": prompt}))
        scenes.append(s.model_copy(update={"beats": beats}))
    return chapter.model_copy(
        update={"voice_descriptors": dict(current), "scenes": scenes}
    )


def direct_scene(
    client: LLMClient, scene: ScriptScene, profiles: VoiceProfileList
) -> ChapterDirected:
    """One script scene → one ChapterDirected (chapter_id == scene_id)."""
    scene_context, numbered_dialogue, n_lines = _scene_context(scene)

    cues_by_line: dict[int, tuple[str, str]] = {}  # line → (emotion, cue)
    if n_lines:
        user = client.render(
            "scenario_directions.jinja",
            scene_id=scene.scene_id,
            scene_title=scene.title,
            scene_context=scene_context,
            numbered_dialogue=numbered_dialogue,
        )
        budget = client.budget_for(
            system=DIRECTION_SYSTEM,
            user=user,
            desired=max(2048, 50 * n_lines + 512),
            floor=2048,
        )
        try:
            result = client.structured(
                system=DIRECTION_SYSTEM,
                user=user,
                schema=ScenarioDirections,
                max_tokens=budget,
            )
            cues_by_line = {
                d.line: (d.emotion, d.cue.strip()) for d in result.directions
            }
        except Exception as e:
            # A bad / unparseable answer only costs this scene its cues (all
            # lines "calm"). A dead server must fail the step instead, or
            # every remaining scene caches cue-less and the launcher's
            # `ensure_vllm` retry never runs.
            if is_transport_error(e):
                raise
            cues_by_line = {}

    profile_map = _profile_lookup(profiles)

    directed_scenes: list[DirectedScene] = []
    line_no = 0
    for g, run_indices in enumerate(group_dialogue_runs(scene)):
        beats: list[DirectedBeat] = []
        for i in run_indices:
            item = scene.items[i]
            line_no += 1
            emotion, cue = cues_by_line.get(line_no, ("calm", ""))
            render_speaker, descriptor = _voice_for(item.speaker, profile_map)
            for chunk in _split_long_text(spoken_text(item.text)):
                beats.append(
                    DirectedBeat(
                        type="dialogue",
                        text=chunk,
                        speaker=render_speaker,
                        direction=cue,
                        emotion=emotion,
                        prompt=format_prompt(f"{descriptor}, {emotion}", chunk),
                        source_span=chunk,
                        source_paragraph=line_no,
                    )
                )
        if beats:
            directed_scenes.append(
                DirectedScene(
                    scene_id=f"{scene.scene_id}_g{g:02d}",
                    location_hint=scene.title,
                    beats=beats,
                )
            )
    return ChapterDirected(
        chapter_id=scene.scene_id,
        voice_descriptors=scene_voice_descriptors(scene, profile_map),
        scenes=directed_scenes,
    )


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
) -> list[ChapterDirected]:
    """Direct every scene; idempotent per scene like s3 (cached on disk).

    Casting-aware like s3: profiles regenerate only for recast characters
    (all of them with `regen_profiles`), and a cached scene whose voices'
    descriptors changed is re-voiced (prompts rebuilt, emotion/cue kept).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles, previous = refresh_voice_profiles(
        client,
        cast,
        output_dir / "03_voice_profiles.json",
        casting=casting,
        voicebank=voicebank,
        regen=regen_profiles,
    )
    profile_map = _profile_lookup(profiles)
    # Scenes directed before descriptor records existed are dated from the
    # profiles file as it was on disk (see s3_director.run).
    legacy_map = _profile_lookup(previous) if previous is not None else profile_map

    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    results: list[ChapterDirected] = []
    for scene in script.scenes:
        cached_path = directed_dir / f"{scene.scene_id}.json"
        result: ChapterDirected | None = None
        dirty = False
        if cached_path.exists():
            try:
                result = ChapterDirected.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                result = None
        if result is not None:
            current = scene_voice_descriptors(scene, profile_map)
            if result.voice_descriptors is None:
                result.voice_descriptors = scene_voice_descriptors(scene, legacy_map)
                dirty = True
            if result.voice_descriptors != current:
                revoiced = revoice_scene(result, current)
                if revoiced is not None:
                    print(f"  [scenario] {scene.scene_id}: voice descriptor changed — re-voiced prompts")
                    result = revoiced
                    dirty = True
                else:
                    print(f"  [scenario] {scene.scene_id}: speaker set changed — re-directing")
                    result = None
        if result is None:
            result = direct_scene(client, scene, profiles)
            dirty = True
        if dirty:
            cached_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        results.append(result)
        if on_scene_done:
            on_scene_done(scene, result)
    return results

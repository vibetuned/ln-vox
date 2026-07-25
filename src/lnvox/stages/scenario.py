"""Scenario mode direction pass (DESIGN.md §17.3).

Turns the structured verbatim script (`00_script.json`) into the same
`03_directed/*.json` artifacts the novel pipeline produces, so s4/s5 run
unchanged. One ChapterDirected per script scene; within it, one
DirectedScene per staging-delimited run of dialogue — s5's inter-scene pad
then lands exactly where a staged action happens (the "staging pause").

Per line the LLM adds an `emotion` (the §16.7 7-value enum) and a short
`cue` in the script's language. The English TTS prompt is composed as
`(descriptor - emotion) "text"`; the human-readable French cue travels in
`DirectedBeat.direction` and ends up in the sync file. Text stays verbatim.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from lnvox.llm.client import LLMClient
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
    generate_voice_profiles,
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
        except Exception:
            cues_by_line = {}

    profile_map = _profile_lookup(profiles)
    narrator_descriptor = profile_map.get(NARRATOR_NAME, DEFAULT_NARRATOR_DESCRIPTOR)

    directed_scenes: list[DirectedScene] = []
    line_no = 0
    for g, run_indices in enumerate(group_dialogue_runs(scene)):
        beats: list[DirectedBeat] = []
        for i in run_indices:
            item = scene.items[i]
            line_no += 1
            emotion, cue = cues_by_line.get(line_no, ("calm", ""))
            if is_group_label(item.speaker):
                # Group lines render with the Narrator voice (§17.6); the sync
                # file keeps the original label from 00_script.json.
                render_speaker = NARRATOR_NAME
                descriptor = narrator_descriptor
            else:
                render_speaker = item.speaker
                descriptor = profile_map.get(item.speaker) or narrator_descriptor
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
    return ChapterDirected(chapter_id=scene.scene_id, scenes=directed_scenes)


def run(
    script: ScenarioScript,
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    on_scene_done: Callable[[ScriptScene, ChapterDirected], None] | None = None,
) -> list[ChapterDirected]:
    """Direct every scene; idempotent per scene like s3 (cached on disk)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = output_dir / "03_voice_profiles.json"
    if profiles_path.exists():
        profiles = VoiceProfileList.model_validate_json(
            profiles_path.read_text(encoding="utf-8")
        )
    else:
        profiles = generate_voice_profiles(
            client, cast, casting=casting, voicebank=voicebank
        )
        profiles_path.write_text(profiles.model_dump_json(indent=2), encoding="utf-8")

    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    results: list[ChapterDirected] = []
    for scene in script.scenes:
        cached_path = directed_dir / f"{scene.scene_id}.json"
        result: ChapterDirected | None = None
        if cached_path.exists():
            try:
                result = ChapterDirected.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                result = None
        if result is None:
            result = direct_scene(client, scene, profiles)
            cached_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        results.append(result)
        if on_scene_done:
            on_scene_done(scene, result)
    return results

import json
import re
from pathlib import Path

from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import (
    Beat,
    ChapterDirected,
    ChapterScenes,
    CharacterList,
    DirectedBeat,
    DirectedScene,
    Scene,
    SceneDirections,
    VoiceProfile,
    VoiceProfileList,
)
from lnvox.voices.schema import BookCasting, Voicebank


VOICE_SYSTEM = (
    "You write short voice descriptors for an audiobook cast. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)

DIRECTION_SYSTEM = (
    "You direct vocal performances for an audiobook scene. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)

NARRATOR_NAME = "Narrator"
# Default narrator profile when nothing else is specified (first volume,
# no --narrator-clip, no prior-volume reuse): adult British female. The
# matcher uses gender="female" / age_band="adult" / accent_keywords=["england"]
# to filter the voicebank, and this descriptor lands inside the Dramabox
# bracket prefix.
DEFAULT_NARRATOR_DESCRIPTOR = (
    "adult, British, female, warm clear voice, measured and engaging storyteller"
)
DEFAULT_NARRATOR_GENDER = "female"
DEFAULT_NARRATOR_AGE = "adult"
DEFAULT_NARRATOR_ACCENT = "england"

# Dramabox recommends 20-60 seconds per generation; English narration runs
# roughly 12 chars/second so we cap merged beats at ~500 chars (~40s audio).
MAX_MERGED_BEAT_CHARS = 500


# ---------------- voice profiles ----------------


def generate_voice_profiles(
    client: LLMClient,
    cast: CharacterList,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
) -> VoiceProfileList:
    """Write a profile per character, biased to match the assigned voice clip.

    Two paths, per character:
      - If the CharacterCasting carries a non-empty `voice_descriptor`
        (e.g. Narrator with --narrator-clip or a reused prior-volume clip),
        use that VERBATIM and skip the LLM. Fills in gender / age_band /
        accent straight from the assigned clip's metadata.
      - Otherwise, ask the LLM to compose a descriptor that's CONSISTENT
        with the assigned clip's gender / age_band / accent while reflecting
        the character's personality. Then post-process the LLM output to
        snap gender / age_band / accent back onto the clip's actual values
        (defensive — the LLM occasionally drifts).
    """
    clip_by_id: dict = {}
    if voicebank:
        clip_by_id = {c.id: c for c in voicebank.clips}

    casting_by_name: dict = {}
    if casting:
        casting_by_name = {c.character_name: c for c in casting.castings}

    def _clip_for(name: str):
        cst = casting_by_name.get(name)
        if not cst or not cst.assigned_clip_id:
            return None
        return clip_by_id.get(cst.assigned_clip_id)

    # Pre-built profiles for characters whose voice_descriptor is already
    # locked in (Narrator override / prior reuse). These skip the LLM.
    locked_profiles: dict[str, VoiceProfile] = {}
    cast_for_llm: list[dict] = []

    def _consider(name: str, description: str) -> None:
        clip = _clip_for(name)
        cst = casting_by_name.get(name)
        if cst and cst.voice_descriptor and clip:
            locked_profiles[name] = VoiceProfile(
                name=name,
                voice_descriptor=cst.voice_descriptor,
                accent=clip.accent,
                gender=clip.gender,
                age_band=clip.age_band,
            )
            return
        cast_for_llm.append(
            {
                "name": name,
                "description": description,
                "assigned_clip": (
                    {
                        "id": clip.id,
                        "gender": clip.gender,
                        "age_band": clip.age_band,
                        "accent": clip.accent,
                    }
                    if clip
                    else None
                ),
            }
        )

    for c in cast.characters:
        _consider(c.name, c.description)

    if NARRATOR_NAME not in {c.name for c in cast.characters}:
        _consider(
            NARRATOR_NAME,
            "Neutral audiobook narrator for a modern action/fantasy young-adult novel.",
        )

    # If everything was locked-in, skip the LLM entirely.
    if cast_for_llm:
        cast_with_clips_json = json.dumps(
            cast_for_llm, ensure_ascii=False, indent=2
        )
        user = client.render(
            "voice_profiles.jinja",
            cast_with_clips_json=cast_with_clips_json,
        )
        llm_result = client.structured(
            system=VOICE_SYSTEM,
            user=user,
            schema=VoiceProfileList,
            max_tokens=2048,
        )
    else:
        llm_result = VoiceProfileList(profiles=[])

    # Defensive snap: force gender / age_band / accent on LLM-generated
    # entries to match the assigned clip, regardless of what the LLM wrote.
    # Also reject obvious garbage like "N/A" / "unknown" / empty strings —
    # those happen when the LLM is asked to write a profile without a clip
    # to anchor it; fall back to a clip-derived or default descriptor.
    _NA_VALUES = {"", "n/a", "na", "unknown", "none", "tbd"}
    snapped: list[VoiceProfile] = []
    for p in llm_result.profiles:
        clip = _clip_for(p.name)
        descriptor = p.voice_descriptor.strip()
        if descriptor.lower() in _NA_VALUES:
            # LLM produced garbage. Recover with whatever info we have.
            from lnvox.voices.matcher import descriptor_from_clip

            descriptor = (
                descriptor_from_clip(clip)
                if clip
                else DEFAULT_NARRATOR_DESCRIPTOR
            )
        if clip:
            snapped.append(
                p.model_copy(
                    update={
                        "voice_descriptor": descriptor,
                        "accent": clip.accent,
                        "gender": clip.gender,
                        "age_band": clip.age_band,
                    }
                )
            )
        else:
            snapped.append(p.model_copy(update={"voice_descriptor": descriptor}))

    # Merge in the locked profiles (Narrator etc.). Preserve cast order.
    profiles_by_name = {p.name: p for p in snapped}
    profiles_by_name.update(locked_profiles)

    ordered: list[VoiceProfile] = []
    seen: set[str] = set()
    for c in cast.characters:
        if c.name in profiles_by_name:
            ordered.append(profiles_by_name[c.name])
            seen.add(c.name)
    if NARRATOR_NAME in profiles_by_name and NARRATOR_NAME not in seen:
        ordered.append(profiles_by_name[NARRATOR_NAME])
        seen.add(NARRATOR_NAME)
    # Any extras the LLM dreamed up land at the end.
    for name, p in profiles_by_name.items():
        if name not in seen:
            ordered.append(p)

    # Last-resort Narrator stub.
    if not any(p.name == NARRATOR_NAME for p in ordered):
        ordered.append(
            VoiceProfile(name=NARRATOR_NAME, voice_descriptor=DEFAULT_NARRATOR_DESCRIPTOR)
        )

    return VoiceProfileList(profiles=ordered)


def _profile_lookup(profiles: VoiceProfileList) -> dict[str, str]:
    return {p.name: p.voice_descriptor for p in profiles.profiles}


# ---------------- merge consecutive same-speaker beats ----------------


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _split_long_text(text: str, max_chars: int = MAX_MERGED_BEAT_CHARS) -> list[str]:
    """Break `text` at sentence boundaries into chunks ≤ `max_chars`.

    Sentences are split on `.!?` followed by whitespace; punctuation is kept
    with the preceding sentence. Sentences longer than `max_chars` on their
    own are emitted as-is (we don't split mid-sentence — Dramabox handles
    a slightly oversized sentence better than a chopped one).
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    sentences = [s for s in _SENTENCE_BOUNDARY.split(text) if s]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for s in sentences:
        # Flush before adding if the new sentence would push us over.
        if current and current_len + 1 + len(s) > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
        current.append(s)
        current_len += len(s) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def _merge_same_speaker(beats: list[Beat]) -> list[Beat]:
    """Fuse adjacent beats voiced by the same speaker into a single beat.

    - Consecutive narration beats fuse (one Narrator voices them all).
    - Consecutive named-speaker dialogue beats fuse (same character, same run).
    - "Unknown" dialogue beats DO NOT fuse, since two consecutive unattributable
      lines may belong to different characters.
    - A merge is REJECTED if the resulting beat would exceed
      MAX_MERGED_BEAT_CHARS (~40 seconds at typical narration pace).
    - After merging, any beat (whether merged or carried in unchanged from s2)
      that still exceeds MAX_MERGED_BEAT_CHARS is split at sentence boundaries.
      Dramabox OOMs on very long inputs (>~3000 chars triggers >24GB VRAM
      allocation), so this final split pass is mandatory regardless of how
      the long beat arrived.
    """
    merged: list[Beat] = []
    for beat in beats:
        can_merge = False
        if merged and merged[-1].type == beat.type:
            joined_len = len(merged[-1].text) + 1 + len(beat.text)
            within_cap = joined_len <= MAX_MERGED_BEAT_CHARS
            if within_cap:
                if beat.type == "narration":
                    can_merge = True
                elif (
                    beat.type == "dialogue"
                    and beat.speaker
                    and beat.speaker != "Unknown"
                    and merged[-1].speaker == beat.speaker
                ):
                    can_merge = True
        if can_merge:
            merged[-1] = merged[-1].model_copy(
                update={"text": f"{merged[-1].text} {beat.text}".strip()}
            )
        else:
            merged.append(beat.model_copy(deep=True))

    # Final pass: split any beat still over the cap.
    final: list[Beat] = []
    for beat in merged:
        if len(beat.text) <= MAX_MERGED_BEAT_CHARS:
            final.append(beat)
            continue
        for chunk in _split_long_text(beat.text):
            final.append(beat.model_copy(update={"text": chunk}, deep=True))
    return final


# ---------------- direction generation ----------------


def _build_scene_context(beats: list[Beat]) -> tuple[str, str, list[int]]:
    """Return (scene_context, numbered_dialogue, dialogue_beat_indices).

    `scene_context` interleaves narration and dialogue with speaker tags so
    the LLM can read the scene in order. `numbered_dialogue` is the explicit
    1-indexed list of lines the LLM must produce cues for. The third return
    value maps each dialogue index back to its position in `beats`.
    """
    context_lines: list[str] = []
    numbered_lines: list[str] = []
    dialogue_indices: list[int] = []
    line_no = 0
    for i, b in enumerate(beats):
        if b.type == "narration":
            context_lines.append(f"(narration) {b.text}")
        else:
            line_no += 1
            speaker = b.speaker or "Unknown"
            context_lines.append(f"[L{line_no}] [{speaker}] \"{b.text}\"")
            numbered_lines.append(f"{line_no}. [{speaker}] \"{b.text}\"")
            dialogue_indices.append(i)
    return "\n\n".join(context_lines), "\n".join(numbered_lines), dialogue_indices


def direct_scene(
    client: LLMClient, scene: Scene, profiles: VoiceProfileList
) -> DirectedScene:
    """Produce a DirectedScene from a Scene + voice profiles."""
    merged_beats = _merge_same_speaker(scene.beats)
    scene_context, numbered_dialogue, dialogue_indices = _build_scene_context(merged_beats)

    cues_by_line: dict[int, str] = {}
    if dialogue_indices:
        user = client.render(
            "scene_directions.jinja",
            scene_id=scene.scene_id,
            location_hint=scene.location_hint or "",
            scene_context=scene_context,
            numbered_dialogue=numbered_dialogue,
        )
        # Cue output is small: ~30 tokens per line × line count + JSON overhead.
        budget = max(2048, min(16000, 40 * len(dialogue_indices) + 512))
        try:
            directions = client.structured(
                system=DIRECTION_SYSTEM,
                user=user,
                schema=SceneDirections,
                max_tokens=budget,
            )
            cues_by_line = {d.line: d.cue.strip() for d in directions.directions}
        except Exception:
            cues_by_line = {}

    profile_map = _profile_lookup(profiles)
    directed_beats: list[DirectedBeat] = []
    line_no = 0
    for b in merged_beats:
        if b.type == "narration":
            speaker = NARRATOR_NAME
            descriptor = profile_map.get(NARRATOR_NAME, DEFAULT_NARRATOR_DESCRIPTOR)
            direction = descriptor
        else:
            line_no += 1
            speaker = b.speaker or "Unknown"
            descriptor = profile_map.get(speaker, "voice unknown")
            cue = cues_by_line.get(line_no, "").strip()
            direction = f"{descriptor}, {cue}" if cue else descriptor
        prompt = f'[{direction}]\n"{b.text}"'
        directed_beats.append(
            DirectedBeat(
                type=b.type,
                text=b.text,
                speaker=speaker,
                direction=direction,
                prompt=prompt,
            )
        )

    return DirectedScene(
        scene_id=scene.scene_id,
        location_hint=scene.location_hint,
        beats=directed_beats,
    )


# ---------------- chapter / book orchestration ----------------


def direct_chapter(
    client: LLMClient, chapter_scenes: ChapterScenes, profiles: VoiceProfileList
) -> ChapterDirected:
    directed = [direct_scene(client, s, profiles) for s in chapter_scenes.scenes]
    return ChapterDirected(chapter_id=chapter_scenes.chapter_id, scenes=directed)


def run(
    chapters_scenes: list[ChapterScenes],
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    on_chapter_done=None,
) -> list[ChapterDirected]:
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
        profiles_path.write_text(
            profiles.model_dump_json(indent=2), encoding="utf-8"
        )

    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    results: list[ChapterDirected] = []
    for cs in chapters_scenes:
        result = direct_chapter(client, cs, profiles)
        (directed_dir / f"{cs.chapter_id}.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(cs, result)
    return results

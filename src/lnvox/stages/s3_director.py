import json
import re
from pathlib import Path

from lnvox.llm.client import LLMClient, is_transport_error
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
# roughly 12 chars/second. We cap merged beats at 375 chars (~30s audio):
# beyond that Dramabox's denoise quality drops — noise rises and the voice can
# slur into unintelligible speech — so we stay well under the upper bound.
MAX_MERGED_BEAT_CHARS = 375


def format_prompt(direction: str, text: str) -> str:
    """Compose the Dramabox-ready prompt string.

    Empirically (via the Voicebank Studio TTS Lab) Dramabox reads the direction
    aloud unless it's phrased a specific way: lowercased, with its commas turned
    into ' - ', and wrapped in parentheses. The spoken `text` keeps its own
    punctuation untouched. Used by both the Director (s3) and lecture mode so
    the format stays single-sourced.
    """
    spoken_direction = re.sub(r"\s*,\s*", " - ", direction.lower()).strip()
    return f'({spoken_direction}) "{text}"'


# ---------------- voice profiles ----------------


def _salvage_voice_profiles(raw: str) -> VoiceProfileList | None:
    """Recover complete voice profiles from a truncated/runaway response.

    Mirrors s1's character salvage: walk the `profiles` array, decode objects
    until the first incomplete one, keep the valid entries. Characters missing
    from a partial result degrade gracefully downstream (the director falls back
    to a default descriptor), so a partial list beats a hard crash.
    """
    m = re.search(r'"profiles"\s*:\s*\[', raw)
    if not m:
        return None
    pos = m.end()
    decoder = json.JSONDecoder()
    profiles: list[VoiceProfile] = []
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(raw) or raw[pos] == "]":
            break
        try:
            obj, pos = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            try:
                profiles.append(VoiceProfile.model_validate(obj))
            except Exception:
                continue
    if not profiles:
        return None
    print(
        f"  [salvage] recovered {len(profiles)} voice profile(s) from a "
        f"truncated response"
    )
    return VoiceProfileList(profiles=profiles)


def _written_for(name: str, casting_by_name: dict) -> tuple[str, str]:
    """What a profile for `name` is written for: the casting's
    ``(assigned_clip_id, voice_descriptor)``, '' for each when unset."""
    cst = casting_by_name.get(name)
    if cst is None:
        return "", ""
    return cst.assigned_clip_id or "", cst.voice_descriptor or ""


def generate_voice_profiles(
    client: LLMClient,
    cast: CharacterList,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    keep: dict[str, VoiceProfile] | None = None,
) -> VoiceProfileList:
    """Write a profile per character, biased to match the assigned voice clip.

    Three paths, per character:
      - In `keep` (a cached profile still current for the casting — see
        `refresh_voice_profiles`): reused verbatim, no LLM.
      - If the CharacterCasting carries a non-empty `voice_descriptor`
        (e.g. Narrator with --narrator-clip or a reused prior-volume clip),
        use that VERBATIM and skip the LLM. Fills in gender / age_band /
        accent straight from the assigned clip's metadata.
      - Otherwise, ask the LLM to compose a descriptor that's CONSISTENT
        with the assigned clip's gender / age_band / accent while reflecting
        the character's personality. Then post-process the LLM output to
        snap gender / age_band / accent back onto the clip's actual values
        (defensive — the LLM occasionally drifts).

    Every returned profile is stamped with what it was written for
    (`VoiceProfile.written_for_clip` / `written_for_descriptor`).
    """
    keep = keep or {}
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
        if name in keep:
            return
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
            # A partial refresh (only recast characters) may not include the
            # Narrator; don't tell the model it's there.
            includes_narrator=any(e["name"] == NARRATOR_NAME for e in cast_for_llm),
        )
        # Budget scales with the number of characters needing a profile (~400
        # tokens each is ample for a short descriptor) instead of a fixed cap
        # that truncated mid-JSON on large casts. Clamped to the context window,
        # and salvageable if the model still runs long.
        budget = client.budget_for(
            system=VOICE_SYSTEM,
            user=user,
            desired=max(4096, 400 * len(cast_for_llm) + 1024),
            floor=4096,
        )
        llm_result = client.structured(
            system=VOICE_SYSTEM,
            user=user,
            schema=VoiceProfileList,
            max_tokens=budget,
            salvage=_salvage_voice_profiles,
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

    # Merge in the kept and locked profiles (Narrator etc.). A kept name was
    # never sent to the LLM, so it wins over a same-named entry the model
    # invented anyway. Preserve cast order.
    profiles_by_name = {p.name: p for p in snapped}
    profiles_by_name.update(keep)
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

    stamped: list[VoiceProfile] = []
    for p in ordered:
        clip_id, locked = _written_for(p.name, casting_by_name)
        stamped.append(
            p.model_copy(
                update={"written_for_clip": clip_id, "written_for_descriptor": locked}
            )
        )
    return VoiceProfileList(profiles=stamped)


def _profile_is_current(
    p: VoiceProfile, casting_by_name: dict, clip_by_id: dict
) -> bool:
    """Whether a cached profile still describes the character's cast voice.

    A recorded profile is current while the clip and locked descriptor it was
    written for are unchanged. A legacy profile (no record — the launcher used
    to regenerate profiles on every run) is treated as current unless it
    provably describes a different voice: generation snaps gender / age_band /
    accent onto the assigned clip, and copies a locked descriptor verbatim, so
    a mismatch with the CURRENT clip means the character was recast since.
    """
    clip_id, locked = _written_for(p.name, casting_by_name)
    if p.written_for_clip is not None or p.written_for_descriptor is not None:
        return (p.written_for_clip or "", p.written_for_descriptor or "") == (
            clip_id,
            locked,
        )
    clip = clip_by_id.get(clip_id) if clip_id else None
    if clip is None:
        return True
    if locked and p.voice_descriptor != locked:
        return False
    return (p.gender, p.age_band, p.accent) == (clip.gender, clip.age_band, clip.accent)


def refresh_voice_profiles(
    client: LLMClient,
    cast: CharacterList,
    profiles_path: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    regen: bool = False,
) -> tuple[VoiceProfileList, VoiceProfileList | None]:
    """Load `03_voice_profiles.json`, regenerating only what the casting changed.

    Returns ``(profiles, previous)`` where `previous` is the cache as it was
    on disk (None if absent/unreadable) — callers use it to date chapters
    directed before descriptor records existed. `regen=True`
    (`--regen-profiles`) regenerates every profile. Writes the file only when
    its content changed.
    """
    previous: VoiceProfileList | None = None
    if profiles_path.exists():
        try:
            previous = VoiceProfileList.model_validate_json(
                profiles_path.read_text(encoding="utf-8")
            )
        except Exception:
            previous = None  # malformed / truncated — regenerate

    casting_by_name = {c.character_name: c for c in casting.castings} if casting else {}
    clip_by_id = {c.id: c for c in voicebank.clips} if voicebank else {}
    keep: dict[str, VoiceProfile] = {}
    if previous is not None and not regen:
        keep = {
            p.name: p
            for p in previous.profiles
            if _profile_is_current(p, casting_by_name, clip_by_id)
        }

    needed = [c.name for c in cast.characters]
    if NARRATOR_NAME not in needed:
        needed.append(NARRATOR_NAME)
    stale = [n for n in needed if n not in keep]
    if previous is None or regen:
        print(f"  [profiles] generating voice profiles for {len(needed)} character(s)")
    elif stale:
        print(
            f"  [profiles] regenerating {len(stale)} voice profile(s) whose casting "
            f"changed or is new: {', '.join(stale)}"
        )

    profiles = generate_voice_profiles(
        client, cast, casting=casting, voicebank=voicebank, keep=keep
    )
    if previous is None or profiles.model_dump() != previous.model_dump():
        profiles_path.write_text(profiles.model_dump_json(indent=2), encoding="utf-8")
    return profiles, previous


def _profile_lookup(profiles: VoiceProfileList) -> dict[str, str]:
    return {p.name: p.voice_descriptor for p in profiles.profiles}


def _descriptor_for(speaker: str, profile_map: dict[str, str]) -> str:
    """The voice descriptor a beat by `speaker` is composed with."""
    if speaker == NARRATOR_NAME:
        return profile_map.get(NARRATOR_NAME, DEFAULT_NARRATOR_DESCRIPTOR)
    return profile_map.get(speaker, "voice unknown")


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
            # Concatenate source_spans too — adjacent beats are contiguous in
            # the source, so the joined span stays a (near-)contiguous slice
            # the sync stage can match exactly.
            spans = [s for s in (merged[-1].source_span, beat.source_span) if s]
            merged[-1] = merged[-1].model_copy(
                update={
                    "text": f"{merged[-1].text} {beat.text}".strip(),
                    "source_span": " ".join(spans),
                }
            )
        else:
            merged.append(beat.model_copy(deep=True))

    # Final pass: split any beat still over the cap. Split source_span in
    # parallel so each sub-beat keeps its own grounding; when the span splits
    # into a different count than the text (rare — dialogue with attribution),
    # only the chunks we can pair keep a span, the rest fall back to text-based
    # matching in the sync stage.
    final: list[Beat] = []
    for beat in merged:
        if len(beat.text) <= MAX_MERGED_BEAT_CHARS:
            final.append(beat)
            continue
        text_chunks = _split_long_text(beat.text)
        span_chunks = _split_long_text(beat.source_span) if beat.source_span else []
        for i, chunk in enumerate(text_chunks):
            span = span_chunks[i] if i < len(span_chunks) else ""
            final.append(
                beat.model_copy(update={"text": chunk, "source_span": span}, deep=True)
            )
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
        # Cue output is ~40 tokens per dialogue line + JSON overhead; clamp to
        # the context window so a long scene doesn't truncate and lose all cues.
        budget = client.budget_for(
            system=DIRECTION_SYSTEM,
            user=user,
            desired=max(2048, 40 * len(dialogue_indices) + 512),
            floor=2048,
        )
        try:
            directions = client.structured(
                system=DIRECTION_SYSTEM,
                user=user,
                schema=SceneDirections,
                max_tokens=budget,
            )
            cues_by_line = {d.line: d.cue.strip() for d in directions.directions}
        except Exception as e:
            # A bad / unparseable answer only costs this scene its cues. A dead
            # server must fail the step instead: swallowing it would cache
            # every remaining chapter cue-less, and the launcher's
            # `ensure_vllm` retry would never run.
            if is_transport_error(e):
                raise
            cues_by_line = {}

    profile_map = _profile_lookup(profiles)
    directed_beats: list[DirectedBeat] = []
    line_no = 0
    for b in merged_beats:
        if b.type == "narration":
            speaker = NARRATOR_NAME
            descriptor = _descriptor_for(speaker, profile_map)
            direction = descriptor
        else:
            line_no += 1
            speaker = b.speaker or "Unknown"
            descriptor = _descriptor_for(speaker, profile_map)
            cue = cues_by_line.get(line_no, "").strip()
            direction = f"{descriptor}, {cue}" if cue else descriptor
        prompt = format_prompt(direction, b.text)
        directed_beats.append(
            DirectedBeat(
                type=b.type,
                text=b.text,
                speaker=speaker,
                direction=direction,
                prompt=prompt,
                source_span=b.source_span,
            )
        )

    return DirectedScene(
        scene_id=scene.scene_id,
        location_hint=scene.location_hint,
        beats=directed_beats,
    )


# ---------------- chapter / book orchestration ----------------


def chapter_voice_descriptors(
    chapter_scenes: ChapterScenes, profile_map: dict[str, str]
) -> dict[str, str]:
    """Speaker → descriptor for every voice in the chapter — the record
    `ChapterDirected.voice_descriptors` is checked against on a re-run."""
    speakers = {
        NARRATOR_NAME if b.type == "narration" else (b.speaker or "Unknown")
        for s in chapter_scenes.scenes
        for b in s.beats
    }
    return {sp: _descriptor_for(sp, profile_map) for sp in sorted(speakers)}


def direct_chapter(
    client: LLMClient, chapter_scenes: ChapterScenes, profiles: VoiceProfileList
) -> ChapterDirected:
    directed = [direct_scene(client, s, profiles) for s in chapter_scenes.scenes]
    return ChapterDirected(
        chapter_id=chapter_scenes.chapter_id,
        voice_descriptors=chapter_voice_descriptors(
            chapter_scenes, _profile_lookup(profiles)
        ),
        scenes=directed,
    )


def revoice_chapter(
    chapter: ChapterDirected, current: dict[str, str]
) -> ChapterDirected | None:
    """Re-compose a directed chapter's prompts for changed voice descriptors.

    The performance cues come from the scene text alone (the direction prompt
    never sees a descriptor), so a recast needs no new LLM call: strip the
    recorded descriptor off each changed speaker's `direction`, keep the cue,
    and rebuild `direction` + `prompt` exactly as `direct_scene` would.
    Returns None when that isn't safe — the speaker set differs (the scenes
    changed) or a direction doesn't carry the recorded descriptor (a chapter
    dated from a profiles file it wasn't written with) — and the caller then
    re-directs it through the LLM.
    """
    recorded = chapter.voice_descriptors or {}
    if recorded.keys() != current.keys():
        return None
    changed = {sp for sp in current if current[sp] != recorded[sp]}
    scenes: list[DirectedScene] = []
    for scene in chapter.scenes:
        beats: list[DirectedBeat] = []
        for b in scene.beats:
            if b.speaker not in changed:
                beats.append(b)
                continue
            old = recorded[b.speaker]
            if b.direction == old:
                cue = ""
            elif b.direction.startswith(old + ", "):
                cue = b.direction[len(old) + 2 :]
            else:
                return None
            new = current[b.speaker]
            direction = f"{new}, {cue}" if cue else new
            beats.append(
                b.model_copy(
                    update={"direction": direction, "prompt": format_prompt(direction, b.text)}
                )
            )
        scenes.append(scene.model_copy(update={"beats": beats}))
    return chapter.model_copy(
        update={"voice_descriptors": dict(current), "scenes": scenes}
    )


# Articles carried by a speaker tag but not by the cast entry (or the reverse):
# s2 writes "The Square" for a character s1 canonicalised as "A SQUARE".
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.I)
_SPEAKER_NARRATOR_RE = re.compile(r"(?:the\s+)?narrator", re.I)


def _speaker_key(name: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", " ".join(name.lower().split())).strip()


def build_speaker_index(cast: CharacterList) -> dict[str, str]:
    """Lookup from any spelling of a speaker to its canonical cast name.

    s2 tags dialogue with whatever the prose calls someone, which routinely
    differs from the cast's canonical name by an article or by using an alias.
    Those tags reach s4 as-is, and `_build_speaker_to_clip_path` has no entry
    for them — so the beat renders with NO reference clip and the character
    silently loses their cast voice mid-scene.
    """
    index: dict[str, str] = {}
    for c in cast.characters:
        for spelling in (c.name, *(c.aliases or [])):
            key = _speaker_key(spelling)
            # A canonical name always wins over an alias claiming the same key.
            if key and (key not in index or spelling == c.name):
                index[key] = c.name
    return index


def canonical_speaker(name: str | None, index: dict[str, str]) -> str | None:
    """Resolve one s2 speaker tag onto a cast name, or `None` if unresolvable.

    `None` (narration) passes through untouched — s3 assigns those the
    Narrator downstream.
    """
    if not name:
        return name
    if _SPEAKER_NARRATOR_RE.fullmatch(" ".join(name.lower().split())):
        return NARRATOR_NAME
    return index.get(_speaker_key(name), name)


def canonicalize_scene_speakers(
    chapters_scenes: list[ChapterScenes], cast: CharacterList
) -> tuple[int, dict[str, str]]:
    """Rewrite every beat speaker in place onto its cast name. Returns
    ``(changed_beat_count, {original: canonical})`` for logging."""
    index = build_speaker_index(cast)
    changed = 0
    remapped: dict[str, str] = {}
    for cs in chapters_scenes:
        for scene in cs.scenes:
            for beat in scene.beats:
                original = beat.speaker
                resolved = canonical_speaker(original, index)
                if resolved != original:
                    beat.speaker = resolved
                    changed += 1
                    if original:
                        remapped[original] = resolved or ""
    return changed, remapped


def run(
    chapters_scenes: list[ChapterScenes],
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    regen_profiles: bool = False,
    on_chapter_done=None,
) -> list[ChapterDirected]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fold s2's free-text speaker tags onto cast names BEFORE profiles or
    # directed beats are built, so a tag that differs only by an article or
    # alias keeps its cast voice instead of rendering reference-free.
    changed, remapped = canonicalize_scene_speakers(chapters_scenes, cast)
    if changed:
        sample = ", ".join(f"{k} → {v}" for k, v in list(remapped.items())[:5])
        print(f"  [s3] canonicalised {changed} beat speaker(s): {sample}")

    # Casting-aware: only characters whose clip / locked descriptor changed
    # get a new profile, so a re-run keeps every other descriptor stable.
    profiles, previous = refresh_voice_profiles(
        client,
        cast,
        output_dir / "03_voice_profiles.json",
        casting=casting,
        voicebank=voicebank,
        regen=regen_profiles,
    )
    profile_map = _profile_lookup(profiles)
    # A chapter directed before descriptor records existed is dated from the
    # profiles file as it was on disk — the one the latest chapters were
    # written with — so a recast since then is still detected below.
    legacy_map = _profile_lookup(previous) if previous is not None else profile_map

    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    results: list[ChapterDirected] = []
    for cs in chapters_scenes:
        # Idempotent: a chapter already directed on a prior run is reloaded from
        # disk instead of re-calling the LLM — as long as it was written with
        # its speakers' CURRENT descriptors.
        cached_path = directed_dir / f"{cs.chapter_id}.json"
        result: ChapterDirected | None = None
        dirty = False
        if cached_path.exists():
            try:
                result = ChapterDirected.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                result = None  # malformed / truncated — re-direct
        if result is not None:
            current = chapter_voice_descriptors(cs, profile_map)
            if result.voice_descriptors is None:
                result.voice_descriptors = chapter_voice_descriptors(cs, legacy_map)
                dirty = True
            if result.voice_descriptors != current:
                revoiced = revoice_chapter(result, current)
                stale = sorted(
                    sp for sp in current
                    if (result.voice_descriptors or {}).get(sp) != current[sp]
                )
                if revoiced is not None:
                    print(
                        f"  [s3] {cs.chapter_id}: voice descriptor changed for "
                        f"{', '.join(stale)} — re-voiced prompts (cues kept)"
                    )
                    result = revoiced
                    dirty = True
                else:
                    print(
                        f"  [s3] {cs.chapter_id}: directed with other voices "
                        f"({', '.join(stale) or 'speaker set changed'}) — re-directing"
                    )
                    result = None
        if result is None:
            result = direct_chapter(client, cs, profiles)
            dirty = True
        if dirty:
            cached_path.write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(cs, result)
    return results

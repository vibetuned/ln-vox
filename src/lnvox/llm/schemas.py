from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


Gender = Literal["male", "female", "nonbinary", "unknown"]
AgeBand = Literal["child", "teen", "young_adult", "adult", "elder", "unknown"]


def _coerce_str_to_list(v):
    """Defensive coercion: Gemma occasionally returns a single string where
    the schema expects a list of strings (e.g. `aliases: "Sir Patrick"`).
    Wrap scalars in a singleton list so downstream code doesn't need to care.
    None and empty values normalise to an empty list."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    return v


# `maxItems` bounds are emitted into the JSON schema sent to the model as
# `guided_json` (so a looping/weak model is FORCED by the grammar to close the
# array) but are NOT enforced during validation — `json_schema_extra` is schema
# metadata only. So if the decoding backend ignores `maxItems`, an over-long
# list still validates; we never reject a real result over the cap.
class Character(BaseModel):
    name: str = Field(description="Canonical name as it appears in the text most often")
    aliases: list[str] = Field(default_factory=list, json_schema_extra={"maxItems": 12})
    gender: Gender = "unknown"
    approx_age: AgeBand = "unknown"
    description: str
    evidence: list[str] = Field(default_factory=list, json_schema_extra={"maxItems": 6})

    _coerce_aliases = field_validator("aliases", "evidence", mode="before")(
        _coerce_str_to_list
    )


class CharacterList(BaseModel):
    # Gemma occasionally wraps the list under `cast_of_characters` or `cast`
    # instead of `characters` — `guided_json` doesn't always enforce field
    # names strictly enough on larger models. Accept all three.
    model_config = ConfigDict(populate_by_name=True)
    characters: list[Character] = Field(
        validation_alias=AliasChoices("characters", "cast_of_characters", "cast"),
        json_schema_extra={"maxItems": 60},
    )


class CharacterMergeGroup(BaseModel):
    """One set of draft-cast entries the merge LLM judges to be the same person.

    The LLM returns only these groups (not a rewritten cast); Stage 1 applies
    them to its own clean clusters, so no descriptions or aliases come back from
    the model. See `s1_characters.merge_clusters`."""

    canonical: str = Field(
        description="Preferred display name for the merged character (one of `names`)"
    )
    names: list[str] = Field(
        default_factory=list,
        description="Two or more names from the provided list that denote ONE person",
    )

    _coerce_names = field_validator("names", mode="before")(_coerce_str_to_list)


class CharacterMergeProposal(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    merges: list[CharacterMergeGroup] = Field(
        default_factory=list,
        validation_alias=AliasChoices("merges", "groups", "merge_groups"),
        json_schema_extra={"maxItems": 60},
    )


BeatType = Literal["narration", "dialogue"]


class Beat(BaseModel):
    type: BeatType
    text: str
    speaker: str | None = Field(
        default=None,
        description="Canonical character name; required when type=='dialogue', else null",
    )
    source_span: str = Field(
        default="",
        description=(
            "Verbatim, contiguous slice of the source text this beat is "
            "grounded in — quote marks, attribution tags and original "
            "whitespace KEPT (unlike `text`, which strips them). Used by the "
            "sync stage as an exact match key back to the original EPUB."
        ),
    )


class SceneBoundary(BaseModel):
    """Stage 2a output: one scene's extent, before beats are tagged."""

    scene_id: str
    location_hint: str = ""
    cast: list[str] = Field(
        default_factory=list,
        description="Canonical names of characters present in this scene.",
    )
    start_paragraph: int = Field(
        description="1-indexed chapter-global paragraph where the scene starts."
    )
    end_paragraph: int = Field(
        description="1-indexed chapter-global paragraph where the scene ends (inclusive)."
    )

    _coerce_cast = field_validator("cast", mode="before")(_coerce_str_to_list)


class ChapterBoundaries(BaseModel):
    chapter_id: str
    scenes: list[SceneBoundary]


class SceneBeats(BaseModel):
    """Stage 2b output: the beats for a single scene."""

    beats: list[Beat]


class Scene(BaseModel):
    scene_id: str
    location_hint: str = ""
    # Chapter-global paragraph range from Stage 2a (0 when unknown / legacy).
    start_paragraph: int = 0
    end_paragraph: int = 0
    beats: list[Beat]


class ChapterScenes(BaseModel):
    chapter_id: str
    scenes: list[Scene]


# ---------- Stage 3 (Director) ----------


class VoiceProfile(BaseModel):
    name: str = Field(description="Canonical character name (or 'Narrator')")
    voice_descriptor: str = Field(
        description="Short performance descriptor (6-15 words), Dramabox-ready"
    )
    accent: str = Field(
        default="any",
        description="CV accent code (us, england, indian, …) or 'any'.",
    )
    # gender + age_band are populated when s3 runs AFTER voice cast: they
    # come straight from the assigned reference clip's metadata so the
    # descriptor stays consistent with the actual voice ref.
    gender: str = Field(
        default="",
        description="'male' or 'female' (matches the assigned ref clip).",
    )
    age_band: str = Field(
        default="",
        description="'teen' | 'young_adult' | 'adult' | 'elder' (matches clip).",
    )


class VoiceProfileList(BaseModel):
    # maxItems caps a runaway/looping model in the guided schema only (not
    # validation) — see the note on Character. Generous: a single book's
    # speaking cast is well under 120.
    profiles: list[VoiceProfile] = Field(json_schema_extra={"maxItems": 120})


class DirectionCue(BaseModel):
    line: int = Field(description="1-indexed dialogue-line number from the prompt")
    cue: str = Field(description="Short performance cue (2-8 words)")


class SceneDirections(BaseModel):
    directions: list[DirectionCue] = Field(json_schema_extra={"maxItems": 300})


class DirectedBeat(BaseModel):
    type: BeatType
    text: str
    speaker: str  # always set — "Narrator" for narration
    direction: str  # voice + performance descriptor that prefixes the line
    prompt: str  # final Dramabox-ready string: `<direction>, "<text>"`
    source_span: str = ""  # carried through from Stage 2 for the sync stage
    # Chapter-global paragraph index this beat was split from (lecture mode
    # only; -1 in narration mode). Lets Stage 6 place ingest-extracted visual
    # elements (code/table/figure), which carry an `after_paragraph`, against
    # the beat that covers that paragraph. See DESIGN.md §13.2/§13.5.
    # Scenario mode (§17) reuses it as the chapter-global dialogue-line index
    # so the sync emitter can re-group a long line's split chunks.
    source_paragraph: int = -1
    # Scenario mode (§17) / emotion voicebank (§16.7): one of the 7-value
    # Emotion enum. "calm" everywhere else — a plain str so old artifacts
    # and non-scenario paths validate unchanged.
    emotion: str = "calm"


class DirectedScene(BaseModel):
    scene_id: str
    location_hint: str = ""
    beats: list[DirectedBeat]


class ChapterDirected(BaseModel):
    chapter_id: str
    scenes: list[DirectedScene]


# ---------- Lecture mode (DESIGN.md §13) ----------


# Block kinds the lecture-mode ingest classifier emits. `prose` (and headings,
# folded into prose) is narrated; `drop` is excised entirely; the rest become
# reader-side visual elements (§13.2).
BlockKind = Literal[
    "prose", "code", "table", "figure", "footnote", "equation", "drop"
]


class BlockClass(BaseModel):
    """LLM-fallback classification of ONE untagged block (§13.2a step 2).

    Only blocks the deterministic rules can't classify reach the LLM, so this
    is a tiny, cheap call. `reason` is kept for debugging the classifier's
    judgment on messy publisher markup."""

    kind: BlockKind
    reason: str = ""


class NormalizedBeat(BaseModel):
    """One beat's speech-normalized text, keyed back by its 0-indexed position
    in the batch sent to the normalize pass (§13.6)."""

    index: int
    text: str


class NormalizedBeats(BaseModel):
    # maxItems caps a runaway model in the guided schema only (see Character).
    beats: list[NormalizedBeat] = Field(json_schema_extra={"maxItems": 200})


# ---------- Scenario mode (DESIGN.md §17) ----------


# Shared with the §16.7 emotion-voicebank plan: 7 language-neutral values.
Emotion = Literal["calm", "joy", "sadness", "anger", "fear", "surprise", "disgust"]

ScriptItemType = Literal["dialogue", "staging", "cue"]


class ScriptItem(BaseModel):
    """One structured line of a theater script (verbatim text, never rewritten)."""

    type: ScriptItemType
    speaker: str = Field(
        default="",
        description="Speaker label as printed (markdown stripped); '' unless dialogue",
    )
    text: str = Field(description="Verbatim line text, markdown markers/escapes removed")


class SceneStructure(BaseModel):
    """LLM output for one scene chunk of a script (§17.2)."""

    items: list[ScriptItem] = Field(json_schema_extra={"maxItems": 300})


class RosterEntry(BaseModel):
    """One entry of the script's own characters section, when present."""

    name: str
    description: str = ""
    actor: str = Field(
        default="",
        description="Performer this role maps to when the script lists one; else ''",
    )


class RosterList(BaseModel):
    entries: list[RosterEntry] = Field(
        default_factory=list, json_schema_extra={"maxItems": 80}
    )


class ScriptScene(BaseModel):
    scene_id: str  # "01", "02", … (also the s4/s5 chapter id)
    title: str = ""
    items: list[ScriptItem] = Field(default_factory=list)


class ScenarioScript(BaseModel):
    """artifacts/<id>/00_script.json — the structured, verbatim script."""

    scenario_id: str
    title: str = ""
    language: str = ""
    roster: list[RosterEntry] = Field(default_factory=list)
    scenes: list[ScriptScene] = Field(default_factory=list)


class ScenarioCue(BaseModel):
    line: int = Field(description="1-indexed dialogue-line number from the prompt")
    emotion: Emotion = "calm"
    cue: str = Field(
        default="",
        description="Short performance cue (2-6 words) in the script's language",
    )


class ScenarioDirections(BaseModel):
    directions: list[ScenarioCue] = Field(json_schema_extra={"maxItems": 300})

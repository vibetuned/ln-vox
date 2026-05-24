from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class Character(BaseModel):
    name: str = Field(description="Canonical name as it appears in the text most often")
    aliases: list[str] = Field(default_factory=list)
    gender: Gender = "unknown"
    approx_age: AgeBand = "unknown"
    description: str
    evidence: list[str] = Field(default_factory=list)

    _coerce_aliases = field_validator("aliases", "evidence", mode="before")(
        _coerce_str_to_list
    )


class CharacterList(BaseModel):
    characters: list[Character]


BeatType = Literal["narration", "dialogue"]


class Beat(BaseModel):
    type: BeatType
    text: str
    speaker: str | None = Field(
        default=None,
        description="Canonical character name; required when type=='dialogue', else null",
    )


class Scene(BaseModel):
    scene_id: str
    location_hint: str = ""
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
    profiles: list[VoiceProfile]


class DirectionCue(BaseModel):
    line: int = Field(description="1-indexed dialogue-line number from the prompt")
    cue: str = Field(description="Short performance cue (2-8 words)")


class SceneDirections(BaseModel):
    directions: list[DirectionCue]


class DirectedBeat(BaseModel):
    type: BeatType
    text: str
    speaker: str  # always set — "Narrator" for narration
    direction: str  # stage-direction body that goes inside the [ ... ] brackets
    prompt: str  # final Dramabox-ready string: [direction]\n"text"


class DirectedScene(BaseModel):
    scene_id: str
    location_hint: str = ""
    beats: list[DirectedBeat]


class ChapterDirected(BaseModel):
    chapter_id: str
    scenes: list[DirectedScene]

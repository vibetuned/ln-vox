"""Pydantic models for the voicebank and book-level voice casting."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from lnvox.llm.schemas import _coerce_str_to_list


VBGender = Literal["male", "female"]
VBAgeBand = Literal["teen", "young_adult", "adult", "elder"]


class VoiceClip(BaseModel):
    """A single voice-bank entry: a 10-20 second reference clip with metadata."""

    id: str = Field(description="Unique clip id, prefixed by source (e.g. 'cv_a1b2c3')")
    source: str = Field(
        description="Dataset source: common_voice, artie, fair_speech, speech_accent_archive, manual, youtube"
    )
    clip_path: str = Field(description="Path to the WAV file, relative to voicebank root")
    duration_seconds: float
    gender: VBGender
    age_band: VBAgeBand
    accent: str = Field(default="any", description="Normalized accent token, free-text")
    sample_sentences: list[str] = Field(
        default_factory=list,
        description="Up to 3 transcripts of the source clips; useful for prompt-matching.",
    )
    license: str = "CC0"
    notes: str = ""


class Voicebank(BaseModel):
    """Top-level voicebank manifest."""

    version: str = "1"
    clips: list[VoiceClip] = Field(default_factory=list)


class VoiceTarget(BaseModel):
    """LLM-inferred target attributes used to filter the voicebank for a character."""

    gender: VBGender
    age_band: VBAgeBand
    accent_keywords: list[str] = Field(default_factory=list)
    timbre_keywords: list[str] = Field(default_factory=list)
    manner_keywords: list[str] = Field(default_factory=list)

    _coerce_kw = field_validator(
        "accent_keywords", "timbre_keywords", "manner_keywords", mode="before"
    )(_coerce_str_to_list)


class _RankedChoice(BaseModel):
    """Internal: one ranked candidate returned by the LLM."""

    clip_id: str
    score: float = Field(description="0..1 confidence in the match")
    reason: str = Field(description="One short sentence justifying the choice")


class MatchResult(BaseModel):
    ranked_choices: list[_RankedChoice]


class CharacterCasting(BaseModel):
    """Result of LLM voice matching for one character."""

    character_name: str
    target: VoiceTarget
    candidates_considered: int = 0
    ranked: list[_RankedChoice] = Field(default_factory=list)
    assigned_clip_id: str = ""
    # Optional pre-computed Dramabox descriptor for this character. When set
    # (typically for the Narrator with a manual --narrator-clip or a reused
    # prior-volume clip), Stage 3 uses this verbatim instead of asking the
    # LLM to compose one — guarantees the descriptor stays consistent with
    # the assigned reference clip.
    voice_descriptor: str = ""


class BookCasting(BaseModel):
    """All character → clip assignments for a book."""

    book_id: str
    castings: list[CharacterCasting] = Field(default_factory=list)

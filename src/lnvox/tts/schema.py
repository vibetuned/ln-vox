"""Pydantic models for Stage 4 (Dramabox TTS) outputs."""

from pydantic import BaseModel, Field


class RenderedBeat(BaseModel):
    """One Dramabox-rendered audio beat."""

    beat_id: str = Field(description="Stable id like '01_s1_b0023'")
    scene_id: str
    type: str = Field(description="'narration' or 'dialogue'")
    speaker: str
    wav_path: str = Field(description="Path to the rendered WAV, relative to artifacts/<book>")
    duration_seconds: float
    cache_key: str = Field(description="Content-hash key used to dedupe re-renders")
    cached: bool = False


class ChapterAudio(BaseModel):
    """All rendered beats for one chapter, in playback order."""

    chapter_id: str
    beats: list[RenderedBeat] = Field(default_factory=list)
    total_duration_seconds: float = 0.0

"""Stage L — lecture mode (DESIGN.md §13.3).

Replaces s1 + s2 + s3 for non-fiction / technical books. No character
extraction, no scene segmentation, no Director. Two sub-steps:

  1. Deterministic beat split — each chapter's prose is split into beats at
     sentence boundaries (≤ ``MAX_MERGED_BEAT_CHARS``), reusing the same length
     policy as the narration Director. Every beat is narration, voiced by the
     single Narrator, and its ``source_span`` is the VERBATIM source slice.
  2. LLM speech-normalize — each beat's ``text`` is respelled for the ear
     (numbers, units, abbreviations…) while ``source_span`` stays untouched.
     Optional: ``--no-normalize`` (or no LLM) leaves ``text == source_span``.

Output: ``03_directed/<chapter_id>.json`` (``ChapterDirected``) — the exact
schema Stage 4 already consumes, so s4/s5/s6 run unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from lnvox.ingest.text import Chapter
from lnvox.llm.schemas import (
    ChapterDirected,
    DirectedBeat,
    DirectedScene,
    NormalizedBeats,
)
from lnvox.llm.chunker import split_paragraphs
from lnvox.stages.s3_director import (
    DEFAULT_NARRATOR_DESCRIPTOR,
    MAX_MERGED_BEAT_CHARS,
    NARRATOR_NAME,
    _split_long_text,
    format_prompt,
)
from lnvox.voices.schema import BookCasting


NORMALIZE_SYSTEM = (
    "You prepare prose for a single-voice audiobook narrator. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)

# How many beats to send per normalize call. Bounded so the prompt stays small
# and a parse failure only forfeits one batch (which then falls back to verbatim
# source_span). ~25 beats × ~500 chars ≈ a comfortable prompt.
_NORMALIZE_BATCH = 25


def narrator_descriptor(casting: Optional[BookCasting]) -> str:
    """The Narrator's Dramabox voice descriptor, from the cast (no perf cue)."""
    if casting:
        for c in casting.castings:
            if c.character_name == NARRATOR_NAME and c.voice_descriptor:
                return c.voice_descriptor
    return DEFAULT_NARRATOR_DESCRIPTOR


def split_chapter(chapter: Chapter) -> list[DirectedBeat]:
    """Split one chapter's prose into narration beats (pre-normalize).

    `text` starts equal to `source_span` (verbatim); the normalize pass may
    overwrite `text` later. Each beat records the chapter-global paragraph it
    came from so Stage 6 can place visual elements.
    """
    beats: list[DirectedBeat] = []
    for para_idx, paragraph in enumerate(split_paragraphs(chapter.text)):
        for chunk in _split_long_text(paragraph, MAX_MERGED_BEAT_CHARS):
            chunk = chunk.strip()
            if not chunk:
                continue
            beats.append(
                DirectedBeat(
                    type="narration",
                    text=chunk,
                    speaker=NARRATOR_NAME,
                    direction="",  # filled with the narrator descriptor below
                    prompt="",
                    source_span=chunk,
                    source_paragraph=para_idx,
                )
            )
    return beats


def _normalize_beats(client, beats: list[DirectedBeat]) -> None:
    """Speech-normalize each beat's `text` in place (best-effort, batched).

    On any failure for a batch, those beats keep `text == source_span` — a
    flaky normalize call degrades to literal reading, never a dropped beat.
    """
    for start in range(0, len(beats), _NORMALIZE_BATCH):
        batch = beats[start : start + _NORMALIZE_BATCH]
        passages = "\n".join(
            f"[{i}] {b.source_span}" for i, b in enumerate(batch)
        )
        user = client.render("lecture_normalize.jinja", passages=passages)
        budget = client.budget_for(
            system=NORMALIZE_SYSTEM,
            user=user,
            desired=max(2048, int(sum(len(b.source_span) for b in batch) / 2) + 1024),
            floor=2048,
        )
        try:
            result: NormalizedBeats = client.structured(
                system=NORMALIZE_SYSTEM,
                user=user,
                schema=NormalizedBeats,
                max_tokens=budget,
            )
        except Exception:
            continue  # keep verbatim source_span for this batch
        by_index = {nb.index: nb.text.strip() for nb in result.beats}
        for i, b in enumerate(batch):
            spoken = by_index.get(i, "")
            if spoken:
                b.text = spoken


def direct_chapter(
    chapter: Chapter, descriptor: str, *, client=None, normalize: bool = True
) -> ChapterDirected:
    """Build a single-scene ChapterDirected for one chapter."""
    beats = split_chapter(chapter)
    if normalize and client is not None and beats:
        _normalize_beats(client, beats)
    for b in beats:
        b.direction = descriptor
        b.prompt = format_prompt(descriptor, b.text)
    scene = DirectedScene(
        scene_id=f"{chapter.chapter_id}_s1",
        location_hint="",
        beats=beats,
    )
    return ChapterDirected(chapter_id=chapter.chapter_id, scenes=[scene])


def run(
    chapters: list[Chapter],
    output_dir: Path,
    *,
    casting: Optional[BookCasting] = None,
    client=None,
    normalize: bool = True,
    on_chapter_done: Callable[[ChapterDirected], None] | None = None,
) -> list[ChapterDirected]:
    """Direct every chapter → ``03_directed/<chapter_id>.json``. Idempotent."""
    output_dir.mkdir(parents=True, exist_ok=True)
    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    descriptor = narrator_descriptor(casting)

    results: list[ChapterDirected] = []
    for chapter in chapters:
        cached = directed_dir / f"{chapter.chapter_id}.json"
        result: ChapterDirected | None = None
        if cached.exists():
            try:
                result = ChapterDirected.model_validate_json(
                    cached.read_text(encoding="utf-8")
                )
            except Exception:
                result = None  # malformed — re-direct
        if result is None:
            result = direct_chapter(
                chapter, descriptor, client=client, normalize=normalize
            )
            cached.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        results.append(result)
        if on_chapter_done:
            on_chapter_done(result)
    return results

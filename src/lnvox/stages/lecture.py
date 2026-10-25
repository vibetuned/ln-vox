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
from lnvox.llm.client import is_transport_error
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


# Language names for the normalize prompt; other codes are named as-is.
_LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ja": "Japanese",
}


def _normalize_beats(client, beats: list[DirectedBeat], language: str = "en") -> None:
    """Speech-normalize each beat's `text` in place (best-effort, batched).

    When a batch's answer is bad or unparseable, those beats keep
    `text == source_span` — a flaky normalize call degrades to literal
    reading, never a dropped beat. A transport error (server down) is
    re-raised instead: degrading would cache every remaining chapter
    un-normalized and the launcher's `ensure_vllm` retry would never run.

    `language` is the book's language code: a non-English book is respelled
    in its own language (the prompt's examples are English, and the model
    would otherwise read French years out in English words).
    """
    code = (language or "en").split("-")[0].lower()
    language_name = _LANGUAGE_NAMES.get(code, language)
    for start in range(0, len(beats), _NORMALIZE_BATCH):
        batch = beats[start : start + _NORMALIZE_BATCH]
        passages = "\n".join(
            f"[{i}] {b.source_span}" for i, b in enumerate(batch)
        )
        user = client.render(
            "lecture_normalize.jinja",
            passages=passages,
            language=code,
            language_name=language_name,
        )
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
        except Exception as e:
            if is_transport_error(e):
                raise
            continue  # keep verbatim source_span for this batch
        by_index = {nb.index: nb.text.strip() for nb in result.beats}
        for i, b in enumerate(batch):
            spoken = by_index.get(i, "")
            if spoken:
                b.text = spoken


def direct_chapter(
    chapter: Chapter,
    descriptor: str,
    *,
    client=None,
    normalize: bool = True,
    language: str = "en",
) -> ChapterDirected:
    """Build a single-scene ChapterDirected for one chapter."""
    beats = split_chapter(chapter)
    normalized = normalize and client is not None
    if normalized and beats:
        _normalize_beats(client, beats, language)
    for b in beats:
        b.direction = descriptor
        b.prompt = format_prompt(descriptor, b.text)
    scene = DirectedScene(
        scene_id=f"{chapter.chapter_id}_s1",
        location_hint="",
        beats=beats,
    )
    return ChapterDirected(
        chapter_id=chapter.chapter_id,
        voice_descriptors={NARRATOR_NAME: descriptor},
        normalized=normalized,
        scenes=[scene],
    )


def _revoice(result: ChapterDirected, descriptor: str) -> ChapterDirected:
    """Swap the narrator descriptor into every beat — no LLM needed, since
    the (normalized) text doesn't depend on the voice."""
    for scene in result.scenes:
        for b in scene.beats:
            b.direction = descriptor
            b.prompt = format_prompt(descriptor, b.text)
    result.voice_descriptors = {NARRATOR_NAME: descriptor}
    return result


def run(
    chapters: list[Chapter],
    output_dir: Path,
    *,
    casting: Optional[BookCasting] = None,
    client=None,
    normalize: bool = True,
    language: str = "en",
    on_chapter_done: Callable[[ChapterDirected], None] | None = None,
) -> list[ChapterDirected]:
    """Direct every chapter → ``03_directed/<chapter_id>.json``. Idempotent.

    A cached chapter is reused only while it matches this run: different
    chapter text (a re-extraction, a proofread) or a different normalize
    setting (``--no-normalize`` toggled, or no client) redoes it; a
    different narrator descriptor just re-voices its prompts. A chapter
    written before the normalize setting and descriptors were recorded is
    treated as current and stamped.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    directed_dir = output_dir / "03_directed"
    directed_dir.mkdir(exist_ok=True)
    descriptor = narrator_descriptor(casting)
    normalized = normalize and client is not None
    voices = {NARRATOR_NAME: descriptor}

    results: list[ChapterDirected] = []
    for chapter in chapters:
        cached = directed_dir / f"{chapter.chapter_id}.json"
        result: ChapterDirected | None = None
        dirty = False
        if cached.exists():
            try:
                result = ChapterDirected.model_validate_json(
                    cached.read_text(encoding="utf-8")
                )
            except Exception:
                result = None  # malformed — re-direct
        # Every beat keeps its verbatim `source_span`, so the cached spans
        # are the text the chapter was directed from: compare them with a
        # fresh split instead of storing a hash (also catches caches written
        # before this check existed).
        if result is not None and [
            b.source_span for s in result.scenes for b in s.beats
        ] != [b.source_span for b in split_chapter(chapter)]:
            print(f"  [lecture] {chapter.chapter_id}: chapter text changed — rebuilding")
            result = None
        if result is not None:
            if result.normalized is None:
                result.normalized = normalized
                dirty = True
            if result.voice_descriptors is None:
                result.voice_descriptors = dict(voices)
                dirty = True
            if result.normalized != normalized:
                print(
                    f"  [lecture] {chapter.chapter_id}: cached "
                    f"{'with' if result.normalized else 'without'} speech-normalize "
                    f"— rebuilding {'with' if normalized else 'without'} it"
                )
                result = None
            elif result.voice_descriptors != voices:
                print(
                    f"  [lecture] {chapter.chapter_id}: narrator descriptor "
                    f"changed — re-voiced prompts"
                )
                result = _revoice(result, descriptor)
                dirty = True
        if result is None:
            result = direct_chapter(
                chapter, descriptor, client=client, normalize=normalize, language=language
            )
            dirty = True
        if dirty:
            cached.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        results.append(result)
        if on_chapter_done:
            on_chapter_done(result)
    return results

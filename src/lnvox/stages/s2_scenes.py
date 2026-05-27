import json
from pathlib import Path

from lnvox.ingest.text import Chapter
from lnvox.llm.chunker import Chunk, chunk_text, split_paragraphs
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import (
    ChapterBoundaries,
    ChapterScenes,
    CharacterList,
    Scene,
    SceneBeats,
    SceneBoundary,
)


BOUNDARY_SYSTEM = (
    "You divide novel chapters into scenes by paragraph range. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation before or after the JSON. "
    "Scenes must tile the chapter with no gaps or overlaps."
)

BEATS_SYSTEM = (
    "You split one scene into narration/dialogue beats and ground each beat in "
    "the verbatim source it came from. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation before or after the JSON. "
    "Preserve source order exactly; do not invent, omit, or paraphrase content."
)


def _build_alias_map(cast: CharacterList) -> dict[str, str]:
    """Lowercased name OR alias → canonical character `name`.

    If the same alias appears under multiple characters, the last one wins;
    that's rare enough to ignore in v1.
    """
    mapping: dict[str, str] = {}
    for c in cast.characters:
        mapping[c.name.lower().strip()] = c.name
        for alias in c.aliases:
            key = alias.lower().strip()
            if key:
                mapping[key] = c.name
    return mapping


def _normalize_speakers(scenes: ChapterScenes, alias_map: dict[str, str]) -> int:
    """In-place: rewrite each dialogue beat's speaker to its canonical name.

    Returns the count of beats whose speaker was rewritten. Unknown or
    unmatched names are left untouched.
    """
    rewritten = 0
    for scene in scenes.scenes:
        for beat in scene.beats:
            if beat.type != "dialogue" or not beat.speaker:
                continue
            canonical = alias_map.get(beat.speaker.lower().strip())
            if canonical and canonical != beat.speaker:
                beat.speaker = canonical
                rewritten += 1
    return rewritten


def _detect_boundaries(
    client: LLMClient,
    chapter: Chapter,
    chunk: Chunk,
    *,
    cast_json: str,
    title: str,
) -> list[SceneBoundary]:
    """Stage 2a: ask for scene boundaries over one chunk's paragraphs.

    The chunk is numbered LOCALLY (1..N) for the prompt; the returned ranges
    are offset by the chunk's `base_paragraph` so they become chapter-global.
    """
    numbered = "\n\n".join(
        f"[{i + 1}]\n{p}" for i, p in enumerate(chunk.paragraphs)
    )
    user = client.render(
        "scene_boundaries.jinja",
        cast_json=cast_json,
        chapter_id=chapter.chapter_id,
        title=title,
        paragraph_count=len(chunk.paragraphs),
        numbered_paragraphs=numbered,
    )
    # Boundaries are tiny: a handful of scenes, each a short metadata record.
    budget = max(1024, min(8192, 80 * len(chunk.paragraphs) + 512))
    try:
        result = client.structured(
            system=BOUNDARY_SYSTEM,
            user=user,
            schema=ChapterBoundaries,
            max_tokens=budget,
        )
        boundaries = result.scenes
    except Exception:
        boundaries = []

    n = len(chunk.paragraphs)
    cleaned: list[SceneBoundary] = []
    for b in boundaries:
        start = max(1, min(b.start_paragraph, n))
        end = max(start, min(b.end_paragraph, n))
        b.start_paragraph = chunk.base_paragraph + start
        b.end_paragraph = chunk.base_paragraph + end
        cleaned.append(b)
    if not cleaned:
        # Fallback: treat the whole chunk as a single scene.
        cleaned = [
            SceneBoundary(
                scene_id=f"{chapter.chapter_id}_s1",
                location_hint="",
                cast=[],
                start_paragraph=chunk.base_paragraph + 1,
                end_paragraph=chunk.base_paragraph + n,
            )
        ]
    return cleaned


def _tag_scene_beats(
    client: LLMClient,
    boundary: SceneBoundary,
    scene_text: str,
    *,
    cast_json: str,
) -> SceneBeats:
    """Stage 2b: split one scene's text into beats with source_span."""
    user = client.render(
        "scene_beats.jinja",
        cast_json=cast_json,
        scene_id=boundary.scene_id,
        location_hint=boundary.location_hint or "",
        text=scene_text,
    )
    # source_span roughly duplicates the source, so budget ~1.4x the scene
    # text; capped well inside the context window.
    budget = max(4096, min(32000, int(len(scene_text) * 1.4) + 1024))
    try:
        return client.structured(
            system=BEATS_SYSTEM, user=user, schema=SceneBeats, max_tokens=budget
        )
    except Exception:
        return SceneBeats(beats=[])


def segment_chapter(
    client: LLMClient, chapter: Chapter, cast: CharacterList
) -> ChapterScenes:
    cast_summary = [
        {"name": c.name, "aliases": c.aliases} for c in cast.characters
    ]
    cast_json = json.dumps(cast_summary, ensure_ascii=False, indent=2)
    alias_map = _build_alias_map(cast)

    paragraphs = split_paragraphs(chapter.text)
    chunks = chunk_text(client, chapter.text)

    # Pass 2a — scene boundaries (chapter-global paragraph ranges).
    boundaries: list[SceneBoundary] = []
    for chunk_idx, chunk in enumerate(chunks, 1):
        title = chapter.title
        if len(chunks) > 1:
            title = f"{title} (part {chunk_idx}/{len(chunks)})"
        boundaries.extend(
            _detect_boundaries(
                client, chapter, chunk, cast_json=cast_json, title=title
            )
        )

    # Pass 2b — beats per scene, sliced from the chapter-global paragraph list.
    all_scenes: list[Scene] = []
    for b in boundaries:
        scene_id = f"{chapter.chapter_id}_s{len(all_scenes) + 1}"
        scene_paras = paragraphs[b.start_paragraph - 1 : b.end_paragraph]
        scene_text = "\n\n".join(scene_paras)
        if not scene_text.strip():
            continue
        b.scene_id = scene_id
        beats = _tag_scene_beats(
            client, b, scene_text, cast_json=cast_json
        ).beats
        all_scenes.append(
            Scene(
                scene_id=scene_id,
                location_hint=b.location_hint,
                start_paragraph=b.start_paragraph,
                end_paragraph=b.end_paragraph,
                beats=beats,
            )
        )

    chapter_scenes = ChapterScenes(chapter_id=chapter.chapter_id, scenes=all_scenes)
    _normalize_speakers(chapter_scenes, alias_map)
    return chapter_scenes


def run(
    chapters: list[Chapter],
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    on_chapter_done=None,
) -> list[ChapterScenes]:
    scenes_dir = output_dir / "02_scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    results: list[ChapterScenes] = []
    for ch in chapters:
        # Idempotent: a chapter already segmented on a prior run is reloaded
        # from disk instead of re-calling the LLM. Mirrors s1's per-chapter
        # cache so a crash mid-volume resumes cheaply.
        cached_path = scenes_dir / f"{ch.chapter_id}.json"
        result: ChapterScenes | None = None
        if cached_path.exists():
            try:
                result = ChapterScenes.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                result = None  # malformed / truncated — re-segment
        if result is None:
            result = segment_chapter(client, ch, cast)
            cached_path.write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)
    return results

import json
from pathlib import Path

from lnvox.ingest.text import Chapter
from lnvox.llm.chunker import Chunk, chunk_text, split_paragraphs
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import (
    Beat,
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
    # Clamped to what the served context leaves after this (up to ~40K-char)
    # prompt: a fixed budget 400s (input + output > context) on a smaller
    # --max-model-len.
    budget = client.budget_for(
        system=BOUNDARY_SYSTEM,
        user=user,
        desired=max(1024, min(8192, 80 * len(chunk.paragraphs) + 512)),
        floor=1024,
    )
    # Exceptions from `structured` (dead vLLM, persistent truncation) propagate
    # so the pipeline-level retry + `ensure_vllm` can restart the server. The
    # *only* in-band fallback below is "the LLM successfully returned an empty
    # scene list" — a different, recoverable case (model legitimately found no
    # break, treat the chunk as one scene).
    result = client.structured(
        system=BOUNDARY_SYSTEM,
        user=user,
        schema=ChapterBoundaries,
        max_tokens=budget,
    )
    boundaries = result.scenes

    n = len(chunk.paragraphs)
    cleaned: list[SceneBoundary] = []
    for b in boundaries:
        start = max(1, min(b.start_paragraph, n))
        end = max(start, min(b.end_paragraph, n))
        b.start_paragraph = chunk.base_paragraph + start
        b.end_paragraph = chunk.base_paragraph + end
        cleaned.append(b)
    if not cleaned:
        # LLM returned an empty scene list — treat the whole chunk as one scene.
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


def _tile_boundaries(
    boundaries: list[SceneBoundary], n_paragraphs: int
) -> tuple[list[SceneBoundary], list[str]]:
    """Repair Pass 2a's ranges so the scenes tile paragraphs 1..N exactly once.

    The prompt asks for a gap-free, overlap-free tiling, but nothing enforced
    it: Pass 2b only beat-tags the paragraphs inside a scene's range, so a
    GAP left those paragraphs out of the audio entirely, and an OVERLAP
    voiced them twice. Sort by start, clamp to the chapter, fold a gap into
    the earlier scene (the head gap into the first one), trim an overlap off
    the later scene (dropping one it fully covers), and stretch the last
    scene to the final paragraph. Returns ``(tiled, repair notes)``.
    """
    if n_paragraphs <= 0 or not boundaries:
        return boundaries, []
    notes: list[str] = []
    ordered: list[SceneBoundary] = []
    for b in boundaries:
        start = max(1, min(b.start_paragraph, n_paragraphs))
        end = max(start, min(b.end_paragraph, n_paragraphs))
        if (start, end) != (b.start_paragraph, b.end_paragraph):
            notes.append(
                f"clamped {b.start_paragraph}–{b.end_paragraph} to "
                f"{start}–{end} (chapter has {n_paragraphs} paragraphs)"
            )
        ordered.append(
            b.model_copy(update={"start_paragraph": start, "end_paragraph": end})
        )
    ordered.sort(key=lambda b: (b.start_paragraph, b.end_paragraph))

    tiled: list[SceneBoundary] = []
    next_para = 1  # first paragraph not yet covered
    for b in ordered:
        if b.end_paragraph < next_para:
            notes.append(
                f"dropped {b.start_paragraph}–{b.end_paragraph} "
                f"(already covered by the previous scene)"
            )
            continue
        if b.start_paragraph > next_para:
            gap = f"{next_para}–{b.start_paragraph - 1}"
            if tiled:
                tiled[-1].end_paragraph = b.start_paragraph - 1
                notes.append(f"gap {gap} folded into the previous scene")
            else:
                notes.append(f"head {gap} folded into the first scene")
                b.start_paragraph = next_para
        elif b.start_paragraph < next_para:
            notes.append(
                f"overlap {b.start_paragraph}–{next_para - 1} trimmed off "
                f"{b.start_paragraph}–{b.end_paragraph}"
            )
            b.start_paragraph = next_para
        tiled.append(b)
        next_para = b.end_paragraph + 1
    if next_para <= n_paragraphs:
        notes.append(f"tail {next_para}–{n_paragraphs} folded into the last scene")
        tiled[-1].end_paragraph = n_paragraphs
    return tiled, notes


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
    # The beats JSON carries the source twice (spoken `text` + verbatim
    # `source_span`) plus keys/escapes — measured ~5.2x the scene chars on
    # dialogue-dense chapters, i.e. ~1.6 output tokens per source char; 1.4
    # under-shot those and died on finish_reason=length. 2.0 gives margin,
    # capped at 32K and clamped by `budget_for` to what the served context
    # leaves after the prompt (the scene text is IN the prompt, so a long
    # scene on a 32K context would otherwise ask for more than fits and 400);
    # the client also grows the budget on a length cut now. Exceptions
    # propagate (a dead vLLM here used to be silently swallowed into empty
    # beats, which cached as a degenerate "1 scene, 0 beats" result and
    # corrupted every subsequent chapter until the cache was wiped).
    budget = client.budget_for(
        system=BEATS_SYSTEM,
        user=user,
        desired=max(4096, min(32000, int(len(scene_text) * 2.0) + 1024)),
        floor=4096,
    )
    return client.structured(
        system=BEATS_SYSTEM, user=user, schema=SceneBeats, max_tokens=budget
    )


def _tag_in_halves(
    client: LLMClient,
    boundary: SceneBoundary,
    paragraphs: list[str],
    *,
    cast_json: str,
    depth: int = 0,
) -> list[Beat]:
    """Beats for one scene's paragraphs; if the reply never validates, the
    two halves tagged apart (down to 1/8 of the scene).

    The quantized model sometimes loops on one scene — `{}` pairs until the
    token limit — and does so again on every retry of that prompt, so the
    whole volume stopped there. The same text in two shorter calls gets
    through, and the halves' beats join in order: a scene is contiguous
    paragraphs, and each beat carries its own source span.
    """
    try:
        return _tag_scene_beats(
            client, boundary, "\n\n".join(paragraphs), cast_json=cast_json
        ).beats
    except ValueError as e:
        if len(paragraphs) < 2 or depth >= 3:
            raise
        print(
            f"  [s2] {boundary.scene_id}: no valid beats for "
            f"{len(paragraphs)} paragraph(s) ({str(e).splitlines()[0][:80]}) — "
            f"tagging the two halves apart"
        )
    mid = len(paragraphs) // 2
    return [
        beat
        for half in (paragraphs[:mid], paragraphs[mid:])
        for beat in _tag_in_halves(
            client, boundary, half, cast_json=cast_json, depth=depth + 1
        )
    ]


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
    boundaries, repairs = _tile_boundaries(boundaries, len(paragraphs))
    for note in repairs:
        print(f"  [s2] {chapter.chapter_id}: scene ranges — {note}")

    # Pass 2b — beats per scene, sliced from the chapter-global paragraph list.
    all_scenes: list[Scene] = []
    for b in boundaries:
        scene_id = f"{chapter.chapter_id}_s{len(all_scenes) + 1}"
        scene_paras = paragraphs[b.start_paragraph - 1 : b.end_paragraph]
        scene_text = "\n\n".join(scene_paras)
        if not scene_text.strip():
            continue
        b.scene_id = scene_id
        beats = _tag_in_halves(client, b, scene_paras, cast_json=cast_json)
        # Backstop for the silent-failure variant: the LLM returned valid JSON
        # with an empty beats list for a non-empty scene. This used to silently
        # cache as a degenerate chapter and corrupt every subsequent one.
        # Raise so the pipeline retry can restart vLLM and re-segment.
        if not beats:
            raise RuntimeError(
                f"s2: scene {scene_id} returned 0 beats from "
                f"{len(scene_text)} chars of source — likely a stalled LLM. "
                f"Aborting chapter so retry can restart vLLM."
            )
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


def _looks_broken(result: ChapterScenes) -> bool:
    """Detect a degenerate cached result from an earlier silent-failure run.

    The bug pattern: vLLM dies mid-volume, the old broad excepts swallow it,
    a chapter caches as ``[scene(beats=[])]``. Any scene with zero beats is
    invalid — non-empty scenes are filtered out in `segment_chapter` before
    beat tagging — so this safely flags only the degenerate cache.
    """
    if not result.scenes:
        return True
    return any(not s.beats for s in result.scenes)


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
                if _looks_broken(result):
                    print(
                        f"  [s2] cached {ch.chapter_id} has empty beats "
                        f"(silent-failure carryover) — re-segmenting"
                    )
                    result = None
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

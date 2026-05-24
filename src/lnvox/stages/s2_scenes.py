import json
from pathlib import Path

from lnvox.ingest.text import Chapter
from lnvox.llm.chunker import chunk_text
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import CharacterList, ChapterScenes, Scene


SYSTEM = (
    "You split novel chapters into scenes and tag each line as narration or dialogue. "
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


def segment_chapter(
    client: LLMClient, chapter: Chapter, cast: CharacterList
) -> ChapterScenes:
    cast_summary = [
        {"name": c.name, "aliases": c.aliases} for c in cast.characters
    ]
    cast_json = json.dumps(cast_summary, ensure_ascii=False, indent=2)
    alias_map = _build_alias_map(cast)

    chunks = chunk_text(client, chapter.text)
    all_scenes: list[Scene] = []

    for chunk_idx, chunk in enumerate(chunks, 1):
        title = chapter.title
        if len(chunks) > 1:
            title = f"{title} (part {chunk_idx}/{len(chunks)})"
        user = client.render(
            "scenes.jinja",
            cast_json=cast_json,
            chapter_id=chapter.chapter_id,
            title=title,
            text=chunk,
        )
        # Output budget tracks chunk length, capped well inside the model
        # context window (65K by default in serve_vllm.sh).
        budget = max(8192, min(28000, int(len(chunk) * 0.6)))
        result = client.structured(
            system=SYSTEM, user=user, schema=ChapterScenes, max_tokens=budget
        )
        for scene in result.scenes:
            scene.scene_id = f"{chapter.chapter_id}_s{len(all_scenes) + 1}"
            all_scenes.append(scene)

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
        result = segment_chapter(client, ch, cast)
        (scenes_dir / f"{ch.chapter_id}.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)
    return results

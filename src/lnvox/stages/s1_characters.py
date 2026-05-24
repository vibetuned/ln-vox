import json
from pathlib import Path

from lnvox.ingest.text import Chapter
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import CharacterList


SYSTEM = (
    "You extract structured character data from novel chapters. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation before or after the JSON. "
    "Base every claim strictly on evidence in the provided text."
)


def extract_per_chapter(client: LLMClient, chapter: Chapter) -> CharacterList:
    user = client.render(
        "characters_per_chapter.jinja",
        title=chapter.title,
        text=chapter.text,
    )
    # Per-chapter casts vary wildly — a chamber drama has 3-5 characters; a
    # dungeon raid or court scene can have 30+. Each entry costs ~200 tokens
    # of pretty-printed JSON. Budget scales with chapter length on the
    # assumption that longer chapters introduce more named entities. Capped
    # at 32K so we stay well inside Gemma 4's typical context window.
    budget = max(16384, min(32768, int(len(chapter.text) * 0.6)))
    return client.structured(
        system=SYSTEM, user=user, schema=CharacterList, max_tokens=budget
    )


def _load_prior_volume_casts(prior_volume_dirs: list[Path]) -> list[CharacterList]:
    """Read `01_characters.json` from each prior volume directory, oldest → newest."""
    out: list[CharacterList] = []
    for d in prior_volume_dirs:
        path = d / "01_characters.json"
        if path.exists():
            out.append(CharacterList.model_validate_json(
                path.read_text(encoding="utf-8")
            ))
    return out


def merge_chapters(
    client: LLMClient,
    per_chapter: list[CharacterList],
    *,
    prior_volume_casts: list[CharacterList] | None = None,
    current_volume_label: str = "",
) -> CharacterList:
    payload = [cl.model_dump() for cl in per_chapter]
    lists_json = json.dumps(payload, ensure_ascii=False, indent=2)

    prior_json = ""
    if prior_volume_casts:
        prior_payload = [cl.model_dump() for cl in prior_volume_casts]
        prior_json = json.dumps(prior_payload, ensure_ascii=False, indent=2)

    user = client.render(
        "characters_merge.jinja",
        chapter_lists_json=lists_json,
        prior_volume_casts=prior_json,
        current_volume=current_volume_label,
    )
    # The merged cast can contain 20+ characters with 2-4 sentence descriptions
    # and 2-3 evidence quotes apiece. Estimate ~600 tokens per character JSON;
    # cap at 32K tokens output to stay within Gemma 4's typical context.
    n_chars_est = sum(len(c.characters) for c in per_chapter) + sum(
        len(c.characters) for c in (prior_volume_casts or [])
    )
    budget = max(16384, min(32768, 800 * n_chars_est))
    return client.structured(
        system=SYSTEM, user=user, schema=CharacterList, max_tokens=budget
    )


def run(
    chapters: list[Chapter],
    client: LLMClient,
    output_dir: Path,
    *,
    prior_volume_dirs: list[Path] | None = None,
    current_volume_label: str = "",
    on_chapter_done=None,
) -> CharacterList:
    output_dir.mkdir(parents=True, exist_ok=True)
    per_chapter_dir = output_dir / "01_characters_per_chapter"
    per_chapter_dir.mkdir(exist_ok=True)

    per_chapter: list[CharacterList] = []
    for ch in chapters:
        # Per-chapter results are deterministic. If we already have one on
        # disk from a previous attempt (e.g. s1 crashed at chapter 10), load
        # it back instead of re-calling the LLM. This makes retry cheap.
        cached_path = per_chapter_dir / f"{ch.chapter_id}.json"
        if cached_path.exists():
            try:
                result = CharacterList.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                # File exists but is malformed (e.g. truncated). Re-extract.
                result = extract_per_chapter(client, ch)
                cached_path.write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
        else:
            result = extract_per_chapter(client, ch)
            cached_path.write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        per_chapter.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)

    prior_casts = _load_prior_volume_casts(prior_volume_dirs or [])

    if len(per_chapter) > 1 or prior_casts:
        merged = merge_chapters(
            client,
            per_chapter,
            prior_volume_casts=prior_casts,
            current_volume_label=current_volume_label,
        )
    else:
        merged = per_chapter[0]

    (output_dir / "01_characters.json").write_text(
        merged.model_dump_json(indent=2), encoding="utf-8"
    )
    return merged

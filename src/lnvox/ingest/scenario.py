"""Scenario-mode ingest: theater-script markdown → structured verbatim script.

DESIGN.md §17.2/§17.3. Scene/sequence headers are detected deterministically
(they chunk the LLM's work); a structuring LLM pass classifies each chunk's
lines into dialogue / staging / cue items. The hard invariant is VERBATIM
text: every dialogue item must be an exact (whitespace-collapsed) substring
of its cleaned source chunk — validated here, never trusted from the LLM.
Failed dialogue items are demoted to `staging` with a loud warning so no
line is silently rewritten or dropped.

IP rule (§17): script content is sent only to the locally-served LLM; no
script text may be embedded in code, prompts, or tests.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Callable

from lnvox.ingest.text import Chapter, write_jsonl
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import (
    Character,
    CharacterList,
    RosterEntry,
    RosterList,
    ScenarioScript,
    SceneStructure,
    ScriptItem,
    ScriptScene,
)

STRUCTURE_SYSTEM = (
    "You structure theater scripts into JSON without rewriting any text. "
    "Output a single raw JSON object matching the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)

# Scene / sequence header shapes seen across troupe scripts (generic patterns,
# not tied to any particular document): markdown headings that start with a
# number, French/English structural keywords, or a bold standalone number.
_HEADER_PATTERNS = [
    re.compile(r"^#{1,6}\s*\d+\s*[.\\)]?\s*\S"),
    re.compile(
        r"(?i)^\s*\**\s*(s[ée]quence|sc[èe]ne|acte?|p[ée]riode|tableau|partie)\s+[\divxlc]+"
    ),
    re.compile(r"^\*\*\s*\d+\s*\\?\.?\s*\*\*\s*$"),
    # Bare numbered headers: `3 – TITLE` / `**3 - Title**` (speaker labels are
    # names, not numbers, so a leading digit + dash is a safe scene signal).
    re.compile(r"^\*{0,2}\s*\d+\s*[–—-]\s+\S"),
]

# A heading that introduces the script's own cast list (FR + EN forms).
_ROSTER_HEADER = re.compile(
    r"(?i)^\s*(les\s+)?"
    r"(personnages?|characters?|cast|distribution|dramatis\s+person(ae|æ))"
    r"\b.{0,40}$"
)

# Group-speaker labels rendered with the Narrator fallback voice (§17.6).
_GROUP_STEMS = {"tous", "toutes", "tous ensemble", "ensemble", "all", "choeur"}

# Max characters of one chunk sent to the structure pass; overlong scenes are
# split at blank lines so the model never sees a truncated line.
_MAX_CHUNK_CHARS = 7000


# --------------------------------------------------------------------------- #
#  Deterministic text helpers (unit-tested without an LLM)
# --------------------------------------------------------------------------- #
def clean_md_line(line: str) -> str:
    """Strip markdown emphasis markers and backslash escapes, keep the words.

    Docx→markdown exports escape punctuation (backslash before -, !, ., +, …)
    and wrap names/cues in ** / *; the spoken text underneath is what we keep.
    """
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!?«»])", r"\1", line)
    s = s.replace("**", "")
    s = re.sub(r"(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", s)  # single-* emphasis
    return s.strip()


def norm_ws(s: str) -> str:
    return " ".join(s.split())


def is_scene_header(line: str) -> bool:
    return any(p.match(line.strip()) for p in _HEADER_PATTERNS)


def is_roster_header(line: str) -> bool:
    """A cast-list heading, NOT a staging line that merely mentions the word.

    Requires the roster word at line start AND heading-shaped formatting:
    a markdown heading, a fully-bold line, all-caps, or a bare 1-2 word line.
    """
    stripped = re.sub(r"^#+\s*", "", clean_md_line(line))
    if not _ROSTER_HEADER.match(stripped):
        return False
    raw = line.strip()
    heading_shaped = (
        raw.startswith("#")
        or (raw.startswith("**") and raw.rstrip().endswith("**"))
        or stripped.upper() == stripped
        or len(stripped.split()) <= 2
    )
    return heading_shaped


def chunk_script(md_text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a script into (roster_chunks, [(scene_title, scene_chunk), …]).

    A chunk runs from one detected header to the next. Text before the first
    header is kept as scene 0 only if it contains enough lines to matter
    (title pages / cue legends are dropped into no-man's-land otherwise).
    """
    lines = md_text.splitlines()
    roster_chunks: list[str] = []
    scenes: list[tuple[str, str]] = []

    current_title: str | None = None
    current: list[str] = []
    in_roster = False
    preamble: list[str] = []

    def flush() -> None:
        nonlocal current, current_title, in_roster
        body = "\n".join(current).strip()
        if body:
            if in_roster:
                roster_chunks.append(body)
            elif current_title is not None:
                scenes.append((current_title, body))
        current = []

    for line in lines:
        if is_roster_header(line) and not is_scene_header(line):
            flush()
            in_roster = True
            current_title = None
            continue
        if is_scene_header(line):
            flush()
            in_roster = False
            current_title = norm_ws(re.sub(r"^#+\s*", "", clean_md_line(line)))
            continue
        if current_title is None and not in_roster:
            preamble.append(line)
            continue
        current.append(line)
    flush()

    if not scenes:
        # No recognizable headers — the whole document is one scene.
        body = "\n".join(preamble).strip()
        if body:
            scenes.append(("", body))
    return roster_chunks, scenes


def split_chunk(chunk: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Split an overlong chunk at blank-line boundaries."""
    if len(chunk) <= max_chars:
        return [chunk]
    blocks = re.split(r"\n\s*\n", chunk)
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for b in blocks:
        if current and size + len(b) > max_chars:
            parts.append("\n\n".join(current))
            current, size = [], 0
        current.append(b)
        size += len(b) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts


def _label_stem(label: str) -> str:
    """Lowercased label with parentheticals, punctuation and md stripped."""
    s = clean_md_line(label)
    s = re.sub(r"\s*\(.*?\)", "", s)
    s = re.sub(r"[\s\-–:]+$", "", s)
    return norm_ws(s).lower()


def canonicalize_speakers(labels: list[str]) -> dict[str, str]:
    """Map every raw speaker label to one canonical form per character.

    Troupe scripts drift: inconsistent spacing around a parenthesized first
    name, a dash glued to the label, the short form with/without the
    parenthetical. Labels sharing a stem (parenthetical-stripped, lowercased)
    are one character; the canonical form is the most frequent cleaned
    variant, ties broken by length (the fuller label carries more info).
    """
    cleaned = {
        raw: norm_ws(re.sub(r"[\s\-–:]+$", "", clean_md_line(raw))) for raw in labels
    }
    freq = Counter(cleaned.values())
    by_stem: dict[str, list[str]] = {}
    for form in freq:
        by_stem.setdefault(_label_stem(form), []).append(form)
    canon_for_stem = {
        stem: max(forms, key=lambda f: (freq[f], len(f)))
        for stem, forms in by_stem.items()
    }
    return {raw: canon_for_stem[_label_stem(form)] for raw, form in cleaned.items()}


def is_group_label(label: str) -> bool:
    return _label_stem(label) in _GROUP_STEMS


def validate_items(items: list[ScriptItem], chunk: str) -> tuple[list[ScriptItem], int]:
    """Enforce the verbatim invariant; demote failing dialogue to staging.

    Returns (validated items, number of demotions).
    """
    haystack = norm_ws(
        " ".join(clean_md_line(ln) for ln in chunk.splitlines())
    ).lower()
    out: list[ScriptItem] = []
    demoted = 0
    for item in items:
        text = norm_ws(item.text)
        if not text:
            continue
        if item.type == "dialogue" and text.lower() not in haystack:
            out.append(ScriptItem(type="staging", speaker="", text=text))
            demoted += 1
            continue
        out.append(item.model_copy(update={"text": text, "speaker": norm_ws(item.speaker)}))
    return out, demoted


# --------------------------------------------------------------------------- #
#  LLM passes (content-cached)
# --------------------------------------------------------------------------- #
def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def cached_structured(
    client,
    *,
    cache_dir: Path | None,
    system: str,
    user: str,
    schema,
    desired: int,
    floor: int,
):
    """`client.structured` behind a content-addressed disk cache.

    Same philosophy as the s4 beat cache (§2.6): key = hash of the RENDERED
    prompt + schema name + model, so editing the script re-keys only the
    changed scenes, and a template or model change re-keys everything —
    stale results can't survive by construction. A crash mid-ingest resumes
    paying only for the chunks it hasn't structured yet.

    Returns (result, was_cache_hit).
    """
    path: Path | None = None
    if cache_dir is not None:
        key = _cache_key(schema.__name__, user, client.settings.llm.model)
        path = cache_dir / f"{key}.json"
        if path.exists():
            try:
                return schema.model_validate_json(path.read_text(encoding="utf-8")), True
            except Exception:
                pass  # corrupt/truncated entry — regenerate
    budget = client.budget_for(system=system, user=user, desired=desired, floor=floor)
    result = client.structured(
        system=system, user=user, schema=schema, max_tokens=budget
    )
    if path is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(), encoding="utf-8")
    return result, False


def structure_chunk(
    client: LLMClient, chunk: str, cache_dir: Path | None = None
) -> tuple[SceneStructure, bool]:
    user = client.render("scenario_structure.jinja", chunk=chunk)
    return cached_structured(
        client,
        cache_dir=cache_dir,
        system=STRUCTURE_SYSTEM,
        user=user,
        schema=SceneStructure,
        desired=max(4096, int(len(chunk) * 1.2)),
        floor=4096,
    )


def extract_roster(
    client: LLMClient, roster_chunks: list[str], cache_dir: Path | None = None
) -> tuple[list[RosterEntry], int]:
    entries: list[RosterEntry] = []
    hits = 0
    for chunk in roster_chunks:
        user = client.render("scenario_roster.jinja", chunk=chunk)
        result, cached = cached_structured(
            client,
            cache_dir=cache_dir,
            system=STRUCTURE_SYSTEM,
            user=user,
            schema=RosterList,
            desired=4096,
            floor=2048,
        )
        entries.extend(result.entries)
        hits += cached
    return entries, hits


def extract_characters(
    client: LLMClient,
    roster: list[RosterEntry],
    scenes: list[ScriptScene],
    cache_dir: Path | None = None,
) -> CharacterList:
    """Build the casting roster: script-given info + LLM gap-fill (§17.3).

    Group labels (everyone speaks) are excluded — they render with the
    Narrator fallback and are never cast.
    """
    samples: dict[str, list[str]] = {}
    order: list[str] = []
    for scene in scenes:
        for item in scene.items:
            if item.type != "dialogue" or not item.speaker:
                continue
            if is_group_label(item.speaker):
                continue
            if item.speaker not in samples:
                samples[item.speaker] = []
                order.append(item.speaker)
            if len(samples[item.speaker]) < 2:
                samples[item.speaker].append(item.text[:160])

    roster_by_stem = {_label_stem(e.name): e for e in roster}
    payload = [
        {
            "name": name,
            "bio": (roster_by_stem.get(_label_stem(name)) or RosterEntry(name="")).description,
            "sample_lines": samples[name],
        }
        for name in order
    ]
    if not payload:
        return CharacterList(characters=[])

    import json as _json

    user = client.render(
        "scenario_characters.jinja",
        speakers_json=_json.dumps(payload, ensure_ascii=False, indent=2),
    )
    result, _ = cached_structured(
        client,
        cache_dir=cache_dir,
        system=STRUCTURE_SYSTEM,
        user=user,
        schema=CharacterList,
        desired=max(4096, 200 * len(payload) + 1024),
        floor=4096,
    )
    # The LLM fills gender/age/description; the roster of names is ours.
    by_name = {c.name: c for c in result.characters}
    characters = [
        by_name.get(name)
        or Character(name=name, description=(roster_by_stem.get(_label_stem(name)) or RosterEntry(name="")).description or "Speaking role.")
        for name in order
    ]
    return CharacterList(characters=characters)


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def run(
    md_path: Path,
    scenario_id: str,
    client: LLMClient,
    out_dir: Path,
    *,
    title: str = "",
    language: str = "fr",
    cache_dir: Path | None = Path("cache") / "scenario",
    progress: Callable[[str], None] = print,
) -> ScenarioScript:
    """Structure one script markdown into artifacts/<id>/ (§17.3).

    LLM results are content-cached under `cache_dir` (None disables) — a
    re-run or crash-resume only pays for scenes whose text actually changed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    md_text = md_path.read_text(encoding="utf-8")
    roster_chunks, scene_chunks = chunk_script(md_text)
    progress(
        f"  {len(scene_chunks)} scene(s), {len(roster_chunks)} roster section(s) detected"
    )

    cache_hits = 0
    llm_calls = 0
    roster: list[RosterEntry] = []
    if roster_chunks:
        roster, hits = extract_roster(client, roster_chunks, cache_dir)
        cache_hits += hits
        llm_calls += len(roster_chunks)
    if roster:
        progress(f"  roster: {len(roster)} entrie(s)")

    scenes: list[ScriptScene] = []
    total_demoted = 0
    for i, (scene_title, chunk) in enumerate(scene_chunks):
        scene_id = f"{i + 1:02d}"
        items: list[ScriptItem] = []
        scene_hits = 0
        parts = split_chunk(chunk)
        for part in parts:
            structured, cached = structure_chunk(client, part, cache_dir)
            validated, demoted = validate_items(structured.items, part)
            items.extend(validated)
            total_demoted += demoted
            scene_hits += cached
        cache_hits += scene_hits
        llm_calls += len(parts)
        scenes.append(ScriptScene(scene_id=scene_id, title=scene_title, items=items))
        n_dlg = sum(1 for it in items if it.type == "dialogue")
        progress(
            f"  ✓ scene {scene_id}: {len(items)} item(s), {n_dlg} dialogue line(s)"
            f"{' (cached)' if parts and scene_hits == len(parts) else ''}"
        )
    if total_demoted:
        progress(
            f"  [warn] {total_demoted} dialogue line(s) failed verbatim validation "
            f"and were kept as staging — review 00_script.json"
        )

    # Canonicalize speaker-label variants across the whole script.
    labels = [
        it.speaker
        for sc in scenes
        for it in sc.items
        if it.type == "dialogue" and it.speaker
    ]
    canon = canonicalize_speakers(labels)
    for sc in scenes:
        sc.items = [
            it.model_copy(update={"speaker": canon.get(it.speaker, it.speaker)})
            if it.type == "dialogue"
            else it
            for it in sc.items
        ]

    script = ScenarioScript(
        scenario_id=scenario_id,
        title=title or norm_ws(clean_md_line(md_path.stem)),
        language=language,
        roster=roster,
        scenes=scenes,
    )
    (out_dir / "00_script.json").write_text(
        script.model_dump_json(indent=2), encoding="utf-8"
    )

    # One "chapter" per scene so s5 picks up titles for m4b chapter markers.
    chapters = [
        Chapter(
            chapter_id=sc.scene_id,
            title=sc.title or f"Scène {sc.scene_id}",
            text="\n\n".join(it.text for it in sc.items if it.type != "cue"),
        )
        for sc in script.scenes
    ]
    write_jsonl(chapters, out_dir / "00_text.jsonl")

    characters = extract_characters(client, roster, scenes, cache_dir)
    (out_dir / "01_characters.json").write_text(
        characters.model_dump_json(indent=2), encoding="utf-8"
    )
    progress(f"  ✓ {len(characters.characters)} character(s) for casting")
    if cache_dir is not None:
        progress(f"  LLM cache: {cache_hits}/{llm_calls + 1} call(s) served from {cache_dir}/")
    return script

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from lnvox.config import Settings
from lnvox.ingest.text import ingest_folder, read_jsonl, write_jsonl
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import (
    CharacterList,
    ChapterDirected,
    ChapterScenes,
    ScenarioScript,
    VoiceProfileList,
)
from lnvox.series import find_prior_volumes
from lnvox.stages import (
    s1_characters,
    s2_scenes,
    s3_director,
    s4_tts,
    s4_vibevoice,
    s5_mix,
    s6_sync,
    scenario_sync,
)
from lnvox.voices import manifest as voice_manifest
from lnvox.voices import matcher as voice_matcher
from lnvox.voices.schema import BookCasting


app = typer.Typer(help="ln-vox novel-to-audiobook pipeline", no_args_is_help=True)
console = Console()


def _book_dir(book_id: str) -> Path:
    return Settings().artifacts_dir / book_id


def _voicebank_dir() -> Path:
    # DESIGN.md §17.5: a per-language bank (e.g. voicebank-fr, seeded from the
    # French Common Voice corpus) is selected once via env, not per-command
    # flags. Default stays the historical English bank.
    import os

    return Path(os.environ.get("LNVOX_VOICEBANK", "voicebank"))


def _filter_chapters(chapters, selected: Optional[str]):
    if not selected:
        return chapters
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    return [c for c in chapters if c.chapter_id in wanted]


@app.command()
def ingest(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    book_id: Optional[str] = typer.Option(None, help="Defaults to folder name."),
    mode: str = typer.Option(
        "narration",
        "--mode",
        help=(
            "narration (default) or lecture. In lecture mode a Narrator-only "
            "01_characters.json stub is written so `voice cast` runs without s1."
        ),
    ),
):
    """Stage 0: Parse a folder of chapter .txt files into JSONL.

    If the folder contains a `.epub_meta.json` sidecar (produced by
    `lnvox ingest-epub`), it's copied to the book's artifacts directory as
    `00_book_meta.json` so Stage 5 can embed the cover image in the m4b.
    """
    import json as _json
    import shutil

    book_id = book_id or folder.name
    chapters = ingest_folder(folder)
    if not chapters:
        console.print(f"[red]No .txt files found in {folder}[/]")
        raise typer.Exit(1)

    out_dir = _book_dir(book_id)
    output = out_dir / "00_text.jsonl"
    write_jsonl(chapters, output)

    # Lecture mode has no Stage 1 (no characters). Write a minimal cast so
    # `lnvox voice cast` runs unchanged — cast_book() synthesizes the Narrator
    # from an empty list. Don't clobber an existing real cast.
    if mode == "lecture":
        cast_file = out_dir / "01_characters.json"
        if not cast_file.exists():
            cast_file.write_text('{"characters": []}', encoding="utf-8")
            console.print(
                f"[dim]Lecture mode: wrote Narrator-only cast stub → {cast_file}[/]"
            )

    # If this folder was produced by `ingest-epub`, propagate its metadata
    # (title, authors, cover image path) to the artifacts dir for later stages.
    epub_meta = folder / ".epub_meta.json"
    if epub_meta.exists():
        data = _json.loads(epub_meta.read_text(encoding="utf-8"))
        # Make cover_image absolute so s5 doesn't need to know the source folder.
        if data.get("cover_image"):
            data["cover_image"] = str((folder / data["cover_image"]).resolve())
        (out_dir / "00_book_meta.json").write_text(
            _json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    table = Table(title=f"Ingested {len(chapters)} chapter(s) → {output}")
    table.add_column("id")
    table.add_column("title", overflow="fold")
    table.add_column("chars", justify="right")
    for ch in chapters:
        table.add_row(ch.chapter_id, ch.title[:80], f"{len(ch.text):,}")
    console.print(table)
    if epub_meta.exists():
        console.print(
            f"[dim]Propagated EPUB metadata to {out_dir / '00_book_meta.json'}[/]"
        )


@app.command(name="ingest-epub")
def ingest_epub(
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, file_okay=True),
    output_dir: Path = typer.Argument(..., help="Destination folder (e.g. novels/level99/volume-01)"),
    mode: str = typer.Option(
        "narration",
        "--mode",
        help=(
            "narration (multi-voice novel) or lecture (single-voice non-fiction: "
            "classify blocks, drop boilerplate, render code/tables to images). "
            "See DESIGN.md §13."
        ),
    ),
    classifier: str = typer.Option(
        "fallback",
        "--ingest-classifier",
        help="Lecture mode: 'fallback' (LLM only on untagged blocks) or 'none' (rules only).",
    ),
    no_render: bool = typer.Option(
        False,
        "--no-render",
        help="Lecture mode: skip rasterizing code/tables to PNG (record HTML-only).",
    ),
):
    """Stage 0a: Extract an EPUB into the novels/-folder layout.

    Writes `NN-slug.txt` per narrative chapter, dumps every illustration to
    `images/`, and stores `.epub_meta.json` so the cover can later be embedded
    in the final m4b.

    Run `lnvox ingest <output_dir>` next to feed it into the rest of the pipeline.
    """
    from lnvox.ingest.epub import extract_epub

    if mode not in ("narration", "lecture"):
        console.print(f"[red]--mode must be 'narration' or 'lecture', got {mode!r}.[/]")
        raise typer.Exit(2)

    # Lecture mode uses the LLM only as a fallback classifier (§13.2a). Build a
    # client lazily so narration extraction never needs an endpoint.
    llm = None
    if mode == "lecture" and classifier == "fallback":
        llm = LLMClient()
        console.print(
            f"[dim]Block classifier fallback via {llm.settings.llm.endpoint} "
            f"(model={llm.settings.llm.model})[/]"
        )

    console.print(
        f"Extracting [bold]{epub_path}[/] → [bold]{output_dir}[/] (mode={mode})…"
    )
    meta = extract_epub(
        epub_path,
        output_dir,
        mode=mode,
        classifier=classifier,
        render=not no_render,
        llm=llm,
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )

    table = Table(title=f"{meta.title or '<untitled>'}")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("authors", ", ".join(meta.authors) or "—")
    table.add_row("publisher", meta.publisher or "—")
    table.add_row("language", meta.language or "—")
    table.add_row("cover image", meta.cover_image or "—")
    table.add_row("images", str(len(meta.images)))
    table.add_row("chapters", str(len(meta.chapters)))
    if meta.visual_elements:
        table.add_row("visual elements", str(len(meta.visual_elements)))
    console.print(table)


@app.command(name="s1")
def stage1(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
):
    """Stage 1: Extract characters from ingested chapters via Gemma 4."""
    out_dir = _book_dir(book_id)
    text_file = out_dir / "00_text.jsonl"
    if not text_file.exists():
        console.print(f"[red]Missing ingest output at {text_file}. Run `lnvox ingest` first.[/]")
        raise typer.Exit(1)

    selected = _filter_chapters(read_jsonl(text_file), chapters)
    if not selected:
        console.print(f"[red]No chapters matched filter '{chapters}'.[/]")
        raise typer.Exit(1)

    client = LLMClient()
    console.print(
        f"[dim]endpoint={client.settings.llm.endpoint} model={client.settings.llm.model}[/]"
    )

    # Auto-detect prior volumes in the same series and feed their cast lists
    # into the merge step (cross-volume continuity).
    artifacts_dir = Settings().artifacts_dir
    priors = find_prior_volumes(artifacts_dir, book_id)
    if priors:
        console.print(
            f"[dim]Found {len(priors)} prior volume(s): {[p.name for p in priors]}[/]"
        )
    console.print(f"Extracting characters from {len(selected)} chapter(s)…")

    def _progress(ch, result):
        console.print(
            f"  [green]✓[/] {ch.chapter_id}: found {len(result.characters)} character(s)"
        )

    current_label = book_id.rsplit("/", 1)[-1] if "/" in book_id else book_id
    merged = s1_characters.run(
        selected,
        client,
        out_dir,
        prior_volume_dirs=priors,
        current_volume_label=current_label,
        on_chapter_done=_progress,
    )

    table = Table(title=f"{len(merged.characters)} character(s) → {out_dir / '01_characters.json'}")
    table.add_column("name")
    table.add_column("aliases", overflow="fold")
    table.add_column("gender")
    table.add_column("age")
    table.add_column("description", overflow="fold")
    for c in merged.characters:
        desc = c.description if len(c.description) <= 120 else c.description[:117] + "…"
        table.add_row(c.name, ", ".join(c.aliases), c.gender, c.approx_age, desc)
    console.print(table)


@app.command(name="s2")
def stage2(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
):
    """Stage 2: Split chapters into scenes and tag dialogue speakers."""
    out_dir = _book_dir(book_id)
    text_file = out_dir / "00_text.jsonl"
    cast_file = out_dir / "01_characters.json"
    if not text_file.exists():
        console.print(f"[red]Missing ingest output. Run `lnvox ingest`.[/]")
        raise typer.Exit(1)
    if not cast_file.exists():
        console.print(f"[red]Missing cast at {cast_file}. Run `lnvox s1`.[/]")
        raise typer.Exit(1)

    selected = _filter_chapters(read_jsonl(text_file), chapters)
    if not selected:
        console.print(f"[red]No chapters matched filter '{chapters}'.[/]")
        raise typer.Exit(1)

    cast = CharacterList.model_validate_json(cast_file.read_text(encoding="utf-8"))
    client = LLMClient()
    console.print(
        f"[dim]endpoint={client.settings.llm.endpoint} model={client.settings.llm.model}[/]"
    )

    def _progress(ch, result):
        n_d = sum(1 for s in result.scenes for b in s.beats if b.type == "dialogue")
        n_n = sum(1 for s in result.scenes for b in s.beats if b.type == "narration")
        console.print(
            f"  [green]✓[/] {ch.chapter_id}: {len(result.scenes)} scene(s), "
            f"{n_n} narration / {n_d} dialogue"
        )

    results = s2_scenes.run(selected, cast, client, out_dir, on_chapter_done=_progress)

    table = Table(title="Stage 2 summary")
    table.add_column("chapter")
    table.add_column("scenes", justify="right")
    table.add_column("narration", justify="right")
    table.add_column("dialogue", justify="right")
    table.add_column("speakers seen", overflow="fold")
    for r in results:
        speakers = sorted({
            b.speaker for s in r.scenes for b in s.beats
            if b.type == "dialogue" and b.speaker
        })
        n_d = sum(1 for s in r.scenes for b in s.beats if b.type == "dialogue")
        n_n = sum(1 for s in r.scenes for b in s.beats if b.type == "narration")
        table.add_row(
            r.chapter_id, str(len(r.scenes)), str(n_n), str(n_d), ", ".join(speakers)
        )
    console.print(table)


@app.command(name="s3")
def stage3(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
    regen_profiles: bool = typer.Option(
        False,
        "--regen-profiles",
        help="Delete and re-generate the cached voice profiles before directing.",
    ),
):
    """Stage 3: Generate Dramabox-ready stage directions per beat.

    Run AFTER `lnvox voice cast` — the director consults the assigned voice
    clip's metadata to write descriptors matching the actual reference voice.
    """
    out_dir = _book_dir(book_id)
    cast_file = out_dir / "01_characters.json"
    scenes_dir = out_dir / "02_scenes"
    assign_file = out_dir / "04_voice_assignments.json"
    if not cast_file.exists():
        console.print(f"[red]Missing cast at {cast_file}. Run `lnvox s1`.[/]")
        raise typer.Exit(1)
    if not scenes_dir.exists():
        console.print(f"[red]Missing scenes at {scenes_dir}. Run `lnvox s2`.[/]")
        raise typer.Exit(1)
    if not assign_file.exists():
        console.print(
            f"[red]Missing voice assignments at {assign_file}. "
            f"Run `lnvox voice cast {book_id}` first (Stage V runs before s3 in v2).[/]"
        )
        raise typer.Exit(1)
    if regen_profiles:
        prof_file = out_dir / "03_voice_profiles.json"
        if prof_file.exists():
            prof_file.unlink()
            console.print(
                f"[dim]Removed cached voice profiles at {prof_file}; will regenerate.[/]"
            )

    cast = CharacterList.model_validate_json(cast_file.read_text(encoding="utf-8"))
    wanted = {s.strip() for s in chapters.split(",")} if chapters else None
    chapter_scenes: list[ChapterScenes] = []
    for path in sorted(scenes_dir.glob("*.json")):
        cs = ChapterScenes.model_validate_json(path.read_text(encoding="utf-8"))
        if wanted is None or cs.chapter_id in wanted:
            chapter_scenes.append(cs)
    if not chapter_scenes:
        console.print(f"[red]No scenes matched filter '{chapters}'.[/]")
        raise typer.Exit(1)

    client = LLMClient()
    console.print(
        f"[dim]endpoint={client.settings.llm.endpoint} model={client.settings.llm.model}[/]"
    )
    console.print(
        f"Directing {sum(len(c.scenes) for c in chapter_scenes)} scene(s) "
        f"across {len(chapter_scenes)} chapter(s)…"
    )

    def _progress(cs, result):
        merged_beats = sum(len(s.beats) for s in result.scenes)
        n_d = sum(1 for s in result.scenes for b in s.beats if b.type == "dialogue")
        console.print(
            f"  [green]✓[/] {cs.chapter_id}: {len(result.scenes)} scene(s), "
            f"{merged_beats} merged beat(s) ({n_d} dialogue)"
        )

    # Load voice assignments + voicebank so the director can match descriptors
    # to the actually-assigned reference clips.
    casting = BookCasting.model_validate_json(
        assign_file.read_text(encoding="utf-8")
    )
    voicebank = voice_manifest.load(_voicebank_dir())

    results = s3_director.run(
        chapter_scenes,
        cast,
        client,
        out_dir,
        casting=casting,
        voicebank=voicebank,
        on_chapter_done=_progress,
    )

    table = Table(title="Stage 3 summary (post-merge)")
    table.add_column("chapter")
    table.add_column("scenes", justify="right")
    table.add_column("beats", justify="right")
    table.add_column("dialogue", justify="right")
    for r in results:
        beats = sum(len(s.beats) for s in r.scenes)
        n_d = sum(1 for s in r.scenes for b in s.beats if b.type == "dialogue")
        table.add_row(r.chapter_id, str(len(r.scenes)), str(beats), str(n_d))
    console.print(table)


@app.command(name="lecture")
def stage_lecture(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
    no_normalize: bool = typer.Option(
        False,
        "--no-normalize",
        help="Skip the LLM speech-normalize pass; read text verbatim (text == source_span).",
    ),
):
    """Lecture mode: build single-voice narration beats (replaces s1/s2/s3).

    Splits each chapter's prose into TTS-sized narration beats and (unless
    --no-normalize) speech-normalizes each beat's text while keeping source_span
    verbatim. Writes 03_directed/*.json directly, so s4/s5/s6 run unchanged.

    Run AFTER `lnvox voice cast <book> --narrator-clip …` so the narrator's
    descriptor is available. See DESIGN.md §13.3.
    """
    from lnvox.stages import lecture as lecture_stage

    out_dir = _book_dir(book_id)
    text_file = out_dir / "00_text.jsonl"
    assign_file = out_dir / "04_voice_assignments.json"
    if not text_file.exists():
        console.print(f"[red]Missing ingest output at {text_file}. Run `lnvox ingest`.[/]")
        raise typer.Exit(1)

    selected = _filter_chapters(read_jsonl(text_file), chapters)
    if not selected:
        console.print(f"[red]No chapters matched filter '{chapters}'.[/]")
        raise typer.Exit(1)

    casting = None
    if assign_file.exists():
        casting = BookCasting.model_validate_json(assign_file.read_text(encoding="utf-8"))
    else:
        console.print(
            f"[yellow]No {assign_file} — using the default narrator descriptor. "
            f"Run `lnvox voice cast {book_id} --narrator-clip …` for a real voice.[/]"
        )

    client = None
    if not no_normalize:
        client = LLMClient()
        console.print(
            f"[dim]Speech-normalize via {client.settings.llm.endpoint} "
            f"(model={client.settings.llm.model})[/]"
        )
    else:
        console.print("[dim]--no-normalize: reading text verbatim (no LLM).[/]")

    console.print(
        f"Building lecture beats for {len(selected)} chapter(s) "
        f"(narrator: {lecture_stage.narrator_descriptor(casting)})…"
    )

    def _progress(result):
        n = sum(len(s.beats) for s in result.scenes)
        console.print(f"  [green]✓[/] {result.chapter_id}: {n} narration beat(s)")

    results = lecture_stage.run(
        selected,
        out_dir,
        casting=casting,
        client=client,
        normalize=not no_normalize,
        on_chapter_done=_progress,
    )

    total = sum(len(s.beats) for r in results for s in r.scenes)
    console.print(
        f"[green]Done.[/] {total} beat(s) across {len(results)} chapter(s) "
        f"→ {out_dir / '03_directed'}/"
    )


@app.command(name="ingest-scenario")
def ingest_scenario(
    md_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Theater-script markdown file."
    ),
    id: str = typer.Option(..., "--id", help="Artifact id (e.g. 'my-play')."),
    title: Optional[str] = typer.Option(None, help="Play title (default: filename)."),
    language: str = typer.Option("fr", help="Script language tag stored in 00_script.json."),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help=(
            "Bypass the cache/scenario/ LLM cache (content-keyed on script "
            "text + prompt + model) — use to re-roll the structuring."
        ),
    ),
):
    """Scenario mode (DESIGN.md §17): structure a theater script.

    LLM-classifies each scene's lines into dialogue / staging / cue items
    with a VERBATIM-text invariant (validated in code), extracts the
    script's own characters section when present, and prepares the casting
    roster. Needs the LLM server running.

    Output: artifacts/<id>/00_script.json, 00_text.jsonl, 01_characters.json.
    Next: `lnvox voice cast <id>`, then `lnvox scenario <id>`.
    """
    from lnvox.ingest import scenario as scenario_ingest

    out_dir = _book_dir(id)
    client = LLMClient()
    console.print(f"Structuring {md_path.name} → {out_dir}/")
    script = scenario_ingest.run(
        md_path,
        id,
        client,
        out_dir,
        title=title or "",
        language=language,
        cache_dir=None if no_cache else Path("cache") / "scenario",
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )
    n_items = sum(len(s.items) for s in script.scenes)
    console.print(
        f"[green]Done.[/] {len(script.scenes)} scene(s), {n_items} item(s) → "
        f"{out_dir / '00_script.json'}\n"
        f"Next: `lnvox voice cast {id}` then `lnvox scenario {id}`."
    )


@app.command(name="scenario")
def stage_scenario(book_id: str):
    """Scenario mode direction pass (DESIGN.md §17.3).

    Adds per-line emotion (7-value enum) + a short cue in the script's
    language, composes the TTS prompts, and writes 03_directed/*.json so
    s4/s5 run unchanged (one chapter per scene; staging pauses become
    DirectedScene boundaries). Run AFTER `lnvox voice cast` so descriptors
    match the assigned clips. Group lines render with the Narrator voice.
    """
    from lnvox.stages import scenario as scenario_stage

    out_dir = _book_dir(book_id)
    script_file = out_dir / "00_script.json"
    if not script_file.exists():
        console.print(f"[red]Missing {script_file}. Run `lnvox ingest-scenario`.[/]")
        raise typer.Exit(1)
    script = ScenarioScript.model_validate_json(script_file.read_text(encoding="utf-8"))

    chars_file = out_dir / "01_characters.json"
    cast = (
        CharacterList.model_validate_json(chars_file.read_text(encoding="utf-8"))
        if chars_file.exists()
        else CharacterList(characters=[])
    )

    assign_file = out_dir / "04_voice_assignments.json"
    casting = None
    voicebank = None
    if assign_file.exists():
        casting = BookCasting.model_validate_json(assign_file.read_text(encoding="utf-8"))
        vb_dir = _voicebank_dir()
        if (vb_dir / "manifest.json").exists():
            voicebank = voice_manifest.load(vb_dir)
    else:
        console.print(
            "[yellow]No 04_voice_assignments.json — descriptors won't be anchored "
            f"to clips. Run `lnvox voice cast {book_id}` first for best results.[/]"
        )

    client = LLMClient()

    def _progress(scene, result):
        beats = sum(len(s.beats) for s in result.scenes)
        console.print(
            f"[dim]  ✓ scene {scene.scene_id}: {beats} beat(s), "
            f"{len(result.scenes)} staging-delimited group(s)[/]"
        )

    results = scenario_stage.run(
        script,
        cast,
        client,
        out_dir,
        casting=casting,
        voicebank=voicebank,
        on_scene_done=_progress,
    )
    total = sum(len(s.beats) for r in results for s in r.scenes)
    console.print(
        f"[green]Done.[/] {total} beat(s) across {len(results)} scene(s) → "
        f"{out_dir / '03_directed'}/\n"
        f"Next: `lnvox s4 {book_id}`, `lnvox s5 {book_id}`, "
        f"`lnvox scenario-sync {book_id}`."
    )


@app.command(name="scenario-sync")
def stage_scenario_sync(
    book_id: str,
    intra: float = typer.Option(
        0.25, help="Pad between lines of one run — MUST match `lnvox s5 --intra`."
    ),
    staging_pause: float = typer.Option(
        1.0,
        help="Pause where staging happens — MUST match `lnvox s5 --inter-scene`.",
    ),
    inter_scene: float = typer.Option(
        scenario_sync.SCENARIO_INTER_SCENE,
        help=(
            "Pad between script scenes — MUST match `lnvox s5 --inter-chapter`. "
            "Play default 0.75 s: the 2 s book pad drags between scenes (§17.4)."
        ),
    ),
    lead_in: float = typer.Option(
        scenario_sync.SCENARIO_LEAD_IN,
        "--lead-in",
        help=(
            "Per-beat lead-in silence — MUST match `lnvox s5 --lead-in`. "
            "0.35 s covers the reader app's ~0.3 s playback poll (§17.4)."
        ),
    ),
):
    """Scenario mode sync emitter (DESIGN.md §17.4).

    Emits 07_sync/<scene>.json + play.json + play.srt: one timed entry per
    script item (dialogue spans, staging pauses, zero-duration cues),
    computed from the same plan as the s5 mix so both agree. Requires
    per-beat rendering (dramabox backend).
    """
    out_dir = _book_dir(book_id)
    if not (out_dir / "00_script.json").exists():
        console.print(f"[red]Missing {out_dir / '00_script.json'}. Run `lnvox ingest-scenario`.[/]")
        raise typer.Exit(1)
    try:
        scenario_sync.run(
            out_dir,
            intra=intra,
            staging_pause=staging_pause,
            inter_scene=inter_scene,
            lead_in=lead_in,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Done.[/] Sync under {out_dir / '07_sync'}/.")


def _default_tts_device() -> str:
    """Pick a torch device for Dramabox when the CLI flag isn't passed.

    Returns "mps" on Apple Silicon when torch reports MPS available; "cuda"
    everywhere else (matches the historical default on Linux). Torch is
    imported lazily so the rest of the CLI doesn't pay the import cost when
    s4 isn't being run. See DESIGN.md §11.3.
    """
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cuda"


@app.command(name="s4")
def stage4(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
    limit: Optional[int] = typer.Option(
        None,
        help="Render at most N beats overall (smoke-test mode).",
    ),
    device: Optional[str] = typer.Option(
        None,
        help=(
            "Torch device for Dramabox. Defaults to 'mps' on Apple Silicon "
            "when available, else 'cuda'. Pass 'cpu' to force CPU."
        ),
    ),
    staged: bool = typer.Option(
        False,
        "--staged",
        help=(
            "Run the crash-isolated staged pipeline (DESIGN.md §15): four "
            "checkpointed phases, one model in VRAM at a time, subprocess "
            "per phase with automatic resume."
        ),
    ),
    keep_staged: bool = typer.Option(
        False,
        help="Keep _staged/ intermediates after a successful staged run (debugging).",
    ),
    tts_backend: Optional[str] = typer.Option(
        None,
        "--tts-backend",
        help=(
            "TTS engine: 'dramabox' (default, per-beat → 05_audio/) or "
            "'vibevoice' (multi-speaker sessions → 05_audio_v2/, DESIGN.md "
            "§16). Falls back to $LNVOX_TTS_BACKEND, else dramabox."
        ),
    ),
    max_session_chars: int = typer.Option(
        s4_vibevoice.DEFAULT_MAX_SESSION_CHARS,
        "--max-session-chars",
        help=(
            "vibevoice only: max characters per generation session — the "
            "cache-granularity vs prosody-continuity tradeoff (DESIGN.md §16.3)."
        ),
    ),
    no_trim: bool = typer.Option(
        False,
        "--no-trim",
        help=(
            "Keep the engine-emitted leading/trailing silence (the edge-"
            "silence trim is on by default — DESIGN.md §2.6)."
        ),
    ),
):
    """Stage 4: Render directed beats to audio via the selected TTS backend.

    Backends (DESIGN.md §16): `dramabox` renders per beat into 05_audio/;
    `vibevoice` renders multi-speaker scene sessions into 05_audio_v2/
    (mix those with `lnvox s5 --v2`).

    Inputs:
      - artifacts/<book>/03_directed/*.json (directed beats)
      - artifacts/<book>/04_voice_assignments.json (character → clip)
      - voicebank/manifest.json + voicebank/clips/*.wav

    Output:
      - artifacts/<book>/05_audio[_v2]/<chapter>/<id>.wav
      - artifacts/<book>/05_audio[_v2]/<chapter>/manifest.json
    """
    import os

    backend = (tts_backend or os.environ.get("LNVOX_TTS_BACKEND") or "dramabox").lower()
    if backend not in ("dramabox", "vibevoice"):
        console.print(
            f"[red]Unknown --tts-backend '{backend}' (expected dramabox|vibevoice).[/]"
        )
        raise typer.Exit(2)
    if backend == "vibevoice" and staged:
        console.print(
            "[red]--staged is Dramabox-only (DESIGN.md §16.2). VibeVoice is a "
            "single-model boot — use the monolithic path (s4_retry.sh covers "
            "crashes).[/]"
        )
        raise typer.Exit(2)
    if no_trim:
        # Read by tts.trim / VibeVoiceClient; inherited by the staged-phase
        # subprocesses, so one env var covers all three render paths.
        os.environ["LNVOX_S4_NO_TRIM"] = "1"
    out_dir = _book_dir(book_id)
    directed_dir = out_dir / "03_directed"
    assign_file = out_dir / "04_voice_assignments.json"
    if not directed_dir.exists():
        console.print(f"[red]Missing {directed_dir}. Run `lnvox s3`.[/]")
        raise typer.Exit(1)
    if not assign_file.exists():
        console.print(f"[red]Missing {assign_file}. Run `lnvox voice cast`.[/]")
        raise typer.Exit(1)

    vb_dir = _voicebank_dir()
    voicebank = voice_manifest.load(vb_dir)
    if not voicebank.clips:
        console.print(
            f"[red]Voicebank empty. Run `lnvox voice seed-cv` first.[/]"
        )
        raise typer.Exit(1)

    casting = BookCasting.model_validate_json(
        assign_file.read_text(encoding="utf-8")
    )

    wanted = {s.strip() for s in chapters.split(",")} if chapters else None
    chapters_loaded: list[ChapterDirected] = []
    for path in sorted(directed_dir.glob("*.json")):
        cd = ChapterDirected.model_validate_json(path.read_text(encoding="utf-8"))
        if wanted is None or cd.chapter_id in wanted:
            chapters_loaded.append(cd)
    if not chapters_loaded:
        console.print(f"[red]No directed chapters matched filter '{chapters}'.[/]")
        raise typer.Exit(1)

    if device is None:
        device = _default_tts_device()
        console.print(f"[dim]Auto-detected device: {device}[/]")

    audio_dir = out_dir / ("05_audio_v2" if backend == "vibevoice" else "05_audio")
    cache_dir = Path("cache") / "tts"

    def _progress(msg):
        console.print(f"[dim]{msg}[/]")

    total_beats = sum(len(s.beats) for ch in chapters_loaded for s in ch.scenes)
    if limit is not None:
        total_beats = min(total_beats, limit)
    console.print(
        f"Rendering {total_beats} beat(s) across {len(chapters_loaded)} chapter(s) "
        f"→ {audio_dir} [{backend}]"
    )

    if backend == "vibevoice":
        from lnvox.tts.vibevoice_client import VibeVoiceClient

        def _vv_factory():
            return VibeVoiceClient(device=device)

        s4_vibevoice.run(
            chapters_loaded,
            casting,
            voicebank,
            vb_dir,
            audio_dir,
            cache_dir,
            client_factory=_vv_factory,
            model_version=VibeVoiceClient.MODEL_VERSION,
            max_session_chars=max_session_chars,
            progress=_progress,
            limit=limit,
        )
        console.print(
            f"[green]Done (vibevoice).[/] Audio under {audio_dir}/, cache under "
            f"{cache_dir}/. Mix with `lnvox s5 {book_id} --v2`."
        )
        return

    def _factory():
        from lnvox.tts.dramabox_client import DramaboxClient

        return DramaboxClient(device=device)

    if staged:
        from lnvox.tts import staged_driver

        staged_driver.run_staged(
            book_id=book_id,
            chapters=chapters_loaded,
            casting=casting,
            voicebank=voicebank,
            voicebank_root=vb_dir,
            book_dir=out_dir,
            cache_dir=cache_dir,
            device=device,
            limit=limit,
            keep_staged=keep_staged,
            progress=_progress,
        )
        console.print(
            f"[green]Done (staged).[/] Audio under {audio_dir}/, cache under {cache_dir}/."
        )
        return

    from lnvox.tts.dramabox_client import DramaboxClient

    s4_tts.run(
        chapters_loaded,
        casting,
        voicebank,
        vb_dir,
        audio_dir,
        cache_dir,
        client_factory=_factory,
        model_version=DramaboxClient.MODEL_VERSION,
        progress=_progress,
        limit=limit,
    )
    console.print(f"[green]Done.[/] Audio under {audio_dir}/, cache under {cache_dir}/.")


@app.command(name="s4-phase", hidden=True)
def stage4_phase(
    phase: str,
    book_id: str,
    device: Optional[str] = typer.Option(None, help="Torch device (default: auto)."),
):
    """Internal: run ONE staged-s4 phase sweep (DESIGN.md §15).

    Spawned as a subprocess by `lnvox s4 --staged` so each attempt gets a
    fresh CUDA context; exits non-zero on any failure and is relaunched by
    the driver, resuming from the per-item files already on disk.
    """
    from lnvox.tts import staged

    if phase not in staged.PHASES:
        console.print(f"[red]Unknown phase '{phase}'. Expected one of {staged.PHASES}.[/]")
        raise typer.Exit(2)
    if device is None:
        device = _default_tts_device()
    staged.run_phase(
        phase,
        book_dir=_book_dir(book_id),
        cache_dir=Path("cache") / "tts",
        device=device,
    )


@app.command(name="s5")
def stage5(
    book_id: str,
    chapters: Optional[str] = typer.Option(
        None, help="Comma-separated chapter ids (default: all)."
    ),
    title: Optional[str] = typer.Option(
        None, help="Book title for the m4b (defaults to book_id)."
    ),
    intra: float = typer.Option(0.25, help="Intra-scene silence in seconds."),
    inter_scene: float = typer.Option(1.0, help="Inter-scene silence in seconds."),
    inter_chapter: float = typer.Option(2.0, help="Inter-chapter silence in seconds."),
    lead_in: float = typer.Option(
        0.0,
        "--lead-in",
        help=(
            "Extra silence before EVERY beat. Scenario mode uses 0.35 so the "
            "reader app's ~0.3 s playback poll lands in silence before each "
            "line (DESIGN.md §17.4); books keep 0."
        ),
    ),
    lufs: float = typer.Option(-18.0, help="Target loudness in LUFS."),
    kbps: int = typer.Option(96, help="AAC bitrate (kbps)."),
    cover: Optional[Path] = typer.Option(
        None,
        help="Path to a cover image to embed. Auto-detected from 00_book_meta.json if omitted.",
    ),
    images_dir: Optional[Path] = typer.Option(
        None,
        "--images-dir",
        help=(
            "Directory of additional images to embed (illustrations, back cover, "
            "etc.). Auto-detected from novels/<book>/images/ if omitted. "
            "Each image becomes a separate attached_pic stream with title=<stem>."
        ),
    ),
    novels_root: Path = typer.Option(
        Path("novels"),
        "--novels-root",
        help="Where chapter .txt files (and the images/ sibling) live.",
    ),
    v2: bool = typer.Option(
        False,
        "--v2",
        help=(
            "Mix from 05_audio_v2/ (VibeVoice session renders, DESIGN.md §16) "
            "instead of 05_audio/."
        ),
    ),
):
    """Stage 5: Mix rendered beats into a final .m4b with chapter markers."""
    from lnvox.tts.schema import ChapterAudio

    out_dir = _book_dir(book_id)
    audio_root = out_dir / ("05_audio_v2" if v2 else "05_audio")
    text_jsonl = out_dir / "00_text.jsonl"
    if not audio_root.exists():
        hint = "lnvox s4 --tts-backend vibevoice" if v2 else "lnvox s4"
        console.print(f"[red]Missing {audio_root}. Run `{hint}`.[/]")
        raise typer.Exit(1)

    # Load chapter titles from the ingest output.
    titles: dict[str, str] = {}
    if text_jsonl.exists():
        for ch in read_jsonl(text_jsonl):
            titles[ch.chapter_id] = ch.title

    wanted = {s.strip() for s in chapters.split(",")} if chapters else None
    chapters_audio: list[ChapterAudio] = []
    for chap_dir in sorted(audio_root.iterdir()):
        manifest = chap_dir / "manifest.json"
        if not manifest.exists():
            continue
        cid = chap_dir.name
        if wanted is not None and cid not in wanted:
            continue
        chapters_audio.append(
            ChapterAudio.model_validate_json(manifest.read_text(encoding="utf-8"))
        )
    if not chapters_audio:
        console.print(f"[red]No rendered chapter manifests found under {audio_root}.[/]")
        raise typer.Exit(1)

    final_dir = out_dir / "06_final"
    book_title = title or book_id

    console.print(
        f"Mixing {len(chapters_audio)} chapter(s), "
        f"{sum(len(c.beats) for c in chapters_audio)} beat(s) total. "
        f"Output: {final_dir}/{book_title}.m4b"
    )

    # Auto-detect cover image from 00_book_meta.json if not passed.
    cover_image: Optional[Path] = cover
    if cover_image is None:
        book_meta_path = out_dir / "00_book_meta.json"
        if book_meta_path.exists():
            import json as _json

            book_meta = _json.loads(book_meta_path.read_text(encoding="utf-8"))
            if book_meta.get("cover_image"):
                cover_image = Path(book_meta["cover_image"])
                if not cover_image.exists():
                    console.print(
                        f"[yellow]Cover image referenced by 00_book_meta.json "
                        f"is missing: {cover_image} — skipping[/]"
                    )
                    cover_image = None
    if cover_image is not None:
        console.print(f"[dim]Embedding cover: {cover_image}[/]")

    # Resolve images directory: explicit flag wins, otherwise check
    # <novels_root>/<book_id>/images/.
    novel_dir = novels_root / book_id
    resolved_images_dir = images_dir if images_dir else (novel_dir / "images")
    extra_images: list[Path] = []
    if resolved_images_dir.exists():
        # Prefer the spine-ordered image list from `.epub_meta.json` when
        # available — that's how a reader encounters the illustrations
        # (`Insert1, Insert2, …, Insert10`) rather than alphabetical
        # (`Insert1, Insert10, Insert2, …`) which is what a raw directory
        # listing produces.
        meta_path = novel_dir / ".epub_meta.json"
        ordered_paths: list[Path] = []
        if images_dir is None and meta_path.exists():
            import json as _json

            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            for rel in meta.get("images", []) or []:
                p = novel_dir / rel
                if p.exists():
                    ordered_paths.append(p)
            if ordered_paths:
                console.print(
                    f"[dim]Using spine-ordered image list from {meta_path.name} "
                    f"({len(ordered_paths)} image(s))[/]"
                )
        extra_images = ordered_paths or s5_mix.collect_images(resolved_images_dir)
        if extra_images:
            # If we have a `cover.*` in the images dir AND no --cover override,
            # promote it to the cover slot so it's the primary attached_pic.
            if cover_image is None:
                covers = [p for p in extra_images if p.stem.lower() in ("cover", "front")]
                if covers:
                    cover_image = covers[0]
                    extra_images = [p for p in extra_images if p != cover_image]
                    console.print(f"[dim]Auto-promoted cover: {cover_image}[/]")
            console.print(
                f"[dim]Found {len(extra_images)} additional image(s) in {resolved_images_dir}[/]"
            )

    output_m4b = s5_mix.mix(
        chapters_audio=chapters_audio,
        chapter_titles=titles,
        book_title=book_title,
        beats_root=out_dir,
        output_dir=final_dir,
        intra_silence=intra,
        inter_scene_silence=inter_scene,
        inter_chapter_silence=inter_chapter,
        lead_in_silence=lead_in,
        target_lufs=lufs,
        aac_kbps=kbps,
        cover_image=cover_image,
        images=extra_images,
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )
    console.print(f"[green]✓[/] {output_m4b}")


@app.command(name="s6")
def stage6(
    book_id: str,
    epub: Optional[Path] = typer.Option(
        None,
        "--epub",
        help=(
            "Path to the source EPUB. Defaults to epubs/<book_id>.epub "
            "(e.g. epubs/level99/volume-01.epub)."
        ),
    ),
    novels_root: Path = typer.Option(
        Path("novels"),
        "--novels-root",
        help="Where the novels/<book_id>/.epub_meta.json lives.",
    ),
    intra: float = typer.Option(0.25, help="Intra-scene silence (must match Stage 5)."),
    inter_scene: float = typer.Option(1.0, help="Inter-scene silence (must match Stage 5)."),
    inter_chapter: float = typer.Option(2.0, help="Inter-chapter silence (must match Stage 5)."),
):
    """Stage 6: Wrap original EPUB XHTML with beat spans + sync_manifest.json.

    Lets a custom audiobook player highlight the active beat by querying
    `[data-beat-id="…"]` in the EPUB and looking up start_seconds/end_seconds
    in `sync_manifest.json`. The silence flags MUST match what was used at
    Stage 5 — otherwise the in-m4b timings drift.
    """
    out_dir = _book_dir(book_id)
    directed_dir = out_dir / "03_directed"
    audio_dir = out_dir / "05_audio"
    if not directed_dir.exists():
        console.print(f"[red]Missing {directed_dir}. Run `lnvox s3` first.[/]")
        raise typer.Exit(1)
    if not audio_dir.exists():
        console.print(f"[red]Missing {audio_dir}. Run `lnvox s4` first.[/]")
        raise typer.Exit(1)

    epub_path = epub or Path("epubs") / f"{book_id}.epub"
    if not epub_path.exists():
        console.print(f"[red]EPUB not found at {epub_path}. Pass --epub <path>.[/]")
        raise typer.Exit(1)

    novel_dir = novels_root / book_id
    sync_dir = out_dir / "07_sync"

    console.print(
        f"Syncing [bold]{book_id}[/]:\n"
        f"  epub      = {epub_path}\n"
        f"  novel_dir = {novel_dir}\n"
        f"  audio_dir = {audio_dir}\n"
        f"  output    = {sync_dir}"
    )

    manifest = s6_sync.run(
        book_id=book_id,
        book_dir=out_dir,
        epub_path=epub_path,
        novel_dir=novel_dir,
        output_dir=sync_dir,
        intra_silence=intra,
        inter_scene_silence=inter_scene,
        inter_chapter_silence=inter_chapter,
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )

    pct = 100.0 * manifest["matched"] / max(1, manifest["total_beats"])
    style = "green" if manifest["unmatched"] == 0 else "yellow"
    console.print(
        f"[{style}]Matched {manifest['matched']}/{manifest['total_beats']} "
        f"beats ({pct:.1f}%).[/]"
    )
    if manifest["unmatched"]:
        console.print(
            f"[yellow]See {sync_dir / 'unmatched.json'} for the {manifest['unmatched']} "
            f"unmatched beats.[/]"
        )


audio_app = typer.Typer(help="Rendered-audio management (purge, inspect)", no_args_is_help=True)
app.add_typer(audio_app, name="audio")


@audio_app.command(name="purge")
def audio_purge(
    book_id: str,
    speaker: str = typer.Option(
        ...,
        "--speaker",
        help="Speaker name to purge (e.g. 'Narrator', 'Kamijou Touma').",
    ),
    cache_dir: Path = typer.Option(
        Path("cache/tts"),
        "--cache-dir",
        help="TTS cache directory to also clean.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
):
    """Delete rendered WAVs + content-hash cache entries for one speaker.

    Useful after switching a character's voice clip — the next s4 run will
    re-render the affected beats with the new reference clip.
    """
    from lnvox.tts.schema import ChapterAudio

    out_dir = _book_dir(book_id)
    audio_root = out_dir / "05_audio"
    if not audio_root.exists():
        console.print(f"[red]Missing {audio_root}. Nothing to purge.[/]")
        raise typer.Exit(1)

    targets: list[tuple[Path, Path | None]] = []
    for chap_dir in sorted(audio_root.iterdir()):
        manifest = chap_dir / "manifest.json"
        if not manifest.exists():
            continue
        ca = ChapterAudio.model_validate_json(manifest.read_text(encoding="utf-8"))
        for beat in ca.beats:
            if beat.speaker != speaker:
                continue
            wav = chap_dir / f"{beat.beat_id}.wav"
            cache_wav = (
                cache_dir / f"{beat.cache_key}.wav" if beat.cache_key else None
            )
            targets.append((wav, cache_wav))

    if not targets:
        console.print(
            f"[yellow]No beats found for speaker '{speaker}' in {audio_root}.[/]"
        )
        return

    console.print(
        f"About to delete:\n"
        f"  - {len(targets)} rendered WAV(s) under {audio_root}\n"
        f"  - {sum(1 for _, c in targets if c)} cache entries under {cache_dir}"
    )
    if not yes:
        confirm = typer.confirm("Proceed?", default=False)
        if not confirm:
            console.print("[yellow]Aborted.[/]")
            raise typer.Exit(1)

    rendered_deleted = 0
    cache_deleted = 0
    for wav, cache_wav in targets:
        if wav.exists():
            wav.unlink()
            rendered_deleted += 1
        if cache_wav and cache_wav.exists():
            cache_wav.unlink()
            cache_deleted += 1

    console.print(
        f"[green]✓[/] Deleted {rendered_deleted} rendered WAV(s) and "
        f"{cache_deleted} cache entry(ies) for '{speaker}'."
    )
    console.print(
        "[dim]Re-run `lnvox s4` (or the pipeline launcher) to re-render with the current voice assignment.[/]"
    )


voice_app = typer.Typer(help="Voicebank management & casting", no_args_is_help=True)
app.add_typer(voice_app, name="voice")


@voice_app.command(name="seed-cv")
def voice_seed_cv(
    cv_root: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Path to extracted Common Voice locale dir (contains clips/ and .tsv files)",
    ),
    max_speakers: int = typer.Option(150, help="Maximum speakers to keep"),
    tsv: str = typer.Option(
        "validated.tsv",
        help="Preferred TSV inside cv_root (falls back to train/dev/test if missing)",
    ),
    target_seconds: float = typer.Option(12.0, help="Target ref clip length"),
    min_seconds: float = typer.Option(8.0, help="Drop speakers below this total duration"),
):
    """Populate the voicebank from a locally-extracted Common Voice tarball.

    Common Voice 23.0+ is only available via Mozilla Data Collective; download
    the tarball there, extract it, and pass the locale directory (the one that
    contains clips/ and validated.tsv) as CV_ROOT.
    """
    from lnvox.voices.common_voice import seed_from_common_voice

    vb_dir = _voicebank_dir()
    existing = voice_manifest.load(vb_dir)
    console.print(
        f"[dim]Existing voicebank: {len(existing.clips)} clip(s) in {vb_dir}/[/]"
    )

    try:
        new_vb = seed_from_common_voice(
            vb_dir,
            cv_root=cv_root,
            tsv_name=tsv,
            max_speakers=max_speakers,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except (RuntimeError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

    # Merge with existing (keep existing clips not from this run).
    existing_ids = {c.id for c in new_vb.clips}
    kept = [c for c in existing.clips if c.id not in existing_ids]
    merged = type(new_vb)(clips=new_vb.clips + kept)
    voice_manifest.save(vb_dir, merged)

    console.print(
        f"[green]✓[/] Voicebank now has {len(merged.clips)} clip(s) "
        f"({len(new_vb.clips)} from this run)"
    )


@voice_app.command(name="list")
def voice_list():
    """Summarise the current voicebank."""
    vb_dir = _voicebank_dir()
    vb = voice_manifest.load(vb_dir)
    summary = voice_manifest.summarize(vb)
    console.print(f"Voicebank: {vb_dir}/  ({summary['total']} clip(s))")
    if not vb.clips:
        console.print("[yellow]Empty. Seed it via `lnvox voice seed-cv`.[/]")
        return

    src_table = Table(title="By source")
    src_table.add_column("source")
    src_table.add_column("clips", justify="right")
    for src, n in sorted(summary["by_source"].items()):
        src_table.add_row(src, str(n))
    console.print(src_table)

    demo_table = Table(title="By gender × age_band")
    demo_table.add_column("bucket")
    demo_table.add_column("clips", justify="right")
    for bucket, n in sorted(summary["by_gender_age"].items()):
        demo_table.add_row(bucket, str(n))
    console.print(demo_table)


@voice_app.command(name="cast")
def voice_cast(
    book_id: str,
    top_n: int = typer.Option(3, help="Number of candidates to rank per character"),
    narrator_clip: Optional[str] = typer.Option(
        None,
        "--narrator-clip",
        help="Voicebank clip id to use for the Narrator (overrides auto-cast + any prior-volume reuse).",
    ),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="Ignore this volume's existing 04_voice_assignments.json (re-cast "
        "characters with no prior-volume match instead of keeping their clip).",
    ),
):
    """LLM-match each character to a voicebank clip; write 04_voice_assignments.json.

    Auto-detects ALL prior volumes in the same series. Characters matching a
    prior volume's assignment — by canonical name or alias, newest volume wins
    — keep that clip (no LLM call), so voices stay stable even when s1 renames
    a character ("Kamijou Touma" → "Kamijou") or the character skips a volume.
    The Narrator is also reused unless --narrator-clip is provided.
    """
    vb_dir = _voicebank_dir()
    vb = voice_manifest.load(vb_dir)
    if not vb.clips:
        console.print(
            "[red]Voicebank is empty. Run `lnvox voice seed-cv` first.[/]"
        )
        raise typer.Exit(1)

    out_dir = _book_dir(book_id)
    cast_file = out_dir / "01_characters.json"
    profiles_file = out_dir / "03_voice_profiles.json"
    if not cast_file.exists():
        console.print(f"[red]Missing cast at {cast_file}. Run `lnvox s1`.[/]")
        raise typer.Exit(1)

    cast = CharacterList.model_validate_json(cast_file.read_text(encoding="utf-8"))
    profiles = (
        VoiceProfileList.model_validate_json(profiles_file.read_text(encoding="utf-8"))
        if profiles_file.exists()
        else None
    )

    # Cross-volume reuse: fold EVERY prior volume's assignments into an
    # identity index (names + aliases, newest volume wins). A single-volume
    # lookback loses characters that skip a volume; exact-name matching loses
    # them on s1 canonical-name drift — both silently recast established
    # voices mid-series.
    artifacts_dir = Settings().artifacts_dir
    prior_index = None
    prior_vols: list[str] = []

    # Weakest layer: this volume's own existing assignments (if any). Prior
    # volumes fold in AFTER and therefore override it, so continuity chains
    # win — but a first-appearance character keeps its already-cast clip
    # instead of being re-rolled by the LLM. Makes re-casting idempotent and
    # keeps already-generated audio stable. `--fresh` opts out.
    own_assign = out_dir / "04_voice_assignments.json"
    if not fresh and own_assign.exists():
        prior_index = voice_matcher.PriorCastIndex()
        prior_index.add_volume(
            BookCasting.model_validate_json(own_assign.read_text(encoding="utf-8")),
            cast.characters,
        )
        prior_vols.append(f"{book_id.rsplit('/', 1)[-1]} (existing)")

    for prior_dir in find_prior_volumes(artifacts_dir, book_id):
        prior_assign = prior_dir / "04_voice_assignments.json"
        if not prior_assign.exists():
            continue
        prior_casting = BookCasting.model_validate_json(
            prior_assign.read_text(encoding="utf-8")
        )
        prior_chars_file = prior_dir / "01_characters.json"
        prior_chars = (
            CharacterList.model_validate_json(
                prior_chars_file.read_text(encoding="utf-8")
            ).characters
            if prior_chars_file.exists()
            else []
        )
        if prior_index is None:
            prior_index = voice_matcher.PriorCastIndex()
        prior_index.add_volume(prior_casting, prior_chars)
        prior_vols.append(prior_dir.name)
    if prior_index is not None:
        console.print(
            f"[dim]Reusing assignments from prior volume(s) "
            f"{', '.join(prior_vols)} ({len(prior_index)} identity keys)[/]"
        )

    client = LLMClient()
    console.print(
        f"[dim]endpoint={client.settings.llm.endpoint} model={client.settings.llm.model}[/]"
    )
    if narrator_clip:
        console.print(f"[dim]Narrator override: {narrator_clip}[/]")
    console.print(
        f"Casting {len(cast.characters)} character(s) against {len(vb.clips)} clip(s)…"
    )

    def _progress(character, casting):
        if not casting.assigned_clip_id:
            console.print(
                f"  [yellow]·[/] {character.name}: no match "
                f"({casting.candidates_considered} candidate(s))"
            )
            return
        # Highlight reused vs freshly cast.
        marker = "[green]✓[/]"
        suffix = ""
        if prior_index is not None:
            prior = prior_index.lookup(
                character.name, character.aliases, character.gender
            )
            if prior and prior.assigned_clip_id == casting.assigned_clip_id:
                suffix = " [dim](reused from prior volume)[/]"
        console.print(
            f"  {marker} {character.name} → {casting.assigned_clip_id} "
            f"({casting.candidates_considered} cand.){suffix}"
        )

    result = voice_matcher.cast_book(
        client,
        book_id,
        cast.characters,
        vb,
        profiles=profiles,
        top_n=top_n,
        prior_index=prior_index,
        narrator_clip_override=narrator_clip,
        on_character_done=_progress,
    )

    out_path = out_dir / "04_voice_assignments.json"
    out_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    table = Table(title=f"{out_path}")
    table.add_column("character")
    table.add_column("gender")
    table.add_column("age")
    table.add_column("assigned clip")
    table.add_column("candidates", justify="right")
    name_to_char = {c.name: c for c in cast.characters}
    for cst in result.castings:
        ch = name_to_char.get(cst.character_name)
        table.add_row(
            cst.character_name,
            cst.target.gender if cst.assigned_clip_id else "—",
            cst.target.age_band if cst.assigned_clip_id else "—",
            cst.assigned_clip_id or "[red]none[/]",
            str(cst.candidates_considered),
        )
    console.print(table)


if __name__ == "__main__":
    app()

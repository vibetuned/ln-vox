import sys
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
from lnvox.voices import audit as audit_mod
from lnvox.voices import manifest as voice_manifest
from lnvox.voices import matcher as voice_matcher
from lnvox.voices.schema import BookCasting

# Windows: a piped stdout/stderr (`| tee`, the launcher's logs, ComfyUI's log
# pipe) defaults to the ANSI code page, and the arrows / check marks in the
# progress output crash it with UnicodeEncodeError. A console is unaffected.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")


app = typer.Typer(help="ln-vox novel-to-audiobook pipeline", no_args_is_help=True)
console = Console()


def _book_dir(book_id: str) -> Path:
    return Settings().artifacts_dir / book_id


def _default_book_id(folder: Path) -> str:
    """The book id a novels folder stands for.

    Book ids are hierarchical (`<series>/volume-NN`, which cross-volume
    continuity keys on), and the folder's last name alone loses the series:
    novels/series/volume-01 would land in artifacts/volume-01.
    """
    try:
        rel = folder.resolve().relative_to(Path("novels").resolve())
    except ValueError:
        rel = None
    return rel.as_posix() if rel and rel.parts else folder.name


def _voicebank_dir() -> Path:
    # DESIGN.md §17.5: a per-language bank (e.g. voicebank-fr, seeded from the
    # French Common Voice corpus) is selected once via env, not per-command
    # flags. Default stays the historical English bank. Read through
    # Settings (`LNVOX_VOICEBANK`) so a `.env` entry works too.
    return Settings().voicebank


def _filter_chapters(chapters, selected: Optional[str]):
    if not selected:
        return chapters
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    return [c for c in chapters if c.chapter_id in wanted]


@app.command()
def ingest(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    book_id: Optional[str] = typer.Option(
        None,
        help=(
            "Defaults to the folder's path under ./novels (novels/series/"
            "volume-01 → series/volume-01), else the folder name."
        ),
    ),
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

    if not book_id:
        book_id = _default_book_id(folder)
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
    split_chapters: bool = typer.Option(
        False,
        "--split-chapters",
        help=(
            "Cut a spine page into several chapters at its chapter markers "
            "(headings, or Gutenberg `id=\"Chapter_XI\"` anchors). For EPUBs "
            "that pack a whole book into a few XHTML files. DESIGN.md §18."
        ),
    ),
):
    """Stage 0a: Extract an EPUB into the novels/-folder layout.

    Writes `NN-slug.txt` per narrative chapter, dumps every illustration to
    `images/`, and stores `.epub_meta.json` so the cover can later be embedded
    in the final m4b. Re-running into the same folder first removes the
    chapter files the previous extraction listed in `.epub_meta.json`.

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
        split_chapters=split_chapters,
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


@app.command(name="ingest-pdf")
def ingest_pdf(
    pdf_path: Path = typer.Argument(..., exists=True, dir_okay=False, file_okay=True),
    output_dir: Path = typer.Argument(..., help="Destination folder (e.g. novels/my-book/volume-01)"),
    chapter: Optional[list[str]] = typer.Option(
        None,
        "--chapter",
        help=(
            "One chapter as FIRST-LAST:Title (1-based, inclusive pages; the "
            "title is optional). Repeat per chapter. Default: the PDF's "
            "bookmarks, else the whole document as one chapter."
        ),
    ),
    pages: Optional[str] = typer.Option(
        None,
        "--pages",
        help="Without --chapter: read only pages FIRST-LAST (skip front/back matter).",
    ),
    title: Optional[str] = typer.Option(
        None, "--title", help="Book title for the EPUB (default: the PDF's metadata, else its file name)."
    ),
    author: Optional[list[str]] = typer.Option(
        None, "--author", help="Author for the EPUB; repeat for several (default: the PDF's metadata)."
    ),
    language: str = typer.Option("en", "--language", help="The EPUB's language code."),
    cover_page: int = typer.Option(
        1,
        "--cover-page",
        help="Page whose image is the cover (embedded in the m4b and the EPUB); 0 = no cover.",
    ),
    cover: Optional[Path] = typer.Option(
        None,
        "--cover",
        help=(
            "Use this image as the cover instead of a page's (overrides "
            "--cover-page) — e.g. the folder's current images/cover.jpg to keep it "
            "when re-extracting."
        ),
    ),
    epub: Optional[Path] = typer.Option(
        None,
        "--epub",
        help=(
            "Where to write the EPUB built from the chapters — the sync target "
            "for Stage 6. Default: epubs/<book_id>.epub, where the launcher "
            "looks for it. An EPUB not generated by ln-vox is never overwritten."
        ),
    ),
    no_epub: bool = typer.Option(False, "--no-epub", help="Don't write the EPUB."),
    font_filter: bool = typer.Option(
        False,
        "--font-filter",
        help=(
            "For a born-digital PDF (not a scan): keep only the running text, "
            "told apart by its font; section headings become their own paragraphs. "
            "Figures and tables are rendered into the EPUB before their first "
            "mention, and endnotes tied to their markers for the reader app "
            "(rendering needs `--with pypdfium2`)."
        ),
    ),
    notes_pages: Optional[str] = typer.Option(
        None,
        "--notes",
        help="With --font-filter: the endnotes section's pages FIRST-LAST (default: the 'Notes' bookmark).",
    ),
    no_notes: bool = typer.Option(False, "--no-notes", help="With --font-filter: leave the endnotes out."),
    no_figures: bool = typer.Option(
        False, "--no-figures", help="With --font-filter: drop figures and tables without rendering them."
    ),
    drop_pattern: Optional[list[str]] = typer.Option(
        None,
        "--drop-pattern",
        help=(
            "A regular expression removed from every paragraph — book-specific furniture such as "
            "margin page references (e.g. 'p[.] [A-Z][0-9]+'). Repeat for several."
        ),
    ),
    rewrite: Optional[list[str]] = typer.Option(
        None,
        "--rewrite",
        help=(
            "REGEX=>TEXT: replace every match in every paragraph (re.sub syntax; a blank "
            "line in TEXT starts a new paragraph). Repeat for several; applied after --drop-pattern."
        ),
    ),
):
    """Stage 0a: Extract a PDF into the novels/-folder layout, plus an EPUB.

    Writes `NN-slug.txt` per chapter — running heads, page numbers and line
    hyphenation removed, lines re-joined into paragraphs — the cover page's
    image as `images/cover.jpg`, and `.epub_meta.json` (the same sidecar
    `ingest-epub` writes). The generated EPUB carries exactly the chapters'
    text, so Stage 6 can sync it with the m4b. A scan needs an OCR text layer
    first. Needs pypdf and Pillow, which aren't locked dependencies:
    `uv run --with "pypdf[image]" lnvox ingest-pdf …`.

    Run `lnvox ingest <output_dir>` next (`--mode lecture` for non-fiction).
    """
    from lnvox.ingest.pdf import extract_pdf, parse_chapter_spec, parse_page_range

    try:
        specs = [parse_chapter_spec(c) for c in chapter or []]
        page_range = parse_page_range(pages) if pages else None
        notes_range = parse_page_range(notes_pages) if notes_pages else None
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)
    if (notes_range or no_notes or no_figures) and not font_filter:
        console.print("[red]--notes, --no-notes and --no-figures only apply with --font-filter.[/]")
        raise typer.Exit(2)
    if notes_range and no_notes:
        console.print("[red]--notes and --no-notes contradict each other.[/]")
        raise typer.Exit(2)
    if specs and page_range:
        console.print("[red]--pages only applies without --chapter (the chapters name their pages).[/]")
        raise typer.Exit(2)
    import re

    for pattern in drop_pattern or []:
        try:
            re.compile(pattern)
        except re.error as e:
            console.print(f"[red]--drop-pattern {pattern!r}: {e}[/]")
            raise typer.Exit(2)
    rewrites = []
    for spec in rewrite or []:
        pattern, sep, replacement = spec.partition("=>")
        try:
            if not sep:
                raise re.error("expected REGEX=>TEXT")
            re.compile(pattern)
        except re.error as e:
            console.print(f"[red]--rewrite {spec!r}: {e}[/]")
            raise typer.Exit(2)
        rewrites.append((pattern, replacement))
    if cover_page < 0:
        console.print("[red]--cover-page must be a page number, or 0 for no cover.[/]")
        raise typer.Exit(2)
    if no_epub and epub:
        console.print("[red]--epub and --no-epub contradict each other.[/]")
        raise typer.Exit(2)
    epub_path = None if no_epub else (epub or Path("epubs") / f"{_default_book_id(output_dir)}.epub")

    console.print(f"Extracting [bold]{pdf_path}[/] → [bold]{output_dir}[/]…")
    try:
        meta = extract_pdf(
            pdf_path,
            output_dir,
            chapters=specs or None,
            pages=page_range,
            title=title,
            authors=author or None,
            language=language,
            cover_page=cover_page or None,
            cover_file=cover,
            epub_path=epub_path,
            font_filter=font_filter,
            notes_pages=notes_range,
            notes=not no_notes,
            figures=not no_figures,
            drop_patterns=drop_pattern or (),
            rewrites=rewrites,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except ImportError:
        console.print(
            "[red]PDF ingest needs pypdf. Run it as "
            '`uv run --with "pypdf[image]" lnvox ingest-pdf …` (or `uv pip install "pypdf[image]"`).[/]'
        )
        raise typer.Exit(2)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)

    table = Table(title=f"{meta.title or pdf_path.name}")
    table.add_column("file")
    table.add_column("pages", justify="right")
    table.add_column("title", overflow="fold")
    for ch in meta.chapters:
        table.add_row(ch["file"], "{}-{}".format(*ch["pages"]), ch["title"])
    console.print(table)
    if meta.cover_image:
        console.print(f"[dim]Cover: {(output_dir / meta.cover_image).as_posix()}[/]")
    if meta.epub:
        console.print(f"[dim]EPUB:  {meta.epub.as_posix()} (Stage 6's default sync target)[/]")
    console.print(f"[dim]Next: uv run lnvox ingest {output_dir.as_posix()}[/]")


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

    # Auto-detect prior volumes in the same series. s1 only loads their cast
    # lists (which forces the cluster/merge path even for a one-chapter
    # book); merge_clusters deliberately ignores them — sending prior casts
    # to the merge LLM blew its context. Cross-volume continuity (recurring
    # characters keeping their voice) happens at `voice cast`, which reads
    # the priors' 04_voice_assignments.json.
    artifacts_dir = Settings().artifacts_dir
    priors = find_prior_volumes(artifacts_dir, book_id)
    if priors:
        console.print(
            f"[dim]Found {len(priors)} prior volume(s): {[p.name for p in priors]} "
            f"— their voices are reused at `voice cast`, not merged here[/]"
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
        help=(
            "Force-regenerate EVERY voice profile. Not needed after a recast: "
            "profiles of recast characters regenerate on their own. Chapters "
            "whose descriptors change are re-voiced (and so re-rendered by s4)."
        ),
    ),
):
    """Stage 3: Generate Dramabox-ready stage directions per beat.

    Run AFTER `lnvox voice cast` — the director consults the assigned voice
    clip's metadata to write descriptors matching the actual reference voice.
    Casting-aware on re-runs: a character's cached profile is reused only
    while its clip (and any locked descriptor) is unchanged, and a cached
    chapter whose speakers' descriptors changed has its prompts re-voiced.
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
        console.print("[dim]--regen-profiles: regenerating every voice profile.[/]")

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
        regen_profiles=regen_profiles,
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

    # The book's language (ingest's 00_book_meta.json) keeps the normalize
    # pass from respelling a French book's numbers in English.
    import json as _json

    meta_file = out_dir / "00_book_meta.json"
    language = "en"
    if meta_file.exists():
        language = _json.loads(meta_file.read_text(encoding="utf-8")).get("language") or "en"

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
        language=language,
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
            "Re-roll the structuring: ignore the cache/scenario/ LLM cache "
            "(content-keyed on system + rendered prompt + model) for this run, "
            "and overwrite its entries with the fresh results."
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
        # --no-cache still WRITES the cache: otherwise the next cached run
        # would hand back the very structuring the re-roll replaced.
        cache_dir=Path("cache") / "scenario",
        refresh_cache=no_cache,
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )
    n_items = sum(len(s.items) for s in script.scenes)
    console.print(
        f"[green]Done.[/] {len(script.scenes)} scene(s), {n_items} item(s) → "
        f"{out_dir / '00_script.json'}\n"
        f"Next: `lnvox voice cast {id}` then `lnvox scenario {id}`."
    )


@app.command(name="scenario")
def stage_scenario(
    book_id: str,
    regen_profiles: bool = typer.Option(
        False,
        "--regen-profiles",
        help=(
            "Force-regenerate EVERY voice profile (recast characters regenerate "
            "on their own). Scenes whose descriptors change are re-voiced."
        ),
    ),
):
    """Scenario mode direction pass (DESIGN.md §17.3).

    Adds per-line emotion (7-value enum) + a short cue in the script's
    language, composes the TTS prompts, and writes 03_directed/*.json so
    s4/s5 run unchanged (one chapter per scene; staging pauses become
    DirectedScene boundaries). Run AFTER `lnvox voice cast` so descriptors
    match the assigned clips. Group lines render with the Narrator voice.
    Casting-aware on re-runs, like s3: a recast regenerates that character's
    profile and re-voices the cached scenes it speaks in.
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
        regen_profiles=regen_profiles,
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
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help=(
            "Which mix to follow: the `lnvox s5 --title` of 06_final/<title>.m4b. "
            "Default: the newest mix in 06_final/."
        ),
    ),
    intra: Optional[float] = typer.Option(
        None,
        help="Pad between lines of one run (s5 --intra). Default: the mix's value, else 0.25.",
    ),
    staging_pause: Optional[float] = typer.Option(
        None,
        help="Pause where staging happens (s5 --inter-scene). Default: the mix's value, else 1.0.",
    ),
    inter_scene: Optional[float] = typer.Option(
        None,
        help=(
            "Pad between script scenes (s5 --inter-chapter). Default: the "
            "mix's value, else 0.75 — the play pacing; 2 s drags (§17.4)."
        ),
    ),
    lead_in: Optional[float] = typer.Option(
        None,
        "--lead-in",
        help=(
            "Per-beat lead-in silence (s5 --lead-in). Default: the mix's value, "
            "else 0.35, which covers the reader app's ~0.3 s poll (§17.4)."
        ),
    ),
):
    """Scenario mode sync emitter (DESIGN.md §17.4).

    Emits 07_sync/<scene>.json + play.json + play.srt: one timed entry per
    script item (dialogue spans, staging pauses, zero-duration cues),
    computed from the same plan as the s5 mix so both agree. The pads and
    scene list are read from the mix's 06_final/<title>.timings.json; the
    flags only override them. Requires per-beat rendering (dramabox backend).
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
            mix_title=title,
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


def _keep_awake() -> None:
    """Windows counterpart of the launcher's macOS `caffeinate`: s4 is hours
    of GPU work, which doesn't count as activity, so idle sleep would suspend
    the render. Holds until this process exits."""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


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
    if backend == "dramabox":
        from lnvox.tts.dramabox_client import (
            s4_chunk_cap,
            s4_decode_dtype,
            s4_encoder_precision,
        )

        # Validate the render-quality knobs before any work: a bad value is
        # a deterministic refusal (exit 2 — s4_retry.sh doesn't replay it),
        # not a traceback halfway through planning.
        try:
            s4_encoder_precision()
            chunk_cap = s4_chunk_cap()
            decode_dtype = s4_decode_dtype()
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(2)
        # Chunk cap and decode dtype are staged-planner policies; the
        # monolithic path delegates chunking/decoding to DramaBox's TTSServer
        # internals and keeps the old v1 recipe as ITS default (the staged-
        # path defaults don't apply here). Only an EXPLICIT request for those
        # knobs is refused — rendering different audio under the requested
        # cache key would poison any A/B against the staged path. An empty
        # value is unset (the helpers read it as the default), not a request.
        explicit = [
            name
            for name, val in (
                ("LNVOX_S4_CHUNK_CAP", chunk_cap),
                ("LNVOX_S4_DECODE_DTYPE", decode_dtype),
            )
            if os.environ.get(name, "").strip() and val is not None
        ]
        if explicit and not staged:
            console.print(
                f"[red]{' / '.join(explicit)} need the staged path — re-run "
                "with --staged (LNVOX_S4_ENCODER_PRECISION works on both).[/]"
            )
            raise typer.Exit(2)
    if no_trim:
        # Read by tts.trim / VibeVoiceClient; inherited by the staged-phase
        # subprocesses, so one env var covers all three render paths.
        os.environ["LNVOX_S4_NO_TRIM"] = "1"
    _keep_awake()
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

    from lnvox.tts.dramabox_client import DramaboxClient, s4_variant_tag

    # Monolithic Dramabox on MPS renders SILENCE with exit 0: TTSServer is
    # single-dtype and no single dtype works there (fp16 Gemma encodings are
    # all-NaN — MAC_TEST_REPORT.md F10; the vocoder needs fp32 — F9). Only
    # the staged path applies the per-component dtype map. Refuse before any
    # placement or model load; exit 2 so s4_retry.sh doesn't replay it.
    if device.startswith("mps"):
        console.print(
            "[red]Monolithic Dramabox can't render on MPS (no single dtype "
            "works there — MAC_TEST_REPORT.md F9/F10; it would produce silent "
            "audio). Re-run with --staged — the launcher default; "
            "LNVOX_S4_STAGED=0 is what selects this path.[/]"
        )
        raise typer.Exit(2)

    s4_tts.run(
        chapters_loaded,
        casting,
        voicebank,
        vb_dir,
        audio_dir,
        cache_dir,
        client_factory=_factory,
        model_version=DramaboxClient.MODEL_VERSION + s4_variant_tag(device, staged=False),
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


@app.command(name="remote-tts")
def remote_tts(
    book_id: str,
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help=(
            "SSH host of the render box: an ~/.ssh/config alias or user@host, with key "
            "login. Remembered per book; default $LNVOX_REMOTE_HOST."
        ),
    ),
    remote_dir: Optional[str] = typer.Option(
        None,
        "--remote-dir",
        help="The ln-vox checkout on the box, relative to its home or absolute (default: ln-vox; cloned when missing).",
    ),
    branch: Optional[str] = typer.Option(
        None, "--branch", help="Branch the box checks out (default: this checkout's)."
    ),
    status: bool = typer.Option(False, "--status", help="Only show the render's state and progress."),
    pull: bool = typer.Option(False, "--pull", help="While rendering: fetch the chapters finished so far."),
    restart: bool = typer.Option(
        False, "--restart", help="Sync and launch again after a failure, a stop, or a finished render."
    ),
    stop: bool = typer.Option(False, "--stop", help="Stop the render on the box."),
    wait: bool = typer.Option(
        False, "--wait", help="Stay connected, reporting every --interval seconds; fetch the render when it's done."
    ),
    interval: int = typer.Option(300, "--interval", help="Seconds between --wait reports."),
    no_git: bool = typer.Option(False, "--no-git", help="Don't clone or update the box's checkout."),
    log_lines: int = typer.Option(12, "--log-lines", help="How much of the job's log to show."),
):
    """Render Stage 4 on a GPU box over SSH, e.g. a DGX Spark or a cloud instance.

    Run it once the LLM stages are done here (the launcher with --skip-tts),
    then as often as you like — this machine can shut down in between. Each
    run does what the book's state calls for: the first clones or updates
    the box's checkout, mirrors the book's 00-04 artifacts and the voicebank
    there, and starts Stage 4 detached; later runs show its progress, and
    once it's done fetch 05_audio so Stage 5 and 6 run here. Exit code: 0
    when the render is here, 3 while it renders, 1 when it failed or was
    stopped (--restart to launch again).
    """
    from lnvox.remote import Options, RemoteError
    from lnvox.remote import remote_tts as run_remote

    settings = Settings()
    try:
        code = run_remote(
            book_id,
            artifacts_dir=settings.artifacts_dir,
            voicebank=settings.voicebank,
            host=host,
            repo=remote_dir,
            branch=branch,
            options=Options(
                status=status, pull=pull, restart=restart, stop=stop, wait=wait,
                interval=interval, git=not no_git, log_lines=log_lines,
            ),
            say=lambda m: console.print(m, markup=False, highlight=False),
        )
    except RemoteError as e:
        console.print(f"[red]{e}[/]", highlight=False)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("[dim]Left it rendering; run the command again to check on it.[/]")
        raise typer.Exit(3)
    raise typer.Exit(code)


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
    keep_work: bool = typer.Option(
        False,
        "--keep-work",
        help=(
            "Keep 06_final/_work/ (per-chapter and full-book WAVs) after a "
            "successful mix, for debugging. It is always kept when the mix fails."
        ),
    ),
):
    """Stage 5: Mix rendered beats into a final .m4b with chapter markers.

    Also writes 06_final/<title>.timings.json, recording the pads, lead-in,
    chapters and pictures of this mix; `lnvox s6`, `lnvox scenario-sync` and
    `lnvox sample` follow it, so their flags need not repeat these.
    """
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
        f"Output: {final_dir}/{s5_mix.safe_filename(book_title)}.m4b"
    )

    # The embed order is recorded in the timings sidecar, which Stage 6 reads
    # to align manifest images with these pictures — see resolve_book_images.
    novel_dir = novels_root / book_id
    cover_image, extra_images = s5_mix.resolve_book_images(
        out_dir, novel_dir, cover=cover, images_dir=images_dir,
        log=console.print,
    )
    if cover_image is not None:
        console.print(f"[dim]Embedding cover: {cover_image}[/]")
    if extra_images:
        console.print(f"[dim]Found {len(extra_images)} additional image(s)[/]")
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
        audio_dir=audio_root.name,
        keep_work=keep_work,
        progress=lambda m: console.print(f"[dim]{m}[/]"),
    )
    console.print(f"[green]✓[/] {output_m4b}")


@app.command(name="sample")
def sample_cmd(
    book_id: str,
    out: Optional[Path] = typer.Option(
        None, "--out", help="Output dir (default: samples/<book>)."
    ),
    chapters: Optional[str] = typer.Option(
        None, "--chapters",
        help="Two consecutive chapter ids, e.g. '14,15'. Default: best-scoring pair.",
    ),
    minutes: float = typer.Option(12.0, help="Sample length, centred on the boundary."),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help=(
            "Title for the sample. If 06_final/<title>.m4b exists, that mix is "
            "sliced; otherwise the one sync_manifest.json was timed against."
        ),
    ),
    novels_root: Path = typer.Option(Path("novels"), "--novels-root"),
):
    """Cut a small, committable demo sample from a finished book.

    Straddles a chapter boundary so auto-follow, chapter-relative scrubbing,
    the chapter transition and inline images are all exercisable from a file
    small enough to put in a public repo. See DESIGN.md §22.
    """
    from lnvox.stages import sample as sample_mod

    book_dir = _book_dir(book_id)
    novel_dir = novels_root / book_id
    if not (novel_dir / ".epub_meta.json").exists():
        console.print(f"[red]No .epub_meta.json under {novel_dir}.[/]")
        raise typer.Exit(1)

    pair: Optional[tuple[str, str]] = None
    if chapters:
        parts = [c.strip() for c in chapters.split(",") if c.strip()]
        if len(parts) != 2:
            console.print("[red]--chapters needs exactly two ids, e.g. '14,15'.[/]")
            raise typer.Exit(2)
        pair = (parts[0], parts[1])

    out_dir = out or Path("samples") / book_id.replace("/", "_")
    book_title = title or book_id

    try:
        summary = sample_mod.build(
            book_dir, novel_dir, out_dir,
            book_title=book_title, chapters=pair, minutes=minutes,
            mix_title=title,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except (FileNotFoundError, ValueError, KeyError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

    size_mb = Path(summary["audio"]).stat().st_size / 1e6
    table = Table(title=f"Sample → {out_dir}/")
    table.add_column("field")
    table.add_column("value")
    table.add_row("chapters", ", ".join(summary["chapters"]))
    table.add_row("duration", f"{summary['seconds'] / 60:.1f} min")
    table.add_row("audio size", f"{size_mb:.1f} MB")
    table.add_row("beats", str(summary["beats"]))
    table.add_row("embedded pictures", f"{summary['pictures']} (cover + {max(0, summary['pictures'] - 1)})")
    table.add_row("visual elements", str(summary["visual_elements"]))
    console.print(table)


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
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help=(
            "Which mix to sync against: the `lnvox s5 --title` of "
            "06_final/<title>.m4b. Default: the newest mix in 06_final/."
        ),
    ),
    intra: Optional[float] = typer.Option(
        None, help="Override the intra-scene silence. Default: the mix's recorded value, else 0.25."
    ),
    inter_scene: Optional[float] = typer.Option(
        None, help="Override the inter-scene silence. Default: the mix's recorded value, else 1.0."
    ),
    inter_chapter: Optional[float] = typer.Option(
        None, help="Override the inter-chapter silence. Default: the mix's recorded value, else 2.0."
    ),
    lead_in: Optional[float] = typer.Option(
        None,
        "--lead-in",
        help="Override the per-beat lead-in. Default: the mix's recorded value, else 0.",
    ),
):
    """Stage 6: Wrap original EPUB XHTML with beat spans + sync_manifest.json.

    Lets a custom audiobook player highlight the active beat by querying
    `[data-beat-id="…"]` in the EPUB and looking up start_seconds/end_seconds
    in `sync_manifest.json`. Timings follow the Stage-5 mix recorded in
    06_final/<title>.timings.json — its pads, lead-in, chapter list and
    picture order — so no flag has to be repeated from the s5 run. The pad
    flags only override that record (for a book with no mix, they replace
    the s5 defaults).
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

    try:
        manifest = s6_sync.run(
            book_id=book_id,
            book_dir=out_dir,
            epub_path=epub_path,
            novel_dir=novel_dir,
            output_dir=sync_dir,
            intra_silence=intra,
            inter_scene_silence=inter_scene,
            inter_chapter_silence=inter_chapter,
            lead_in_silence=lead_in,
            mix_title=title,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

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
    v2: bool = typer.Option(
        False,
        "--v2",
        help=(
            "Purge VibeVoice sessions in 05_audio_v2/ instead of Dramabox beats "
            "in 05_audio/. A session holds several speakers' lines, so every "
            "session the speaker appears in is purged — and re-rendered whole."
        ),
    ),
):
    """Delete one speaker's rendered WAVs + content-hash cache entries, so the
    next s4 run renders them afresh.

    Switching a character to a different voice clip does NOT need this: the
    s4 cache key includes the reference clip's file name, so the new clip
    misses the cache and re-renders on its own. Purge when that key can't
    change — a clip file was replaced in place under the same name, or you
    want new takes of a speaker with the same clip and prompts.
    """
    from lnvox.tts.schema import ChapterAudio

    out_dir = _book_dir(book_id)
    audio_root = out_dir / ("05_audio_v2" if v2 else "05_audio")
    if not audio_root.exists():
        other = out_dir / ("05_audio" if v2 else "05_audio_v2")
        hint = f" ({other.name}/ exists — {'drop' if v2 else 'add'} --v2)" if other.exists() else ""
        console.print(f"[red]Missing {audio_root}. Nothing to purge.{hint}[/]")
        raise typer.Exit(1)

    def _speaks(entry_speaker: str) -> bool:
        # A VibeVoice session manifest names all its voices as "A + B + C"
        # (s4_vibevoice.render_chapter); a Dramabox beat names one.
        if v2:
            return speaker in (s.strip() for s in entry_speaker.split(" + "))
        return entry_speaker == speaker

    unit = "session" if v2 else "beat"
    targets: list[tuple[Path, Path | None]] = []
    for chap_dir in sorted(audio_root.iterdir()):
        manifest = chap_dir / "manifest.json"
        if not manifest.exists():
            continue
        ca = ChapterAudio.model_validate_json(manifest.read_text(encoding="utf-8"))
        for beat in ca.beats:
            if not _speaks(beat.speaker):
                continue
            wav = chap_dir / f"{beat.beat_id}.wav"
            cache_wav = (
                cache_dir / f"{beat.cache_key}.wav" if beat.cache_key else None
            )
            targets.append((wav, cache_wav))

    if not targets:
        console.print(
            f"[yellow]No {unit}s found for speaker '{speaker}' in {audio_root}.[/]"
        )
        return

    console.print(
        f"About to delete:\n"
        f"  - {len(targets)} rendered {unit} WAV(s) under {audio_root}\n"
        f"  - {sum(1 for _, c in targets if c)} cache entries under {cache_dir}"
    )
    if v2:
        console.print(
            "[dim]Each session also holds other speakers' lines; they are "
            "re-rendered with it.[/]"
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
    rerun = "lnvox s4 --tts-backend vibevoice" if v2 else "lnvox s4"
    console.print(
        f"[dim]Re-run `{rerun}` (or the pipeline launcher) to re-render, then "
        f"s5 and the sync stage — the durations change.[/]"
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
    accent: Optional[list[str]] = typer.Option(
        None,
        "--accent",
        help=(
            "Keep only speakers with this accent code; repeat for several "
            "(e.g. --accent us --accent england). Default: every accent."
        ),
    ),
    rank_by_quality: bool = typer.Option(
        False,
        "--rank-by-quality",
        help=(
            "Select speakers by measured recording quality (bandwidth, SNR, "
            "hum, hiss) instead of client_id order, with per-gender/age quotas. "
            "Adds a probe pass. DESIGN.md §19."
        ),
    ),
    max_candidates: Optional[int] = typer.Option(
        None,
        "--max-candidates",
        help="--rank-by-quality: speakers to probe (default: max-speakers x 6).",
    ),
    only_new: bool = typer.Option(
        False,
        "--only-new",
        help=(
            "Grow the bank: skip speakers it already has (their WAVs are never "
            "rewritten) and count --max-speakers as NEW voices. DESIGN.md §19.6."
        ),
    ),
    fill_thin: bool = typer.Option(
        False,
        "--fill-thin",
        help=(
            "Implies --only-new. Give new voices to the bank's smallest "
            "gender/age buckets first, keeping only clips that clear "
            "9 kHz / 32 dB (probe AND built clip). DESIGN.md §19.6."
        ),
    ),
    probe_all: bool = typer.Option(
        False,
        "--probe-all",
        help=(
            "--fill-thin: probe every candidate, not just 5x each bucket's "
            "share. Much slower; finds every clean voice in the corpus."
        ),
    ),
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
        from collections import Counter as _Counter

        growing = only_new or fill_thin
        exclude = {c.id for c in existing.clips} if growing else None
        bucket_counts = (
            _Counter((c.gender, c.age_band) for c in existing.clips) if fill_thin else None
        )
        if growing:
            console.print(
                f"[dim]Growing the bank: adding up to {max_speakers} new voice(s); "
                f"the {len(existing.clips)} existing clip(s) are left untouched.[/]"
            )
        new_vb = seed_from_common_voice(
            vb_dir,
            cv_root=cv_root,
            tsv_name=tsv,
            max_speakers=max_speakers,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            accents=accent or None,
            rank_by_quality=rank_by_quality,
            max_candidates=max_candidates,
            exclude_ids=exclude,
            fill_thin_from=bucket_counts,
            probe_all=probe_all,
            progress=lambda m: console.print(f"[dim]{m}[/]"),
        )
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

    # Merge with existing (keep existing clips not from this run).
    existing_ids = {c.id for c in new_vb.clips}
    kept = [c for c in existing.clips if c.id not in existing_ids]
    # Growing appends: existing books were cast against this bank, and keeping
    # its order stable keeps any re-cast as close to the original as possible.
    clips = kept + new_vb.clips if (only_new or fill_thin) else new_vb.clips + kept
    merged = type(new_vb)(clips=clips)
    voice_manifest.save(vb_dir, merged)

    console.print(
        f"[green]✓[/] Voicebank now has {len(merged.clips)} clip(s) "
        f"({len(new_vb.clips)} from this run)"
    )


@voice_app.command(name="audit")
def voice_audit(
    book_id: str,
    fix: bool = typer.Option(
        False, "--fix", help="Reassign the lower-speaking side of each collision."
    ),
    min_semitones: float = typer.Option(
        audit_mod.MIN_SEMITONES,
        help="Flag scene-sharing pairs closer than this in pitch.",
    ),
    min_shared: int = typer.Option(
        audit_mod.MIN_SHARED,
        help="Ignore pairs with less shared-scene dialogue than this.",
    ),
):
    """Check that characters who share scenes have distinguishable voices.

    The matcher casts each character independently on demographics, so two
    people who talk to each other can land on near-identical clips. Run this
    after `voice cast` and before s3 — a casting change is free there, but
    costs a re-render once s4 has run. See DESIGN.md §20.
    """
    from lnvox.voices.acoustics import load_acoustics

    book_dir = _book_dir(book_id)
    casting_path = book_dir / "04_voice_assignments.json"
    if not casting_path.exists():
        console.print(f"[red]No casting at {casting_path} — run `lnvox voice cast` first.[/]")
        raise typer.Exit(1)

    scene_dir = book_dir / "03_directed"
    if not scene_dir.exists() or not any(scene_dir.glob("*.json")):
        scene_dir = book_dir / "02_scenes"
    if not scene_dir.exists() or not any(scene_dir.glob("*.json")):
        console.print("[red]No 02_scenes/ or 03_directed/ — run s2 first.[/]")
        raise typer.Exit(1)

    casting = BookCasting.model_validate_json(
        casting_path.read_text(encoding="utf-8")
    )
    vb_dir = _voicebank_dir()
    voicebank = voice_manifest.load(vb_dir)
    acoustics = load_acoustics(
        vb_dir, voicebank.clips, progress=lambda m: console.print(f"[dim]{m}[/]")
    )

    beats, pairs = audit_mod.scene_cooccurrence(scene_dir)
    collisions = audit_mod.find_collisions(
        casting, acoustics, pairs,
        min_semitones=min_semitones, min_shared=min_shared,
    )

    assigned = sum(1 for c in casting.castings if c.assigned_clip_id)
    console.print(
        f"[dim]{len(beats)} speaking character(s), {assigned}/{len(casting.castings)} "
        f"with a reference clip, co-occurrence from {scene_dir.name}/[/]"
    )
    if not collisions:
        console.print("[green]✓[/] No scene-sharing pair shares or nearly shares a voice.")
        return

    table = Table(title=f"Voice collisions ({len(collisions)})")
    table.add_column("kind")
    table.add_column("characters", overflow="fold")
    table.add_column("shared", justify="right")
    table.add_column("apart", justify="right")
    for c in collisions:
        table.add_row(
            c.kind,
            f"{c.a} ({beats.get(c.a, 0)}) + {c.b} ({beats.get(c.b, 0)})",
            str(c.shared),
            f"{c.semitones:.1f} st",
        )
    console.print(table)

    if not fix:
        console.print("[dim]Re-run with --fix to reassign the lower-speaking side.[/]")
        raise typer.Exit(1)

    applied, unfixed = audit_mod.repair_collisions(
        casting, voicebank, acoustics, beats, pairs, collisions,
        min_semitones=min_semitones, min_shared=min_shared,
    )
    if applied:
        casting_path.write_text(casting.model_dump_json(indent=2), encoding="utf-8")
        for r in applied:
            console.print(
                f"[green]✓[/] {r.character}: {r.old_clip_id} → {r.new_clip_id} "
                f"({r.new_pitch:.0f} Hz)"
            )
    for c in unfixed:
        console.print(
            f"[yellow]![/] {c.a} + {c.b}: no separated clip available "
            f"(locked, narrator, or bank exhausted) — recast by hand."
        )
    console.print(
        "[dim]Re-run s3 (or the launcher) so voice profiles match the new casting.[/]"
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
        help="Voicebank clip id to use for the Narrator (overrides auto-cast + "
        "any prior-volume reuse, but not a Narrator marked \"locked\": true in "
        "this volume's 04_voice_assignments.json). An id not in the voicebank "
        "exits 2 before anything is cast.",
    ),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="Re-cast characters with no prior-volume match instead of keeping "
        "their clip from this volume's existing 04_voice_assignments.json. "
        "Entries marked \"locked\": true are still kept.",
    ),
    seed: str = typer.Option(
        "",
        "--seed",
        envvar="LNVOX_CAST_SEED",
        help="Varies the order fresh casts see the voicebank in, for another draw "
        "of voices (default: one fixed per book and character, so re-casts repeat).",
    ),
):
    """LLM-match each character to a voicebank clip; write 04_voice_assignments.json.

    Auto-detects ALL prior volumes in the same series. Characters matching a
    prior volume's assignment — by canonical name or alias, newest volume wins
    — keep that clip (no LLM call), so voices stay stable even when s1 renames
    a character ("Kamijou Touma" → "Kamijou") or the character skips a volume.
    The Narrator is also reused unless --narrator-clip is provided.

    This volume's existing 04_voice_assignments.json is read first. An entry
    with "locked": true is a deliberate hand edit and is kept as-is — over
    prior volumes, --narrator-clip and --fresh. Unlocked entries keep their
    clip only for characters no prior volume knows. A kept entry keeps its
    audit_note and voice_descriptor too.
    """
    vb_dir = _voicebank_dir()
    vb = voice_manifest.load(vb_dir)
    if not vb.clips:
        console.print(
            "[red]Voicebank is empty. Run `lnvox voice seed-cv` first.[/]"
        )
        raise typer.Exit(1)

    # Check --narrator-clip NOW, not when the Narrator's turn comes: a
    # synthesised Narrator is cast last, so a typo used to surface only after
    # every other character's LLM calls, with nothing written. Exit 2 marks a
    # deterministic error that retrying cannot fix.
    clip_ids = {c.id for c in vb.clips}
    if narrator_clip and narrator_clip not in clip_ids:
        import difflib

        close = difflib.get_close_matches(narrator_clip, sorted(clip_ids), n=3)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        console.print(
            f"[red]--narrator-clip {narrator_clip!r} is not in the voicebank at "
            f"{vb_dir}/ ({len(vb.clips)} clip(s)).{hint} Check the id, or "
            f"LNVOX_VOICEBANK if the clip lives in another bank.[/]"
        )
        raise typer.Exit(2)

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

    # This volume's own existing assignments (if any), handed to the matcher
    # apart from prior volumes. A LOCKED entry is a deliberate hand edit
    # (ComfyUI Set Voice, or "locked": true in the JSON) and beats every
    # automatic rule, prior volumes and --narrator-clip included — which is
    # what makes the launcher's "lock it at --stop-after-audit, then resume"
    # advice hold. An unlocked entry is the weakest layer: continuity chains
    # win, but a first-appearance character keeps its already-cast clip
    # instead of being re-rolled by the LLM, so re-casting is idempotent and
    # already-generated audio stays stable. `--fresh` drops the unlocked
    # entries only: a lock means "never change this automatically", and
    # --fresh is an automatic re-roll. To discard a lock, unset it (or delete
    # the file).
    own_assign = out_dir / "04_voice_assignments.json"
    existing: BookCasting | None = None
    narrator_flag_ignored = False
    if own_assign.exists():
        existing = BookCasting.model_validate_json(
            own_assign.read_text(encoding="utf-8")
        )
        n_locked = sum(1 for c in existing.castings if c.locked)
        if fresh:
            existing.castings = [c for c in existing.castings if c.locked]
            console.print(
                f"[dim]--fresh: re-casting this volume; {n_locked} locked "
                f"entr{'y' if n_locked == 1 else 'ies'} kept[/]"
            )
        elif existing.castings:
            console.print(
                f"[dim]Existing casting: {len(existing.castings)} entries "
                f"({n_locked} locked). Locked entries are kept; the rest keep "
                f"their clip unless a prior volume knows the character.[/]"
            )
        locked_narrator = next(
            (
                c
                for c in existing.castings
                if c.character_name == "Narrator"
                and c.locked
                and c.assigned_clip_id in clip_ids
            ),
            None,
        )
        if (
            narrator_clip
            and locked_narrator is not None
            and locked_narrator.assigned_clip_id != narrator_clip
        ):
            narrator_flag_ignored = True
            console.print(
                f"[yellow]! The Narrator is locked to "
                f"{locked_narrator.assigned_clip_id} in {own_assign.name}; "
                f"--narrator-clip {narrator_clip} is ignored. Set "
                f"\"locked\": false on it to apply the flag.[/]"
            )

    # Cross-volume reuse: fold EVERY prior volume's assignments into an
    # identity index (names + aliases, newest volume wins). A single-volume
    # lookback loses characters that skip a volume; exact-name matching loses
    # them on s1 canonical-name drift — both silently recast established
    # voices mid-series.
    artifacts_dir = Settings().artifacts_dir
    prior_index = None
    prior_vols: list[str] = []
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
    if narrator_clip and not narrator_flag_ignored:
        console.print(f"[dim]Narrator override: {narrator_clip}[/]")
    console.print(
        f"Casting {len(cast.characters)} character(s) against {len(vb.clips)} clip(s)…"
    )

    own_clip = (
        {c.character_name: c.assigned_clip_id for c in existing.castings}
        if existing is not None
        else {}
    )

    def _progress(character, casting):
        if not casting.assigned_clip_id:
            console.print(
                f"  [yellow]·[/] {character.name}: no match "
                f"({casting.candidates_considered} candidate(s))"
            )
            return
        # Highlight locked / reused / kept vs freshly cast.
        marker = "[green]✓[/]"
        suffix = ""
        prior = (
            prior_index.lookup(character.name, character.aliases, character.gender)
            if prior_index is not None
            else None
        )
        if casting.locked:
            suffix = " [dim](locked, kept)[/]"
        elif prior and prior.assigned_clip_id == casting.assigned_clip_id:
            suffix = " [dim](reused from prior volume)[/]"
        elif own_clip.get(character.name) == casting.assigned_clip_id:
            suffix = " [dim](kept from existing casting)[/]"
        if casting.audit_note.startswith("voice cast:"):
            # Matcher fallback (ranking returned no offered id, or a locked
            # clip vanished from the bank) — worth a look before s3.
            marker = "[yellow]✓[/]"
            suffix += f" [yellow]{casting.audit_note}[/]"
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
        existing=existing,
        narrator_clip_override=narrator_clip,
        on_character_done=_progress,
        seed=seed,
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

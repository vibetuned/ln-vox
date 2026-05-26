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
    VoiceProfileList,
)
from lnvox.series import find_prior_volumes, latest_prior_volume
from lnvox.stages import s1_characters, s2_scenes, s3_director, s4_tts, s5_mix, s6_sync
from lnvox.voices import manifest as voice_manifest
from lnvox.voices import matcher as voice_matcher
from lnvox.voices.schema import BookCasting


app = typer.Typer(help="ln-vox novel-to-audiobook pipeline", no_args_is_help=True)
console = Console()


def _book_dir(book_id: str) -> Path:
    return Settings().artifacts_dir / book_id


def _voicebank_dir() -> Path:
    return Path("voicebank")


def _filter_chapters(chapters, selected: Optional[str]):
    if not selected:
        return chapters
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    return [c for c in chapters if c.chapter_id in wanted]


@app.command()
def ingest(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    book_id: Optional[str] = typer.Option(None, help="Defaults to folder name."),
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
):
    """Stage 0a: Extract an EPUB into the novels/-folder layout.

    Writes `NN-slug.txt` per narrative chapter, dumps every illustration to
    `images/`, and stores `.epub_meta.json` so the cover can later be embedded
    in the final m4b.

    Run `lnvox ingest <output_dir>` next to feed it into the rest of the pipeline.
    """
    from lnvox.ingest.epub import extract_epub

    console.print(f"Extracting [bold]{epub_path}[/] → [bold]{output_dir}[/]…")
    meta = extract_epub(
        epub_path,
        output_dir,
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


@app.command(name="s4")
def stage4(
    book_id: str,
    chapters: Optional[str] = typer.Option(None, help="Comma-separated chapter ids (default: all)."),
    limit: Optional[int] = typer.Option(
        None,
        help="Render at most N beats overall (smoke-test mode).",
    ),
    device: str = typer.Option("cuda", help="Torch device for Dramabox."),
):
    """Stage 4: Render directed beats to audio via Dramabox.

    Inputs:
      - artifacts/<book>/03_directed/*.json (Dramabox-ready prompts)
      - artifacts/<book>/04_voice_assignments.json (character → clip)
      - voicebank/manifest.json + voicebank/clips/*.wav

    Output:
      - artifacts/<book>/05_audio/<chapter>/<beat_id>.wav
      - artifacts/<book>/05_audio/<chapter>/manifest.json
    """
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

    audio_dir = out_dir / "05_audio"
    cache_dir = Path("cache") / "tts"

    def _factory():
        from lnvox.tts.dramabox_client import DramaboxClient

        return DramaboxClient(device=device)

    def _progress(msg):
        console.print(f"[dim]{msg}[/]")

    total_beats = sum(len(s.beats) for ch in chapters_loaded for s in ch.scenes)
    if limit is not None:
        total_beats = min(total_beats, limit)
    console.print(
        f"Rendering {total_beats} beat(s) across {len(chapters_loaded)} chapter(s) "
        f"→ {audio_dir}"
    )

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
):
    """Stage 5: Mix rendered beats into a final .m4b with chapter markers."""
    from lnvox.tts.schema import ChapterAudio

    out_dir = _book_dir(book_id)
    audio_root = out_dir / "05_audio"
    text_jsonl = out_dir / "00_text.jsonl"
    if not audio_root.exists():
        console.print(f"[red]Missing {audio_root}. Run `lnvox s4`.[/]")
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
    resolved_images_dir = images_dir if images_dir else (novels_root / book_id / "images")
    extra_images: list[Path] = []
    if resolved_images_dir.exists():
        extra_images = s5_mix.collect_images(resolved_images_dir)
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
):
    """LLM-match each character to a voicebank clip; write 04_voice_assignments.json.

    Auto-detects prior volumes in the same series. Characters with the same
    canonical name in a prior volume's assignments keep that clip (no LLM call).
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

    # Cross-volume reuse: load the most-recent prior volume's casting.
    artifacts_dir = Settings().artifacts_dir
    prior_dir = latest_prior_volume(artifacts_dir, book_id)
    prior_casting = None
    if prior_dir is not None:
        prior_assign = prior_dir / "04_voice_assignments.json"
        if prior_assign.exists():
            prior_casting = BookCasting.model_validate_json(
                prior_assign.read_text(encoding="utf-8")
            )
            console.print(
                f"[dim]Reusing assignments from prior volume: {prior_dir.name} "
                f"({len(prior_casting.castings)} cast entries)[/]"
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
        if prior_casting and any(
            c.character_name == character.name
            and c.assigned_clip_id == casting.assigned_clip_id
            for c in prior_casting.castings
        ):
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
        prior_casting=prior_casting,
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

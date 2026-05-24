"""Stage 4: Render directed beats to audio via Dramabox.

For each beat in `03_directed/<chapter>.json`, look up the speaker's assigned
voice clip from `04_voice_assignments.json`, then call Dramabox to render the
beat's `prompt` (already in screenplay format) to a WAV file. Content-hashed
cache makes re-runs after a single line edit cheap.

Output layout:
    artifacts/<book>/05_audio/<chapter_id>/<beat_id>.wav
    artifacts/<book>/05_audio/<chapter_id>/manifest.json
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Callable

from lnvox.llm.schemas import ChapterDirected
from lnvox.tts.schema import ChapterAudio, RenderedBeat
from lnvox.voices.schema import BookCasting, Voicebank


def _content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="ignore"))
        h.update(b"\x1f")  # separator
    return h.hexdigest()[:16]


def _wav_duration(path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        return 0.0


def _build_speaker_to_clip_path(
    casting: BookCasting,
    voicebank: Voicebank,
    voicebank_root: Path,
) -> dict[str, Path | None]:
    """character_name → Path to the assigned ref clip, or None if no assignment."""
    clip_by_id = {c.id: c for c in voicebank.clips}
    mapping: dict[str, Path | None] = {}
    for cst in casting.castings:
        if not cst.assigned_clip_id:
            mapping[cst.character_name] = None
            continue
        clip = clip_by_id.get(cst.assigned_clip_id)
        mapping[cst.character_name] = (
            (voicebank_root / clip.clip_path).resolve() if clip else None
        )
    return mapping


def render_chapter(
    chapter: ChapterDirected,
    *,
    client,  # DramaboxClient (typed loosely so this module imports without the optional dep)
    speaker_to_clip: dict[str, Path | None],
    output_dir: Path,
    cache_dir: Path,
    model_version: str,
    progress: Callable[[str], None] = print,
) -> ChapterAudio:
    """Render every beat in `chapter` to a WAV file. Returns a ChapterAudio manifest."""
    chapter_dir = output_dir / chapter.chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[RenderedBeat] = []
    total_dur = 0.0
    cache_hits = 0
    renders = 0

    for scene in chapter.scenes:
        for idx, beat in enumerate(scene.beats):
            beat_id = f"{scene.scene_id}_b{idx:04d}"
            wav_path = chapter_dir / f"{beat_id}.wav"

            ref_clip_path = speaker_to_clip.get(beat.speaker)
            ref_token = ref_clip_path.name if ref_clip_path else "no-ref"
            cache_key = _content_hash(beat.prompt, ref_token, model_version)
            cache_path = cache_dir / f"{cache_key}.wav"

            cached = False
            if cache_path.exists():
                shutil.copy(cache_path, wav_path)
                cached = True
                cache_hits += 1
            else:
                client.generate(
                    prompt=beat.prompt,
                    output_path=wav_path,
                    voice_ref=ref_clip_path,
                )
                if wav_path.exists():
                    shutil.copy(wav_path, cache_path)
                renders += 1

            dur = _wav_duration(wav_path)
            total_dur += dur
            rendered.append(
                RenderedBeat(
                    beat_id=beat_id,
                    scene_id=scene.scene_id,
                    type=beat.type,
                    speaker=beat.speaker,
                    wav_path=str(wav_path.relative_to(output_dir.parent)),
                    duration_seconds=round(dur, 3),
                    cache_key=cache_key,
                    cached=cached,
                )
            )

            if (renders + cache_hits) % 5 == 0:
                progress(
                    f"    {chapter.chapter_id}: {renders + cache_hits}/{sum(len(s.beats) for s in chapter.scenes)} "
                    f"(rendered={renders} cached={cache_hits} dur={total_dur:.1f}s)"
                )

    result = ChapterAudio(
        chapter_id=chapter.chapter_id,
        beats=rendered,
        total_duration_seconds=round(total_dur, 3),
    )
    (chapter_dir / "manifest.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    progress(
        f"  ✓ {chapter.chapter_id}: {len(rendered)} beats, {total_dur:.1f}s "
        f"({renders} rendered, {cache_hits} cached)"
    )
    return result


def run(
    chapters: list[ChapterDirected],
    casting: BookCasting,
    voicebank: Voicebank,
    voicebank_root: Path,
    output_dir: Path,
    cache_dir: Path,
    *,
    client_factory: Callable[[], "object"],
    model_version: str,
    on_chapter_done: Callable[[ChapterDirected, ChapterAudio], None] | None = None,
    progress: Callable[[str], None] = print,
    limit: int | None = None,
) -> list[ChapterAudio]:
    """Render every chapter, optionally truncating to the first `limit` beats overall.

    The Dramabox client is constructed lazily by `client_factory()` so this
    module imports cleanly even when the optional `tts` extra isn't installed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    speaker_to_clip = _build_speaker_to_clip_path(casting, voicebank, voicebank_root)

    if limit is not None:
        remaining = limit
        sliced: list[ChapterDirected] = []
        for ch in chapters:
            if remaining <= 0:
                break
            kept_scenes = []
            for sc in ch.scenes:
                if remaining <= 0:
                    break
                take = sc.beats[:remaining]
                if take:
                    kept_scenes.append(sc.model_copy(update={"beats": take}))
                    remaining -= len(take)
            if kept_scenes:
                sliced.append(ch.model_copy(update={"scenes": kept_scenes}))
        chapters = sliced

    progress(f"Loading Dramabox (first model load downloads ~3-4GB of weights)…")
    client = client_factory()

    results: list[ChapterAudio] = []
    for ch in chapters:
        result = render_chapter(
            ch,
            client=client,
            speaker_to_clip=speaker_to_clip,
            output_dir=output_dir,
            cache_dir=cache_dir,
            model_version=model_version,
            progress=progress,
        )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)
    return results

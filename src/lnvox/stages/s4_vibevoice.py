"""Stage 4, v2 renderer: VibeVoice multi-speaker sessions (DESIGN.md §16).

Instead of Dramabox's beat-at-a-time rendering, consecutive beats of a scene
are grouped into *sessions* (≤4 distinct speakers, ≤max_session_chars) and
each session is rendered in ONE VibeVoice call — dialogue turn-taking and
prosody flow continuously inside it. Output goes to `05_audio_v2/` so both
engines' renders coexist per book; the manifest reuses the ChapterAudio
schema with one RenderedBeat entry per session, so Stage 5 consumes it
unchanged (`lnvox s5 --v2`).

The planner is pure logic (no torch import) so it's unit-testable without
the model — see tests/test_sessions.py.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lnvox.llm.schemas import ChapterDirected, DirectedBeat, DirectedScene
from lnvox.stages.s4_tts import (
    _build_speaker_to_clip_path,
    _content_hash,
    _wav_duration,
    slice_chapters,
)
from lnvox.tts.schema import ChapterAudio, RenderedBeat
from lnvox.voices.schema import BookCasting, Voicebank

# VibeVoice-Large supports at most 4 distinct voices per generation session.
MAX_SPEAKERS_PER_SESSION = 4

# Cache-granularity vs. prosody-continuity tradeoff (DESIGN.md §16.3):
# ~3000 chars ≈ 3-4 min of speech; one edited line re-renders its session.
DEFAULT_MAX_SESSION_CHARS = 3000


def _collapse(text: str) -> str:
    """The script format is line-based — newlines inside a beat would start
    a new (speaker-less) line, so collapse all whitespace runs to spaces."""
    return " ".join(text.split())


@dataclass
class Session:
    """A consecutive run of beats within one scene, rendered in one call."""

    session_id: str
    scene_id: str
    speakers: list[str]  # order of first appearance == Speaker 1..N
    beats: list[DirectedBeat]

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.beats)

    def script(self) -> str:
        """`Speaker N: text` lines, N by order of first appearance."""
        number = {name: i + 1 for i, name in enumerate(self.speakers)}
        return "\n".join(
            f"Speaker {number[b.speaker]}: {_collapse(b.text)}" for b in self.beats
        )


def plan_scene(
    scene: DirectedScene, *, max_chars: int = DEFAULT_MAX_SESSION_CHARS
) -> list[Session]:
    """Greedily partition a scene's beats into sessions.

    A session closes when the next beat would introduce a 5th distinct
    speaker or push past `max_chars`. Beats are never split — a single
    over-long beat gets a session of its own. Sessions never span scenes.
    """
    sessions: list[Session] = []
    beats: list[DirectedBeat] = []
    speakers: list[str] = []
    chars = 0

    def close() -> None:
        nonlocal beats, speakers, chars
        if beats:
            sessions.append(
                Session(
                    session_id=f"{scene.scene_id}_v{len(sessions):03d}",
                    scene_id=scene.scene_id,
                    speakers=speakers,
                    beats=beats,
                )
            )
            beats, speakers, chars = [], [], 0

    for beat in scene.beats:
        is_new_speaker = beat.speaker not in speakers
        if beats and (
            (is_new_speaker and len(speakers) >= MAX_SPEAKERS_PER_SESSION)
            or chars + len(beat.text) > max_chars
        ):
            close()
        if beat.speaker not in speakers:
            speakers.append(beat.speaker)
        beats.append(beat)
        chars += len(beat.text)
    close()
    return sessions


def plan_chapter(
    chapter: ChapterDirected, *, max_chars: int = DEFAULT_MAX_SESSION_CHARS
) -> list[Session]:
    return [
        s for scene in chapter.scenes for s in plan_scene(scene, max_chars=max_chars)
    ]


def _resolve_refs(
    speakers: list[str], speaker_to_clip: dict[str, Path | None]
) -> list[Path]:
    """One cloning ref per speaker, in Speaker 1..N order.

    VibeVoice has no descriptor fallback — a ref is mandatory. Resolution:
    the speaker's assigned clip → the Narrator's clip → error. Deterministic,
    and the chosen filenames land in the cache key.
    """
    narrator_ref = speaker_to_clip.get("Narrator")
    refs: list[Path] = []
    for name in speakers:
        ref = speaker_to_clip.get(name) or narrator_ref
        if ref is None:
            raise RuntimeError(
                f"No voice clip assigned for speaker '{name}' and no Narrator "
                "fallback — re-run `lnvox voice cast` (VibeVoice needs a "
                "cloning reference for every speaker, DESIGN.md §16.3)."
            )
        refs.append(ref)
    return refs


def render_chapter(
    chapter: ChapterDirected,
    *,
    client=None,  # VibeVoiceClient (typed loosely so this module imports without the optional dep)
    client_provider: Callable[[], "object"] | None = None,  # lazy alternative
    speaker_to_clip: dict[str, Path | None],
    output_dir: Path,
    cache_dir: Path,
    model_version: str,
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
    progress: Callable[[str], None] = print,
) -> ChapterAudio:
    """Render every session in `chapter` to a WAV. Returns a ChapterAudio manifest.

    The client may be passed lazily via `client_provider` — resolved on the
    first cache MISS only, so a fully-cached re-run never loads the model.
    """

    def _client():
        nonlocal client
        if client is None:
            client = client_provider()
        return client

    chapter_dir = output_dir / chapter.chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    sessions = plan_chapter(chapter, max_chars=max_session_chars)
    rendered: list[RenderedBeat] = []
    total_dur = 0.0
    cache_hits = 0
    renders = 0

    for session in sessions:
        wav_path = chapter_dir / f"{session.session_id}.wav"
        script = session.script()
        refs = _resolve_refs(session.speakers, speaker_to_clip)
        ref_token = "|".join(p.name for p in refs)
        cache_key = _content_hash(script, ref_token, model_version)
        cache_path = cache_dir / f"{cache_key}.wav"

        cached = False
        if cache_path.exists():
            shutil.copy(cache_path, wav_path)
            cached = True
            cache_hits += 1
        else:
            _client().generate_session(
                script=script,
                voice_refs=refs,
                output_path=wav_path,
            )
            if wav_path.exists():
                shutil.copy(wav_path, cache_path)
            renders += 1

        dur = _wav_duration(wav_path)
        total_dur += dur
        rendered.append(
            RenderedBeat(
                beat_id=session.session_id,
                scene_id=session.scene_id,
                type=(
                    "dialogue"
                    if any(b.type == "dialogue" for b in session.beats)
                    else "narration"
                ),
                speaker=" + ".join(session.speakers),
                wav_path=str(wav_path.relative_to(output_dir.parent)),
                duration_seconds=round(dur, 3),
                cache_key=cache_key,
                cached=cached,
            )
        )
        progress(
            f"    {chapter.chapter_id}: {renders + cache_hits}/{len(sessions)} sessions "
            f"({len(session.beats)} beats, {len(session.speakers)} voice(s), "
            f"{'cached' if cached else f'{dur:.1f}s'})"
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
        f"  ✓ {chapter.chapter_id}: {len(rendered)} session(s), {total_dur:.1f}s "
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
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
    on_chapter_done: Callable[[ChapterDirected, ChapterAudio], None] | None = None,
    progress: Callable[[str], None] = print,
    limit: int | None = None,
) -> list[ChapterAudio]:
    """Render every chapter as VibeVoice sessions into `output_dir` (05_audio_v2).

    The client is constructed lazily by `client_factory()` so this module
    imports cleanly even when the vibevoice package isn't installed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    speaker_to_clip = _build_speaker_to_clip_path(casting, voicebank, voicebank_root)
    chapters = slice_chapters(chapters, limit)

    _holder: list = []

    def _get_client():
        if not _holder:
            progress("Loading VibeVoice-Large (first load downloads ~18GB of weights)…")
            _holder.append(client_factory())
        return _holder[0]

    results: list[ChapterAudio] = []
    for ch in chapters:
        result = render_chapter(
            ch,
            client_provider=_get_client,
            speaker_to_clip=speaker_to_clip,
            output_dir=output_dir,
            cache_dir=cache_dir,
            model_version=model_version,
            max_session_chars=max_session_chars,
            progress=progress,
        )
        results.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)
    return results

"""Scenario sync emitter (DESIGN.md §17.4).

Walks the structured script (`00_script.json`) against the directed beats
(`03_directed/`) and the rendered-audio manifests (`05_audio/*/manifest.json`)
and emits per-scene sync files plus a whole-play one: for every script item a
timed entry `{start, end, type, speaker, text, direction, emotion}`.

Timing is DETERMINISTIC — the same cursor math s5 uses (beat durations from
the manifests + the same pad values), never audio probing, so the sync file
and the m4b agree by construction:
- consecutive beats inside a staging-delimited run → intra pad;
- a staging run between two dialogue runs → one inter-scene pad (the
  "staging pause"), shared by every staging item of the run;
- scenes (chapters) separated by the inter-chapter pad;
- cues are zero-duration markers at the moment the preceding line ends.

Requires per-beat rendering (the Dramabox path). VibeVoice session manifests
(one entry per multi-line session) carry no per-line timing — rejected with
an explanatory error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from lnvox.llm.schemas import ChapterDirected, ScenarioScript, ScriptScene
from lnvox.tts.schema import ChapterAudio


# Pad between script scenes (s5's --inter-chapter in scenario mode). The book
# default (2.0 s) reads right between novel chapters but drags between play
# scenes — user-tested 2026-07-24, 0.75 s keeps sketch pacing (§17.4). The
# launcher passes the matching value to s5 so m4b and sync stay in agreement.
SCENARIO_INTER_SCENE = 0.75

# Extra silence prepended to EVERY beat in a scenario mix (s5 --lead-in). The
# 2reply reader app polls playback position every ~0.300 s on older devices;
# a dialogue entry's `start` points at the *beginning of the lead-in*, so the
# highlight always lands during silence, never after speech began (§17.4).
SCENARIO_LEAD_IN = 0.35


class SyncEntry(BaseModel):
    start: float
    end: float
    type: str  # dialogue | staging | cue
    speaker: str = ""  # original script label (group labels kept as printed)
    text: str
    direction: str = ""  # short cue in the script's language (dialogue only)
    emotion: str = ""  # 7-value enum (dialogue only)


class SceneSync(BaseModel):
    scene_id: str
    title: str = ""
    start: float = 0.0
    end: float = 0.0
    entries: list[SyncEntry] = Field(default_factory=list)


class PlaySync(BaseModel):
    scenario_id: str
    title: str = ""
    total_duration_seconds: float = 0.0
    intra_silence: float = 0.25
    staging_pause: float = 1.0
    inter_scene_silence: float = SCENARIO_INTER_SCENE  # between scenes (s5 inter-chapter)
    lead_in_silence: float = 0.0  # per-beat lead-in (s5 --lead-in)
    scenes: list[SceneSync] = Field(default_factory=list)


def _beat_times(
    chapter: ChapterDirected,
    manifest: ChapterAudio,
    *,
    offset: float,
    intra: float,
    staging_pause: float,
    lead_in: float = 0.0,
) -> tuple[dict[int, tuple[float, float]], float]:
    """Replay s5's concat plan → {dialogue line index: (start, end)}, chapter end.

    Manifest beats are in playback order and correspond 1:1 with the directed
    beats (per-beat rendering). A line split into chunks spans from its first
    chunk's start to its last chunk's end. `lead_in` silence precedes every
    beat in the mix; a line's `start` is the start of its FIRST chunk's
    lead-in — the app's poll margin (§17.4) — while `end` is where speech ends.
    """
    flat = [
        (ds.scene_id, b.source_paragraph)
        for ds in chapter.scenes
        for b in ds.beats
    ]
    if len(flat) != len(manifest.beats):
        raise ValueError(
            f"chapter {chapter.chapter_id}: {len(flat)} directed beat(s) but "
            f"{len(manifest.beats)} manifest entrie(s). scenario-sync needs "
            "per-beat rendering — re-run `lnvox s4` with the dramabox backend "
            "(VibeVoice session manifests carry no per-line timing, DESIGN.md §17.4)."
        )
    line_times: dict[int, tuple[float, float]] = {}
    cursor = offset
    prev_group: str | None = None
    for (group_id, line_idx), rendered in zip(flat, manifest.beats):
        if prev_group is not None:
            cursor += intra if group_id == prev_group else staging_pause
        start = cursor  # start of the lead-in, not of speech
        cursor += lead_in + rendered.duration_seconds
        s0, _ = line_times.get(line_idx, (start, start))
        line_times[line_idx] = (min(s0, start), cursor)
        prev_group = group_id
    return line_times, cursor


def build_scene_sync(
    scene: ScriptScene,
    chapter: ChapterDirected,
    manifest: ChapterAudio,
    *,
    offset: float = 0.0,
    intra: float = 0.25,
    staging_pause: float = 1.0,
    lead_in: float = 0.0,
) -> SceneSync:
    """Timed entries for one script scene, at absolute offset `offset`."""
    line_times, scene_end = _beat_times(
        chapter,
        manifest,
        offset=offset,
        intra=intra,
        staging_pause=staging_pause,
        lead_in=lead_in,
    )
    # direction/emotion per line, from the first directed chunk of that line.
    line_meta: dict[int, tuple[str, str]] = {}
    for ds in chapter.scenes:
        for b in ds.beats:
            line_meta.setdefault(b.source_paragraph, (b.direction, b.emotion))

    # Per item: the start of the next dialogue line at-or-after it (else scene
    # end) — the far edge of a staging run's pause window.
    next_line_start: list[float] = [scene_end] * len(scene.items)
    upcoming = scene_end
    line_no = sum(1 for it in scene.items if it.type == "dialogue")
    for i in range(len(scene.items) - 1, -1, -1):
        if scene.items[i].type == "dialogue":
            upcoming = line_times.get(line_no, (scene_end, scene_end))[0]
            line_no -= 1
        next_line_start[i] = upcoming

    entries: list[SyncEntry] = []
    prev_end = offset
    line_no = 0
    for i, item in enumerate(scene.items):
        if item.type == "dialogue":
            line_no += 1
            start, end = line_times.get(line_no, (prev_end, prev_end))
            direction, emotion = line_meta.get(line_no, ("", "calm"))
            entries.append(
                SyncEntry(
                    start=round(start, 3),
                    end=round(end, 3),
                    type="dialogue",
                    speaker=item.speaker,
                    text=item.text,
                    direction=direction,
                    emotion=emotion,
                )
            )
            prev_end = end
        elif item.type == "staging":
            entries.append(
                SyncEntry(
                    start=round(prev_end, 3),
                    end=round(next_line_start[i], 3),
                    type="staging",
                    text=item.text,
                )
            )
        else:  # cue
            entries.append(
                SyncEntry(
                    start=round(prev_end, 3),
                    end=round(prev_end, 3),
                    type="cue",
                    text=item.text,
                )
            )
    return SceneSync(
        scene_id=scene.scene_id,
        title=scene.title,
        start=round(offset, 3),
        end=round(scene_end, 3),
        entries=entries,
    )


def build_play_sync(
    script: ScenarioScript,
    chapters: dict[str, ChapterDirected],
    manifests: dict[str, ChapterAudio],
    *,
    intra: float = 0.25,
    staging_pause: float = 1.0,
    inter_scene: float = SCENARIO_INTER_SCENE,
    lead_in: float = 0.0,
) -> PlaySync:
    """Absolute-time sync for the whole play (scenes in script order)."""
    scenes: list[SceneSync] = []
    cursor = 0.0
    for scene in script.scenes:
        chapter = chapters.get(scene.scene_id)
        manifest = manifests.get(scene.scene_id)
        if chapter is None or manifest is None:
            raise FileNotFoundError(
                f"scene {scene.scene_id}: missing directed chapter or audio "
                "manifest — run `lnvox scenario` and `lnvox s4` first."
            )
        scene_sync = build_scene_sync(
            scene,
            chapter,
            manifest,
            offset=cursor,
            intra=intra,
            staging_pause=staging_pause,
            lead_in=lead_in,
        )
        scenes.append(scene_sync)
        cursor = scene_sync.end + inter_scene
    total = scenes[-1].end if scenes else 0.0
    return PlaySync(
        scenario_id=script.scenario_id,
        title=script.title,
        total_duration_seconds=round(total, 3),
        intra_silence=intra,
        staging_pause=staging_pause,
        inter_scene_silence=inter_scene,
        lead_in_silence=lead_in,
        scenes=scenes,
    )


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(play: PlaySync, *, min_display: float = 1.5) -> str:
    """SRT export: dialogue as `SPEAKER: text`, staging bracketed, cues
    double-bracketed. Zero-width entries get `min_display` seconds on screen."""
    blocks: list[str] = []
    n = 0
    for scene in play.scenes:
        for e in scene.entries:
            if e.type == "dialogue":
                payload = f"{e.speaker}: {e.text}"
            elif e.type == "staging":
                payload = f"[{e.text}]"
            else:
                payload = f"(({e.text}))"
            end = e.end if e.end > e.start else e.start + min_display
            n += 1
            blocks.append(f"{n}\n{_srt_time(e.start)} --> {_srt_time(end)}\n{payload}\n")
    return "\n".join(blocks)


def run(
    book_dir: Path,
    *,
    intra: float = 0.25,
    staging_pause: float = 1.0,
    inter_scene: float = SCENARIO_INTER_SCENE,
    lead_in: float = SCENARIO_LEAD_IN,
    progress: Callable[[str], None] = print,
) -> PlaySync:
    """Read artifacts, emit 07_sync/<scene>.json + play.json + play.srt."""
    script = ScenarioScript.model_validate_json(
        (book_dir / "00_script.json").read_text(encoding="utf-8")
    )
    chapters: dict[str, ChapterDirected] = {}
    manifests: dict[str, ChapterAudio] = {}
    for scene in script.scenes:
        directed = book_dir / "03_directed" / f"{scene.scene_id}.json"
        manifest = book_dir / "05_audio" / scene.scene_id / "manifest.json"
        if directed.exists():
            chapters[scene.scene_id] = ChapterDirected.model_validate_json(
                directed.read_text(encoding="utf-8")
            )
        if manifest.exists():
            manifests[scene.scene_id] = ChapterAudio.model_validate_json(
                manifest.read_text(encoding="utf-8")
            )

    play = build_play_sync(
        script,
        chapters,
        manifests,
        intra=intra,
        staging_pause=staging_pause,
        inter_scene=inter_scene,
        lead_in=lead_in,
    )

    sync_dir = book_dir / "07_sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    for scene_sync in play.scenes:
        (sync_dir / f"{scene_sync.scene_id}.json").write_text(
            scene_sync.model_dump_json(indent=2), encoding="utf-8"
        )
    (sync_dir / "play.json").write_text(play.model_dump_json(indent=2), encoding="utf-8")
    (sync_dir / "play.srt").write_text(to_srt(play), encoding="utf-8")
    progress(
        f"  ✓ {len(play.scenes)} scene sync file(s), "
        f"{sum(len(s.entries) for s in play.scenes)} entrie(s), "
        f"{play.total_duration_seconds / 60:.1f} min — {sync_dir}/"
    )
    return play

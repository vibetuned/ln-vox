"""Tests for the VibeVoice session renderer (DESIGN.md §16).

Covers the model-free pieces: session planning invariants (speaker cap,
char cap, scene boundaries, speaker numbering), script formatting, ref
resolution, and the render/cache/manifest loop with a fake client.
Runnable directly (`python tests/test_sessions.py`) or under pytest.
"""

import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.llm.schemas import ChapterDirected, DirectedBeat, DirectedScene
from lnvox.stages.s4_vibevoice import (
    MAX_SPEAKERS_PER_SESSION,
    _resolve_refs,
    plan_chapter,
    plan_scene,
    render_chapter,
)
from lnvox.tts.schema import ChapterAudio


def _beat(speaker: str, text: str, type_: str = "dialogue") -> DirectedBeat:
    return DirectedBeat(
        type=type_,
        text=text,
        speaker=speaker,
        direction="adult, calm voice",
        prompt=f'adult, calm voice, "{text}"',
    )


def _scene(scene_id: str, beats) -> DirectedScene:
    return DirectedScene(scene_id=scene_id, beats=list(beats))


def _write_wav(path: Path, seconds: float = 0.1, sr: int = 48_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00\x00\x00" * int(seconds * sr))


class FakeClient:
    """Stands in for VibeVoiceClient; records calls, writes a stub WAV."""

    def __init__(self):
        self.calls = []

    def generate_session(self, *, script, voice_refs, output_path):
        self.calls.append((script, tuple(voice_refs)))
        _write_wav(output_path)


# ---- planner ----------------------------------------------------------------


def test_single_session_when_under_caps():
    scene = _scene(
        "01_s1",
        [
            _beat("Narrator", "It was a quiet morning.", "narration"),
            _beat("Touma", "Such misfortune."),
            _beat("Narrator", "He sighed.", "narration"),
        ],
    )
    sessions = plan_scene(scene)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "01_s1_v000"
    assert s.scene_id == "01_s1"
    assert s.speakers == ["Narrator", "Touma"]  # order of first appearance
    assert len(s.beats) == 3


def test_speaker_cap_splits_session():
    # 4 distinct speakers fit; the 5th forces a new session.
    scene = _scene(
        "01_s1",
        [
            _beat("Narrator", "a", "narration"),
            _beat("Touma", "b"),
            _beat("Index", "c"),
            _beat("Mikoto", "d"),
            _beat("Stiyl", "e"),  # 5th distinct speaker → split
            _beat("Narrator", "f", "narration"),
        ],
    )
    sessions = plan_scene(scene)
    assert len(sessions) == 2
    assert len(sessions[0].speakers) == MAX_SPEAKERS_PER_SESSION
    assert sessions[0].speakers == ["Narrator", "Touma", "Index", "Mikoto"]
    # The new session restarts numbering: Stiyl is its Speaker 1.
    assert sessions[1].speakers == ["Stiyl", "Narrator"]
    assert sessions[1].session_id == "01_s1_v001"


def test_char_cap_splits_session():
    scene = _scene("01_s1", [_beat("Narrator", "x" * 60, "narration") for _ in range(3)])
    sessions = plan_scene(scene, max_chars=100)
    # 60 + 60 > 100, so each beat lands in its own session.
    assert len(sessions) == 3
    # A single over-long beat is never split — it gets its own session.
    long_scene = _scene("01_s2", [_beat("Narrator", "y" * 500, "narration")])
    assert len(plan_scene(long_scene, max_chars=100)) == 1


def test_script_numbering_and_whitespace_collapse():
    scene = _scene(
        "01_s1",
        [
            _beat("Narrator", "Hello\n  world", "narration"),
            _beat("Alice", "Hi"),
            _beat("Narrator", "Again", "narration"),
        ],
    )
    (s,) = plan_scene(scene)
    assert s.script().splitlines() == [
        "Speaker 1: Hello world",
        "Speaker 2: Hi",
        "Speaker 1: Again",
    ]


def test_sessions_never_span_scenes():
    chapter = ChapterDirected(
        chapter_id="01",
        scenes=[
            _scene("01_s1", [_beat("Narrator", "a", "narration")]),
            _scene("01_s2", [_beat("Narrator", "b", "narration")]),
        ],
    )
    sessions = plan_chapter(chapter)
    assert [s.session_id for s in sessions] == ["01_s1_v000", "01_s2_v000"]


# ---- ref resolution ----------------------------------------------------------


def test_resolve_refs_fallback_and_error():
    narrator = Path("/vb/narrator.wav")
    alice = Path("/vb/alice.wav")
    mapping = {"Narrator": narrator, "Alice": alice, "Bob": None}
    # Assigned clip wins; unassigned (None or missing) falls back to Narrator.
    assert _resolve_refs(["Alice", "Bob", "Unknown"], mapping) == [
        alice,
        narrator,
        narrator,
    ]
    try:
        _resolve_refs(["Alice"], {"Alice": None})
        raise AssertionError("expected RuntimeError when no ref and no Narrator")
    except RuntimeError as exc:
        assert "voice cast" in str(exc)


# ---- casting → clip map guard ---------------------------------------------------


def test_speaker_clip_map_rejects_wrong_voicebank():
    from lnvox.stages.s4_tts import _build_speaker_to_clip_path
    from lnvox.voices.schema import (
        BookCasting,
        CharacterCasting,
        VoiceClip,
        Voicebank,
        VoiceTarget,
    )

    target = VoiceTarget(gender="female", age_band="adult")
    bank = Voicebank(
        clips=[
            VoiceClip(
                id="clip_a",
                source="manual",
                clip_path="clips/a.wav",
                duration_seconds=10.0,
                gender="female",
                age_band="adult",
                accent="any",
            )
        ]
    )
    ok_casting = BookCasting(
        book_id="t",
        castings=[
            CharacterCasting(character_name="Ana", target=target, assigned_clip_id="clip_a"),
            CharacterCasting(character_name="Uncast", target=target),  # legit None
        ],
    )
    mapping = _build_speaker_to_clip_path(ok_casting, bank, Path("/vb"))
    assert mapping["Ana"] is not None and mapping["Uncast"] is None

    # An ASSIGNED id missing from the loaded bank (wrong LNVOX_VOICEBANK)
    # must fail loudly, not silently degrade to no-ref.
    bad_casting = BookCasting(
        book_id="t",
        castings=[
            CharacterCasting(character_name="Ana", target=target, assigned_clip_id="clip_fr_x")
        ],
    )
    try:
        _build_speaker_to_clip_path(bad_casting, bank, Path("/vb"))
        raise AssertionError("expected RuntimeError for missing assigned clip id")
    except RuntimeError as exc:
        assert "LNVOX_VOICEBANK" in str(exc) and "clip_fr_x" in str(exc)


# ---- renderer / cache / manifest ---------------------------------------------


def test_render_chapter_manifest_and_cache():
    chapter = ChapterDirected(
        chapter_id="01",
        scenes=[
            _scene(
                "01_s1",
                [
                    _beat("Narrator", "It was morning.", "narration"),
                    _beat("Alice", "Good morning!"),
                    _beat("Narrator", "She waved.", "narration"),
                ],
            )
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ref_n = tmp / "vb" / "narrator.wav"
        ref_a = tmp / "vb" / "alice.wav"
        _write_wav(ref_n)
        _write_wav(ref_a)
        speaker_to_clip = {"Narrator": ref_n, "Alice": ref_a}
        output_dir = tmp / "book" / "05_audio_v2"
        cache_dir = tmp / "cache"

        client = FakeClient()
        result = render_chapter(
            chapter,
            client=client,
            speaker_to_clip=speaker_to_clip,
            output_dir=output_dir,
            cache_dir=cache_dir,
            model_version="test-v0",
            progress=lambda _: None,
        )

        assert len(client.calls) == 1
        script, refs = client.calls[0]
        assert script.splitlines()[0] == "Speaker 1: It was morning."
        assert refs == (ref_n, ref_a)  # Speaker 1..N order

        assert len(result.beats) == 1
        entry = result.beats[0]
        assert entry.beat_id == "01_s1_v000"
        assert entry.speaker == "Narrator + Alice"
        assert entry.type == "dialogue"  # any dialogue beat → dialogue
        assert entry.wav_path == "05_audio_v2/01/01_s1_v000.wav"
        assert not entry.cached
        assert (output_dir / "01" / "01_s1_v000.wav").exists()

        # The manifest round-trips through the schema Stage 5 consumes.
        manifest = output_dir / "01" / "manifest.json"
        reloaded = ChapterAudio.model_validate_json(
            manifest.read_text(encoding="utf-8")
        )
        assert reloaded.beats[0].cache_key == entry.cache_key

        # Second render: everything comes from cache, the client is idle.
        client2 = FakeClient()
        result2 = render_chapter(
            chapter,
            client=client2,
            speaker_to_clip=speaker_to_clip,
            output_dir=output_dir,
            cache_dir=cache_dir,
            model_version="test-v0",
            progress=lambda _: None,
        )
        assert client2.calls == []
        assert result2.beats[0].cached

        # A different model version re-keys the cache → re-render.
        client3 = FakeClient()
        render_chapter(
            chapter,
            client=client3,
            speaker_to_clip=speaker_to_clip,
            output_dir=output_dir,
            cache_dir=cache_dir,
            model_version="test-v1",
            progress=lambda _: None,
        )
        assert len(client3.calls) == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

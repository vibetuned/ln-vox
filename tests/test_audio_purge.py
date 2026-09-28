"""`lnvox audio purge` on both render layouts.

Dramabox (05_audio/) renders one WAV per beat and names one speaker per beat.
VibeVoice (05_audio_v2/, `--v2`) renders one WAV per multi-speaker session and
names every voice in it as "A + B + C" — purge used to ignore that directory
entirely. A speaker purge there means every session the speaker appears in.

Runnable directly: `.venv/bin/python tests/test_audio_purge.py`.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typer.testing import CliRunner  # noqa: E402

from lnvox import cli  # noqa: E402
from lnvox.tts.schema import ChapterAudio, RenderedBeat  # noqa: E402


def _layout(root: Path, sub: str, entries: list[tuple[str, str]]) -> Path:
    """Write `<sub>/01/` with one WAV + cache entry per (beat_id, speaker)."""
    chap = root / "artifacts" / "demo" / sub / "01"
    chap.mkdir(parents=True)
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    beats = []
    for beat_id, speaker in entries:
        (chap / f"{beat_id}.wav").write_bytes(b"wav")
        (cache / f"k_{sub}_{beat_id}.wav").write_bytes(b"wav")
        beats.append(RenderedBeat(
            beat_id=beat_id, scene_id="01_s1", type="dialogue", speaker=speaker,
            wav_path=f"{sub}/01/{beat_id}.wav", duration_seconds=1.0,
            cache_key=f"k_{sub}_{beat_id}",
        ))
    (chap / "manifest.json").write_text(
        ChapterAudio(chapter_id="01", beats=beats).model_dump_json()
    )
    return chap


def _purge(root: Path, *args: str):
    env = {"LNVOX_ARTIFACTS_DIR": str(root / "artifacts")}
    return CliRunner().invoke(
        cli.app,
        ["audio", "purge", "demo", "--cache-dir", str(root / "cache"), "--yes", *args],
        env=env,
    )


def _left(chap: Path) -> list[str]:
    return sorted(p.stem for p in chap.glob("*.wav"))


def test_dramabox_purge_matches_the_speaker_exactly():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        chap = _layout(root, "05_audio", [("b0", "Bo"), ("b1", "Bob"), ("b2", "Bo")])
        v2 = _layout(root, "05_audio_v2", [("s0", "Bo + Ann")])
        res = _purge(root, "--speaker", "Bo")
        assert res.exit_code == 0, res.output
        assert _left(chap) == ["b1"]
        assert _left(v2) == ["s0"], "the default purge must not touch 05_audio_v2/"
        assert sorted(p.name for p in (root / "cache").iterdir()) == [
            "k_05_audio_b1.wav", "k_05_audio_v2_s0.wav"]


def test_v2_purge_takes_every_session_the_speaker_is_in():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        v2 = _layout(root, "05_audio_v2", [
            ("s0", "Narrator + Bo"), ("s1", "Narrator"), ("s2", "Ann + Bo + Cy"),
            ("s3", "Bob"),
        ])
        res = _purge(root, "--speaker", "Bo", "--v2")
        assert res.exit_code == 0, res.output
        assert _left(v2) == ["s1", "s3"]
        assert not (root / "cache" / "k_05_audio_v2_s0.wav").exists()
        assert not (root / "cache" / "k_05_audio_v2_s2.wav").exists()
        assert (root / "cache" / "k_05_audio_v2_s3.wav").exists()
        assert "session" in res.output


def test_missing_layout_points_at_the_other_one():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _layout(root, "05_audio_v2", [("s0", "Bo")])
        res = _purge(root, "--speaker", "Bo")
        assert res.exit_code == 1
        assert "--v2" in res.output


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    os.environ.setdefault("COLUMNS", "200")
    _run_all()

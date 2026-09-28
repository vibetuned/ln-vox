"""`lnvox s4` refusals and knob handling at the CLI level (renderers stubbed).

Deterministic refusals exit 2 (s4_retry.sh doesn't replay them); an EMPTY
render knob is unset, not a request; monolithic Dramabox on MPS is refused
(it renders silence — MAC_TEST_REPORT.md F10) while --staged proceeds.
Runnable directly (`python tests/test_s4_cli.py`) or under pytest.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typer.testing import CliRunner

import lnvox.cli as cli
from lnvox.llm.schemas import ChapterDirected, DirectedBeat, DirectedScene
from lnvox.stages import s4_tts
from lnvox.tts import staged_driver
from lnvox.tts.dramabox_client import DramaboxClient
from lnvox.voices.schema import BookCasting, VoiceClip, Voicebank

_KNOBS = ("LNVOX_S4_ENCODER_PRECISION", "LNVOX_S4_CHUNK_CAP", "LNVOX_S4_DECODE_DTYPE")


def _setup(root: Path) -> dict:
    vb = root / "vb"
    vb.mkdir()
    (vb / "manifest.json").write_text(Voicebank(clips=[VoiceClip(
        id="n", source="manual", clip_path="clips/n.wav", duration_seconds=12.0,
        gender="female", age_band="adult")]).model_dump_json())
    book = root / "artifacts" / "b"
    (book / "03_directed").mkdir(parents=True)
    beat = DirectedBeat(type="narration", text="Hi.", speaker="Narrator",
                        direction="calm", prompt='(calm) "Hi."')
    (book / "03_directed" / "ch01.json").write_text(ChapterDirected(
        chapter_id="ch01", scenes=[DirectedScene(scene_id="ch01_s1", beats=[beat])]
    ).model_dump_json())
    (book / "04_voice_assignments.json").write_text(BookCasting(book_id="b").model_dump_json())
    env = {"LNVOX_VOICEBANK": str(vb), "LNVOX_ARTIFACTS_DIR": str(root / "artifacts")}
    env.update({k: None for k in _KNOBS})  # CliRunner: None = unset
    return env


def _invoke(args, env):
    """Run `lnvox s4 …` with both renderers replaced by recorders."""
    calls = []
    saved = (s4_tts.run, staged_driver.run_staged)
    s4_tts.run = lambda *a, **kw: calls.append(("mono", kw["model_version"]))
    staged_driver.run_staged = lambda **kw: calls.append(("staged", kw["device"]))
    try:
        res = CliRunner().invoke(cli.app, ["s4", "b", *args], env=env)
    finally:
        s4_tts.run, staged_driver.run_staged = saved
    return res, calls


def test_deterministic_refusals_exit_2():
    with tempfile.TemporaryDirectory() as tmp:
        env = _setup(Path(tmp))
        cases = [
            (["--tts-backend", "nope"], {}),
            (["--tts-backend", "vibevoice", "--staged"], {}),
            (["--device", "cpu"], {"LNVOX_S4_CHUNK_CAP": "22"}),  # staged-only knob
            (["--device", "cpu"], {"LNVOX_S4_DECODE_DTYPE": "fp32"}),
            (["--device", "cpu", "--staged"], {"LNVOX_S4_CHUNK_CAP": "abc"}),  # bad value
            (["--device", "cpu"], {"LNVOX_S4_ENCODER_PRECISION": "fp8"}),
        ]
        for args, extra in cases:
            res, calls = _invoke(args, {**env, **extra})
            assert res.exit_code == 2, (args, extra, res.output)
            assert calls == [], (args, extra)


def test_empty_knobs_are_unset_on_the_monolithic_path():
    with tempfile.TemporaryDirectory() as tmp:
        env = _setup(Path(tmp))
        res, calls = _invoke(["--device", "cpu"], {
            **env, "LNVOX_S4_CHUNK_CAP": "", "LNVOX_S4_DECODE_DTYPE": "  "})
        assert res.exit_code == 0, res.output
        assert calls == [("mono", DramaboxClient.MODEL_VERSION)]  # v1 key, untagged
        # An explicit opt-out is not a staged-only request either.
        res, calls = _invoke(["--device", "cpu"], {
            **env, "LNVOX_S4_CHUNK_CAP": "none", "LNVOX_S4_DECODE_DTYPE": "auto"})
        assert res.exit_code == 0 and calls[0][0] == "mono", res.output


def test_monolithic_mps_refused_staged_mps_proceeds():
    with tempfile.TemporaryDirectory() as tmp:
        env = _setup(Path(tmp))
        res, calls = _invoke(["--device", "mps"], env)
        assert res.exit_code == 2, res.output
        assert "--staged" in res.output and calls == []
        res, calls = _invoke(["--device", "mps", "--staged"], env)
        assert res.exit_code == 0, res.output
        assert calls == [("staged", "mps")]
        # CUDA / CPU monolithic unchanged.
        res, calls = _invoke(["--device", "cuda"], env)
        assert res.exit_code == 0 and calls[0][0] == "mono", res.output


def test_dramabox_client_refuses_mps_before_loading():
    try:
        DramaboxClient(device="mps")
    except RuntimeError as exc:
        assert "--staged" in str(exc)
    else:
        raise AssertionError("DramaboxClient must refuse MPS")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

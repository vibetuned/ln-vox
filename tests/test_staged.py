"""Tests for the staged Dramabox s4 path (DESIGN.md §15) — model-free.

Covers: the cache-key variant tag per device (the EFFECTIVE recipe — MPS
can't honor the revert knobs), the chunk policy shared by build_plan and
the Studio's lab engine, decode-phase placement (the file in 05_audio must
be the cache entry the manifest names — F13 re-renders AND reverts to an
earlier render), and the driver's verification of the placement outputs.
DramaBox / ltx / the model loaders are stubbed; no model is ever loaded.
Runnable directly (`python tests/test_staged.py`) or under pytest.
"""

import contextlib
import json
import math
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import soundfile as sf

from lnvox.llm.schemas import ChapterDirected, DirectedBeat, DirectedScene
from lnvox.tts import dramabox_client, staged, staged_driver
from lnvox.tts.dramabox_client import DramaboxClient, s4_variant_tag
from lnvox.tts.schema import ChapterAudio
from lnvox.tts.staged import (
    PlanBeat,
    PlanParams,
    PlanRender,
    StagedPlan,
    apply_chunk_policy,
)
from lnvox.voices.schema import BookCasting, Voicebank

SR = 48_000
_KNOBS = ("LNVOX_S4_ENCODER_PRECISION", "LNVOX_S4_CHUNK_CAP", "LNVOX_S4_DECODE_DTYPE")
DEFAULT_TAG = "+enc-bf16+cap22+dec-fp32"


@contextlib.contextmanager
def _env(**values):
    """Set exactly these render knobs (others unset) for the block."""
    saved = {k: os.environ.get(k) for k in _KNOBS + ("LNVOX_S4_NO_TRIM",)}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ---- DramaBox stubs (planner side) --------------------------------------------


def _install_planner_stubs():
    """text_chunker / duration_estimator stand-ins: 0.4 s per word, and a
    chunker that returns a quoted utterance WHOLE (the F11 case that needs
    the word-split fallback)."""
    de = types.ModuleType("duration_estimator")
    de.estimate_speech_duration = lambda text: 0.4 * len(text.split())
    tc = types.ModuleType("text_chunker")
    tc.chunk_prompt_for_duration = lambda prompt, **kw: [types.SimpleNamespace(text=prompt)]
    sys.modules["duration_estimator"] = de
    sys.modules["text_chunker"] = tc
    staged._ensure_ltx_path = lambda: None


_install_planner_stubs()

LONG_PROMPT = '(calm, adult) "' + " ".join(f"w{i}" for i in range(100)) + '"'  # ≈44 s


# ---- variant tag --------------------------------------------------------------


def test_default_keys_unchanged_on_every_device():
    with _env():
        for dev in ("cuda", "cuda:1", "mps", "cpu"):
            assert s4_variant_tag(dev) == DEFAULT_TAG, dev
            assert s4_variant_tag(dev, staged=False) == "", dev
    with _env(LNVOX_S4_ENCODER_PRECISION="bf16"):
        assert s4_variant_tag("cuda", staged=False) == "+enc-bf16"


def test_cuda_reverts_keep_their_keys():
    cases = [
        ({"LNVOX_S4_ENCODER_PRECISION": "bnb4"}, "+cap22+dec-fp32"),
        ({"LNVOX_S4_CHUNK_CAP": "none"}, "+enc-bf16+dec-fp32"),
        ({"LNVOX_S4_DECODE_DTYPE": "auto"}, "+enc-bf16+cap22"),
        (
            {
                "LNVOX_S4_ENCODER_PRECISION": "bnb4",
                "LNVOX_S4_CHUNK_CAP": "off",
                "LNVOX_S4_DECODE_DTYPE": "auto",
            },
            "",
        ),
        ({"LNVOX_S4_CHUNK_CAP": "30"}, "+enc-bf16+cap30+dec-fp32"),
    ]
    for env, tag in cases:
        with _env(**env):
            assert s4_variant_tag("cuda") == tag, (env, s4_variant_tag("cuda"))


def test_mps_tag_is_the_effective_recipe():
    # bnb4 can't load on MPS, decode is forced fp32, cap=none stays 22 s:
    # none of these change the audio there, so none may change the key.
    for env in (
        {"LNVOX_S4_ENCODER_PRECISION": "bnb4"},
        {"LNVOX_S4_CHUNK_CAP": "none"},
        {"LNVOX_S4_DECODE_DTYPE": "auto"},
        {
            "LNVOX_S4_ENCODER_PRECISION": "bnb4",
            "LNVOX_S4_CHUNK_CAP": "0",
            "LNVOX_S4_DECODE_DTYPE": "auto",
        },
    ):
        with _env(**env):
            assert s4_variant_tag("mps") == DEFAULT_TAG, env
    # The Mac's 22 s-chunk render must not land on the key CUDA uses for
    # real 45 s chunks.
    with _env(LNVOX_S4_CHUNK_CAP="none"):
        assert s4_variant_tag("cuda") == "+enc-bf16+dec-fp32"
        assert s4_variant_tag("mps") != s4_variant_tag("cuda")
    with _env(LNVOX_S4_CHUNK_CAP="30", LNVOX_S4_ENCODER_PRECISION="bnb4"):
        assert s4_variant_tag("mps") == "+enc-bf16+cap30+dec-fp32"


def test_empty_knob_is_unset_and_bad_values_raise():
    with _env(LNVOX_S4_CHUNK_CAP="", LNVOX_S4_DECODE_DTYPE=" ", LNVOX_S4_ENCODER_PRECISION=""):
        assert s4_variant_tag("cuda") == DEFAULT_TAG
    for env in ({"LNVOX_S4_CHUNK_CAP": "abc"}, {"LNVOX_S4_CHUNK_CAP": "-5"},
                {"LNVOX_S4_DECODE_DTYPE": "fp16"}, {"LNVOX_S4_ENCODER_PRECISION": "fp8"}):
        with _env(**env):
            for dev in ("cuda", "mps"):
                try:
                    s4_variant_tag(dev)
                except ValueError:
                    continue
                raise AssertionError(f"{env} on {dev} should be rejected")


# ---- chunk policy (build_plan ≡ lab engine) -----------------------------------


def test_chunk_policy_per_device():
    cases = [
        ("cuda", {}, (22.0, 18.0, True)),
        ("cuda", {"LNVOX_S4_CHUNK_CAP": "none"}, (45.0, 37.0, False)),
        ("cuda", {"LNVOX_S4_CHUNK_CAP": "8"}, (8.0, 6.0, True)),
        ("mps", {}, (22.0, 18.0, True)),
        ("mps", {"LNVOX_S4_CHUNK_CAP": "none"}, (22.0, 18.0, True)),
        ("mps", {"LNVOX_S4_CHUNK_CAP": "30"}, (30.0, 26.0, True)),
    ]
    for dev, env, want in cases:
        with _env(**env):
            p = PlanParams(**DramaboxClient.DEFAULT_PARAMS)
            force = apply_chunk_policy(p, dev)
            assert (p.max_chunk_duration, p.target_chunk_duration, force) == want, (dev, env)


def _directed(prompt: str) -> list[ChapterDirected]:
    beat = DirectedBeat(type="narration", text="t", speaker="Narrator",
                        direction="calm", prompt=prompt)
    return [ChapterDirected(chapter_id="ch01",
                            scenes=[DirectedScene(scene_id="ch01_s1", beats=[beat])])]


def _plan_texts(device: str, cache_dir: Path) -> list[str]:
    plan = staged.build_plan("b", _directed(LONG_PROMPT), BookCasting(book_id="b"),
                             Voicebank(), cache_dir, cache_dir, device=device)
    return [c.text for c in plan.renders[0].chunks]


class _Rec:
    """Records constructor kwargs; callable returns an encoder-shaped result."""

    made: list = []

    def __init__(self, *a, **kw):
        self.kw = kw
        type(self).made.append(self)

    def __call__(self, *a, **kw):
        enc = types.SimpleNamespace(to=lambda *a, **k: "ctx")
        return [types.SimpleNamespace(audio_encoding=enc)]


def _make_lab_engine(device: str):
    """Build a StagedLabEngine with every model loader stubbed."""
    from lnvox.tts import lab_engine

    mods = {
        "ltx_core": {}, "ltx_core.components": {},
        "ltx_core.components.patchifiers": {"AudioPatchifier": _Rec},
        "ltx_pipelines": {}, "ltx_pipelines.utils": {},
        "ltx_pipelines.utils.blocks": {
            "AudioConditioner": _Rec, "AudioDecoder": _Rec, "PromptEncoder": _Rec,
        },
        "model_downloader": {"get_all_paths": lambda: {
            "audio_components": "ac", "gemma_root": "gemma-bnb4", "transformer": "dit"}},
        "inference_server": {"DEFAULT_NEG": "neg"},
    }
    for name, attrs in mods.items():
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
    dramabox_client._ensure_path = lambda: None
    dramabox_client._patch_torchaudio_save = lambda: None
    dramabox_client.resolve_gemma_root = lambda root, bnb: root if bnb else "gemma-bf16"
    staged._build_dit = lambda *a, **k: "dit"
    _Rec.made = []
    return lab_engine.StagedLabEngine(device=device)


def test_lab_engine_uses_the_plan_chunk_policy():
    import torch

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        for dev, env, want_max in (
            ("mps", {"LNVOX_S4_CHUNK_CAP": "30"}, 30.0),  # was hard-coded 22 on MPS
            ("mps", {"LNVOX_S4_CHUNK_CAP": "none"}, 22.0),
            ("cuda", {"LNVOX_S4_CHUNK_CAP": "none"}, 45.0),
            ("cuda", {}, 22.0),
        ):
            with _env(**env):
                eng = _make_lab_engine(dev)
                assert eng.params.max_chunk_duration == want_max, (dev, env)
                texts = eng._chunk_texts(LONG_PROMPT, eng.params.duration_multiplier)
                assert texts == _plan_texts(dev, cache), (dev, env)
                # Encoder / decoder follow the staged phases' rules too.
                enc = next(r for r in _Rec.made if "use_bnb_4bit" in r.kw)
                assert enc.kw["use_bnb_4bit"] is False and enc.kw["dtype"] == torch.bfloat16
                assert eng._dec_dtype == torch.float32
        with _env(LNVOX_S4_ENCODER_PRECISION="bnb4", LNVOX_S4_DECODE_DTYPE="auto"):
            eng = _make_lab_engine("mps")  # MPS can't honor either revert
            enc = next(r for r in _Rec.made if "use_bnb_4bit" in r.kw)
            assert enc.kw["use_bnb_4bit"] is False and enc.kw["gemma_root"] == "gemma-bf16"
            assert eng._dec_dtype == torch.float32
            eng = _make_lab_engine("cuda")
            enc = next(r for r in _Rec.made if "use_bnb_4bit" in r.kw)
            assert enc.kw["use_bnb_4bit"] is True
            assert eng._dec_dtype == torch.bfloat16
    # With a cap the long utterance is word-split under it on every device.
    with _env():
        p = PlanParams(**DramaboxClient.DEFAULT_PARAMS)
        texts = staged.chunk_texts(LONG_PROMPT, p, p.duration_multiplier, apply_chunk_policy(p, "cuda"))
        assert len(texts) == math.ceil(44.0 / 18.0) and all(t.startswith("(calm, adult) \"") for t in texts)


# ---- placement (_finalize) ----------------------------------------------------


def _tone(seconds: float, freq: float, lead_silence: float = 0.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    tone = 0.3 * np.sin(2 * np.pi * freq * t)
    audio = np.concatenate([np.zeros(int(SR * lead_silence)), tone])
    return np.stack([audio, audio], axis=1)


def _write(path: Path, audio: np.ndarray, age_s: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, SR, subtype="PCM_16")
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))


def _plan(keys_by_beat: dict[str, str]) -> StagedPlan:
    beats = [PlanBeat(beat_id=b, scene_id="ch01_s1", chapter_id="ch01", type="narration",
                      speaker="Narrator", cache_key=k) for b, k in keys_by_beat.items()]
    renders = [PlanRender(cache_key=k, prompt="p", cached=True)
               for k in dict.fromkeys(keys_by_beat.values())]
    params = PlanParams(**DramaboxClient.DEFAULT_PARAMS)
    params.cfg_scale = 1.0  # no negative-prompt ctx item in driver tests
    return StagedPlan(book_id="b", model_version="v", params=params,
                      renders=renders, beats=beats)


def _layout(tmp: str):
    book = Path(tmp) / "book"
    root = staged.staged_root(book)
    root.mkdir(parents=True)
    cache = Path(tmp) / "cache"
    cache.mkdir()
    return book, root, cache


def _same_audio(a: Path, b: Path) -> bool:
    return np.array_equal(sf.read(str(a))[0], sf.read(str(b))[0])


def test_placement_follows_the_manifest_key_on_revert_and_rerender():
    with tempfile.TemporaryDirectory() as tmp, _env():
        book, root, cache = _layout(tmp)
        placed = book / "05_audio" / "ch01" / "ch01_s1_b0000.wav"
        # K_old rendered long ago (casting A), K_new later (casting B). Same
        # length → same file size: only the mtime tells them apart.
        _write(cache / "kold.wav", _tone(1.0, 330), age_s=3600)
        _write(cache / "knew.wav", _tone(1.0, 550), age_s=60)

        staged._finalize(_plan({"ch01_s1_b0000": "knew"}), root, cache)
        assert _same_audio(placed, cache / "knew.wav")

        # Revert to casting A: its cache file is OLDER than the placed copy —
        # the old "refresh when the cache is newer" rule kept casting B's audio.
        staged._finalize(_plan({"ch01_s1_b0000": "kold"}), root, cache)
        assert _same_audio(placed, cache / "kold.wav"), "revert left the newer audio placed"
        manifest = ChapterAudio.model_validate_json((placed.parent / "manifest.json").read_text())
        assert manifest.beats[0].cache_key == "kold"
        assert abs(manifest.beats[0].duration_seconds - 1.0) < 0.01

        # F13: the SAME key re-rendered with new audio reaches 05_audio.
        _write(cache / "kold.wav", _tone(1.0, 440))
        staged._finalize(_plan({"ch01_s1_b0000": "kold"}), root, cache)
        assert _same_audio(placed, cache / "kold.wav"), "re-render didn't reach 05_audio"


def test_unchanged_rerun_copies_nothing_and_trim_stays_matched():
    with tempfile.TemporaryDirectory() as tmp, _env():
        book, root, cache = _layout(tmp)
        chapter = book / "05_audio" / "ch01"
        # Two beats share one render; the render has 1.5 s of leading silence.
        _write(cache / "k1.wav", _tone(1.0, 330, lead_silence=1.5), age_s=600)
        plan = _plan({"ch01_s1_b0000": "k1", "ch01_s1_b0001": "k1"})
        staged._finalize(plan, root, cache)
        a, b = chapter / "ch01_s1_b0000.wav", chapter / "ch01_s1_b0001.wav"
        assert sf.info(str(a)).duration < 1.3  # trimmed, and the cache upgraded
        assert _same_audio(a, cache / "k1.wav") and _same_audio(b, cache / "k1.wav")
        assert staged.pending_placements(plan, root, cache) == []

        before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in (a, b, cache / "k1.wav")}
        staged._finalize(plan, root, cache)
        after = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in (a, b, cache / "k1.wav")}
        assert before == after, "an unchanged re-run re-copied placed WAVs"


def test_legacy_placement_is_recopied_once():
    """A copy made the old way (plain shutil.copy → fresh mtime) is refreshed
    once, then stays put."""
    import shutil

    with tempfile.TemporaryDirectory() as tmp, _env():
        book, root, cache = _layout(tmp)
        placed = book / "05_audio" / "ch01" / "ch01_s1_b0000.wav"
        _write(cache / "k1.wav", _tone(1.0, 330), age_s=600)
        placed.parent.mkdir(parents=True)
        shutil.copy(cache / "k1.wav", placed)
        plan = _plan({"ch01_s1_b0000": "k1"})
        assert staged.pending_placements(plan, root, cache)  # not an exact copy yet
        staged._finalize(plan, root, cache)
        assert staged.pending_placements(plan, root, cache) == []


def test_stale_manifest_counts_as_pending():
    with tempfile.TemporaryDirectory() as tmp, _env():
        book, root, cache = _layout(tmp)
        _write(cache / "ka.wav", _tone(1.0, 330), age_s=600)
        _write(cache / "kb.wav", _tone(1.0, 550), age_s=300)
        staged._finalize(_plan({"ch01_s1_b0000": "ka"}), root, cache)
        plan_b = _plan({"ch01_s1_b0000": "kb"})
        pending = staged.pending_placements(plan_b, root, cache)
        assert pending == ["ch01/ch01_s1_b0000.wav", "ch01/manifest.json"], pending
        # Placement done but the manifest still names the old key → pending.
        staged._copy_exact(cache / "kb.wav", book / "05_audio" / "ch01" / "ch01_s1_b0000.wav")
        assert staged.pending_placements(plan_b, root, cache) == ["ch01/manifest.json"]
        assert staged.first_pending("decode", plan_b, root, cache) == "placement ch01/manifest.json"


# ---- driver: decode success requires the placement outputs --------------------


def test_driver_reruns_decode_that_died_inside_finalize():
    with tempfile.TemporaryDirectory() as tmp, _env():
        book, root, cache = _layout(tmp)
        plan = _plan({"ch01_s1_b0000": "k1", "ch01_s1_b0001": "k2"})
        for r in plan.renders:
            r.cached = False
        calls = []

        def fake_call(argv):
            phase = argv[4]
            calls.append(phase)
            assert phase == "decode", f"no {phase} work in this plan"
            if calls.count("decode") == 1:
                # Every cache WAV written… then the subprocess dies in
                # _finalize before any manifest (the old loop called it done).
                for i, r in enumerate(plan.renders):
                    _write(cache / f"{r.cache_key}.wav", _tone(1.0, 300 + 100 * i))
                return 1
            staged._finalize(staged.load_plan(root), root, cache)
            return 0

        saved = (staged_driver.build_plan, staged_driver.subprocess.call)
        staged_driver.build_plan = lambda *a, **k: plan
        staged_driver.subprocess.call = fake_call
        try:
            staged_driver.run_staged(
                book_id="b", chapters=[], casting=None, voicebank=None,
                voicebank_root=Path(tmp), book_dir=book, cache_dir=cache,
                device="cuda", progress=lambda _: None,
            )
        finally:
            staged_driver.build_plan, staged_driver.subprocess.call = saved

        assert calls == ["decode", "decode"], calls
        manifest = json.loads((book / "05_audio" / "ch01" / "manifest.json").read_text())
        assert [b["cache_key"] for b in manifest["beats"]] == ["k1", "k2"]
        assert not root.exists()  # success path cleaned the intermediates


def test_driver_aborts_when_finalize_never_completes():
    with tempfile.TemporaryDirectory() as tmp, _env(LNVOX_S4_STALL_LIMIT="3"):
        book, root, cache = _layout(tmp)
        plan = _plan({"ch01_s1_b0000": "k1"})
        _write(cache / "k1.wav", _tone(1.0, 300))
        calls = []

        saved = (staged_driver.build_plan, staged_driver.subprocess.call)
        staged_driver.build_plan = lambda *a, **k: plan
        staged_driver.subprocess.call = lambda argv: calls.append(argv[4]) or 1
        try:
            staged_driver.run_staged(
                book_id="b", chapters=[], casting=None, voicebank=None,
                voicebank_root=Path(tmp), book_dir=book, cache_dir=cache,
                device="cuda", progress=lambda _: None,
            )
        except RuntimeError as e:
            assert "placement ch01/" in str(e), e
        else:
            raise AssertionError("a decode phase that never finalizes must not pass")
        finally:
            staged_driver.build_plan, staged_driver.subprocess.call = saved
            os.environ.pop("LNVOX_S4_STALL_LIMIT", None)
        assert calls == ["decode"] * 3, calls  # 3 attempts without progress


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

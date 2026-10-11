"""Tests for the shell launcher, s4_retry.sh and the Studio's casting edit.

The launcher and the retry loop run for real, from a sandbox directory, with
a fake `uv` first on PATH: it records every call (argv + the LNVOX_LLM_* env
it was handed) and exits 0 — except where a test scripts it: the exit-code
sequence of `uv run lnvox s4` (FAKE_UV_S4_CODES) or the exit of any other
stage (FAKE_UV_EXIT_<stage>). No model, venv sync or GPU is ever touched. The Voicebank
Studio is imported with PySide6 stubbed out when it isn't installed.

Runnable directly (`python tests/test_launcher_scripts.py`) or under pytest.
"""

import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(REPO / "src"))

BOOK = "series/vol-01"
WATCHED_ENV = (
    "LNVOX_LLM__ENDPOINT",
    "LNVOX_LLM_MODEL",
    "LNVOX_LLM__MODEL",
    "LNVOX_LLM_MAX_LEN",
    "LNVOX_LLM_MAX_MODEL_LEN",
    "LNVOX_LLM__MAX_MODEL_LEN",
    "LNVOX_TTS_BACKEND",
)

FAKE_UV = f"""#!{Path(sys.executable).as_posix()}
import json, os, sys
args = sys.argv[1:]
watched = {list(WATCHED_ENV)!r}
with open(os.environ["FAKE_UV_LOG"], "a") as f:
    f.write(json.dumps({{"args": args, "env": {{k: os.environ.get(k) for k in watched}}}}) + "\\n")
if args[:3] == ["run", "lnvox", "s4"]:
    state = os.environ["FAKE_UV_STATE"]
    n = int(open(state).read()) if os.path.exists(state) else 0
    open(state, "w").write(str(n + 1))
    codes = [int(c) for c in os.environ.get("FAKE_UV_S4_CODES", "0").split(",")]
    code = codes[min(n, len(codes) - 1)]
    print(f"fake lnvox s4 attempt {{n + 1}} -> exit {{code}}")
    sys.exit(code)
if args[:2] == ["run", "lnvox"] and len(args) > 2:
    sys.exit(int(os.environ.get("FAKE_UV_EXIT_" + args[2].replace("-", "_"), "0")))
"""


class Sandbox:
    """A throwaway cwd with the real scripts/ and a fake `uv` on PATH."""

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        try:
            (self.root / "scripts").symlink_to(SCRIPTS)
        except OSError:  # Windows without Developer Mode can't symlink
            shutil.copytree(SCRIPTS, self.root / "scripts")
        bindir = self.root / "bin"
        bindir.mkdir()
        uv = bindir / "uv"
        uv.write_text(FAKE_UV)
        uv.chmod(0o755)
        # Only needs to exist: prepare_llm_env checks it's on PATH, and the
        # tests that reach start_vllm serve /v1/models themselves.
        llama = bindir / "llama-server"
        llama.write_text("#!/bin/sh\nexit 0\n")
        llama.chmod(0o755)
        self.log = self.root / "uv_calls.jsonl"
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("LNVOX_") and k not in ("MAX_RETRIES", "STEP_RETRIES", "MAX_ATTEMPTS")
        }
        self.env.update(
            PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            FAKE_UV_LOG=str(self.log),
            FAKE_UV_STATE=str(self.root / "s4_attempts"),
            LNVOX_CAFFEINATED="1",  # no caffeinate re-exec on macOS
            RETRY_DELAY="0",
        )

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", f"scripts/{script}", *args],
            cwd=self.root, env={**self.env, **env},
            capture_output=True, text=True, timeout=120,
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def lnvox(self, stage: str) -> list[dict]:
        return [c for c in self.calls() if c["args"][:3] == ["run", "lnvox", stage]]

    def touch(self, rel: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# ----------------------------------------------------------- run_pipeline.sh


def test_help_and_serve_scripts_name_the_real_model_defaults():
    launcher = (SCRIPTS / "run_pipeline.sh").read_text(encoding="utf-8")
    defaults = dict(re.findall(r'^\s+(llama|vllm|mlx)\)\s+LLM_MODEL="([^"]+)"', launcher, re.M))
    assert set(defaults) == {"llama", "vllm", "mlx"}, defaults

    with tempfile.TemporaryDirectory() as tmp:
        help_text = Sandbox(tmp).run("run_pipeline.sh", "--help").stdout
    for model in defaults.values():
        assert model in help_text, model
    assert "LNVOX_LLM_MODEL" in help_text and "LNVOX_ARTIFACTS_DIR" in help_text

    for backend, model in defaults.items():
        serve = (SCRIPTS / f"serve_{backend}.sh").read_text(encoding="utf-8")
        code_default = re.search(r'MODEL="\$\{LNVOX_LLM_MODEL:-([^}]+)\}"', serve).group(1)
        assert code_default == model, (backend, code_default, model)
        header = "".join(
            line for line in serve.splitlines(keepends=True)[1:40] if line.startswith("#")
        ).split("set -")[0]
        assert model in header, f"serve_{backend}.sh header doesn't name its default {model}"
    # The stale-header bug: serve_llama.sh documented E4B as its default.
    assert "LNVOX_LLM_MODEL=google/gemma-4-12B-it-qat-q4_0-gguf" in (SCRIPTS / "serve_llama.sh").read_text(encoding="utf-8")

    # The ComfyUI pack mirrors these defaults ("keep in sync").
    runtime = (REPO / "comfyui" / "runtime.py").read_text(encoding="utf-8")
    for backend, model in defaults.items():
        assert f'"{backend}": "{model}"' in runtime, backend


def test_mix_and_sync_share_novels_root_and_artifacts_dir():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        sb.touch(f"epubs/{BOOK}.epub")
        (sb.root / "arts" / BOOK / "05_audio").mkdir(parents=True)
        r = sb.run(
            "run_pipeline.sh", BOOK, "--skip-llm", "--skip-tts",
            "--novels-root", "myroot", "--book-title", "Vol/1: A\\B",
            LNVOX_ARTIFACTS_DIR="arts",
        )
        assert r.returncode == 0, r.stderr
        (s5,) = sb.lnvox("s5")
        assert _after(s5["args"], "--novels-root") == "myroot", s5["args"]
        assert _after(s5["args"], "--title") == "Vol/1: A\\B"
        # s6 ran at all only because the 05_audio pre-check looked under
        # $LNVOX_ARTIFACTS_DIR — the same root the CLI writes to.
        (s6,) = sb.lnvox("s6")
        assert _after(s6["args"], "--novels-root") == "myroot", s6["args"]
        # The name s5_mix.mix really writes ('/' and '\' → '_'; on Windows
        # also ':' and the other NTFS-forbidden characters).
        from lnvox.stages.s5_mix import safe_filename

        expected = "Vol_1_ A_B" if sys.platform == "win32" else "Vol_1: A_B"
        assert safe_filename("Vol/1: A\\B") == expected
        assert f"Final output should be at: arts/{BOOK}/06_final/{expected}.m4b" in r.stdout, r.stdout


def test_default_title_prints_the_sanitised_m4b_path():
    with tempfile.TemporaryDirectory() as tmp:
        r = Sandbox(tmp).run("run_pipeline.sh", BOOK, "--skip-llm", "--skip-tts", "--skip-mix", "--skip-sync")
        assert r.returncode == 0, r.stderr
        assert f"Final output should be at: artifacts/{BOOK}/06_final/series_vol-01.m4b" in r.stdout


def _stage_env(sb: Sandbox, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
    """Run to s5 only (no server, no TTS) and return the env s5 was handed."""
    r = sb.run("run_pipeline.sh", BOOK, "--skip-llm", "--skip-tts", "--skip-sync", *args, **env)
    assert r.returncode == 0, r.stderr
    (s5,) = sb.lnvox("s5")
    return r, s5["env"]


def test_llm_endpoint_resolution_is_exported_to_the_stages():
    from lnvox.config import Settings

    cases = [
        ({}, (), "http://localhost:8000/v1"),
        ({"LNVOX_LLM_PORT": "8123"}, (), "http://localhost:8123/v1"),
        ({"LNVOX_LLM_ENDPOINT": "http://short:1/v1", "LNVOX_LLM_PORT": "8123"}, (), "http://short:1/v1"),
        ({"LNVOX_LLM__ENDPOINT": "http://nested:2/v1", "LNVOX_LLM_ENDPOINT": "http://short:1/v1"}, (), "http://nested:2/v1"),
        ({"LNVOX_LLM__ENDPOINT": "http://nested:2/v1"}, ("--vllm-url", "http://flag:3/v1"), "http://flag:3/v1"),
    ]
    for env, args, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            _, seen = _stage_env(Sandbox(tmp), *args, **env)
        assert seen["LNVOX_LLM__ENDPOINT"] == expected, (env, args, seen)
        # And the python client, handed that environment (plus whatever the
        # user had set), resolves the same URL the probe polled.
        stage_env = {**env, **{k: v for k, v in seen.items() if v is not None}}
        saved = dict(os.environ)
        try:
            os.environ.update(stage_env)
            assert Settings(_env_file=None).llm.endpoint == expected, (env, args)
        finally:
            os.environ.clear()
            os.environ.update(saved)


def test_llm_model_and_context_length_env():
    with tempfile.TemporaryDirectory() as tmp:
        r, seen = _stage_env(Sandbox(tmp), LNVOX_LLM_MODEL="my/model")
        assert "Using LLM model: my/model" in r.stdout
        assert seen["LNVOX_LLM_MODEL"] == seen["LNVOX_LLM__MODEL"] == "my/model", seen
    with tempfile.TemporaryDirectory() as tmp:
        _, seen = _stage_env(Sandbox(tmp), "--llm-model", "flag/model", LNVOX_LLM_MODEL="my/model")
        assert seen["LNVOX_LLM_MODEL"] == seen["LNVOX_LLM__MODEL"] == "flag/model", seen
    with tempfile.TemporaryDirectory() as tmp:
        _, seen = _stage_env(Sandbox(tmp), "--llm-backend", "mlx")
        assert seen["LNVOX_LLM_MODEL"] == "mlx-community/gemma-4-12B-it-qat-4bit", seen
    with tempfile.TemporaryDirectory() as tmp:
        _, seen = _stage_env(Sandbox(tmp), "--max-model-len", "4096")
        assert seen["LNVOX_LLM_MAX_LEN"] == "4096", seen
        assert seen["LNVOX_LLM_MAX_MODEL_LEN"] == seen["LNVOX_LLM__MAX_MODEL_LEN"] == "4096", seen
    with tempfile.TemporaryDirectory() as tmp:
        r = Sandbox(tmp).run("run_pipeline.sh", BOOK, "--skip-llm", "--max-model-len", "32k")
        assert r.returncode == 2 and "--max-model-len must be a positive integer" in r.stderr, r.stderr


def test_vllm_big_model_default_window_reaches_the_client():
    # serve_vllm.sh's own default for 31B/27B names — the launcher mirrors it.
    serve = (SCRIPTS / "serve_vllm.sh").read_text(encoding="utf-8")
    assert re.search(r'\*31B\* \|\| "\$MODEL" == \*27B\* \]\]; then\n(?:\s*#.*\n)*\s*DEFAULT_MAX_LEN=20480', serve)

    big = ("--llm-backend", "vllm", "--llm-model", "nvidia/Gemma-4-31B-IT-NVFP4")
    with tempfile.TemporaryDirectory() as tmp:
        _, seen = _stage_env(Sandbox(tmp), *big)
        assert seen["LNVOX_LLM_MAX_LEN"] == seen["LNVOX_LLM_MAX_MODEL_LEN"] == "20480", seen
    with tempfile.TemporaryDirectory() as tmp:  # small model: serve_vllm.sh's 65536 = the client's
        _, seen = _stage_env(Sandbox(tmp), "--llm-backend", "vllm")
        assert seen["LNVOX_LLM_MAX_MODEL_LEN"] is None, seen
    with tempfile.TemporaryDirectory() as tmp:  # a user-set window is left alone
        _, seen = _stage_env(Sandbox(tmp), *big, LNVOX_LLM_MAX_MODEL_LEN="16384")
        assert seen["LNVOX_LLM_MAX_MODEL_LEN"] == "16384" and seen["LNVOX_LLM_MAX_LEN"] is None, seen
    with tempfile.TemporaryDirectory() as tmp:  # external server: window unknown
        _, seen = _stage_env(Sandbox(tmp), *big, "--vllm-url", "http://gpu-box:8000/v1")
        assert seen["LNVOX_LLM_MAX_MODEL_LEN"] is None, seen


class _ModelsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def log_message(self, *_a):
        pass


def test_readiness_probe_follows_llm_port():
    # A server already answering on a custom LNVOX_LLM_PORT must be found by
    # the probe (it used to poll :8000 only and time out after 600 s) and
    # handed to every stage.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), _ModelsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sb = Sandbox(tmp)
            (sb.root / "novels" / BOOK).mkdir(parents=True)
            r = sb.run(
                "run_pipeline.sh", BOOK, "--audit-cast", "off",
                "--skip-tts", "--skip-mix", "--skip-sync",
                LNVOX_LLM_PORT=str(port),
            )
            assert r.returncode == 0, r.stderr + r.stdout
            url = f"http://localhost:{port}/v1"
            assert f"already running at {url}" in r.stdout, r.stdout
            stages = [c for c in sb.calls() if c["args"][:2] == ["run", "lnvox"]]
            assert [c["args"][2] for c in stages] == ["ingest", "s1", "s2", "voice", "s3"], stages
            assert all(c["env"]["LNVOX_LLM__ENDPOINT"] == url for c in stages)
            assert "cross-volume merge" not in r.stdout
            # s3's caches are casting-aware; --regen-profiles would force every
            # descriptor (and so a whole-book re-render) on every run.
            assert stages[-1]["args"] == ["run", "lnvox", "s3", BOOK], stages[-1]

            # The hand-casting hint repeats only the env this run really had:
            # a spelled-out default LNVOX_VOICEBANK=voicebank would override .env.
            r = sb.run(
                "run_pipeline.sh", BOOK, "--audit-cast", "off", "--stop-after-audit",
                LNVOX_LLM_PORT=str(port), LNVOX_ARTIFACTS_DIR="arts",
            )
            assert r.returncode == 0, r.stderr + r.stdout
            assert "  LNVOX_ARTIFACTS_DIR=arts uv run python scripts/voicebank_studio.py" in r.stdout, r.stdout
            assert f"--casting arts/{BOOK}/04_voice_assignments.json" in r.stdout
            assert "LNVOX_VOICEBANK=" not in r.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_scenario_mode_reuses_only_a_complete_ingest():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        skip = ("--mode", "scenario", "--skip-llm", "--skip-tts", "--skip-mix", "--skip-sync")
        env = {"LNVOX_ARTIFACTS_DIR": "arts"}

        r = sb.run("run_pipeline.sh", "play", *skip, **env)
        assert r.returncode == 2 and "needs --scenario-file" in r.stderr, r.stderr

        # A crash in ingest-scenario's characters pass: script, no cast.
        sb.touch("arts/play/00_script.json")
        r = sb.run("run_pipeline.sh", "play", *skip, **env)
        assert r.returncode == 2 and "incomplete" in r.stderr and "01_characters.json" in r.stderr, r.stderr

        sb.touch("arts/play/01_characters.json")
        r = sb.run("run_pipeline.sh", "play", *skip, **env)
        assert r.returncode == 0, r.stderr
        # …looked up under $LNVOX_ARTIFACTS_DIR, not a literal artifacts/.
        r = sb.run("run_pipeline.sh", "play", *skip)
        assert r.returncode == 2, r.stderr


def test_launcher_stops_when_s4_retry_fails():
    tts_only = ("--skip-llm", "--skip-sync", "--vllm-url", "http://127.0.0.1:9/v1")  # --vllm-url: no 5 s VRAM sleep
    # Monolithic Dramabox: the budget runs out → no s5 on a partial render.
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("run_pipeline.sh", BOOK, *tts_only, "--max-retries", "2",
                   LNVOX_S4_STAGED="0", FAKE_UV_S4_CODES="1")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "s4 failed (s4_retry.sh exit 1)" in r.stderr, r.stderr
        assert len(sb.lnvox("s4")) == 2 and not sb.lnvox("s5")
    # VibeVoice: a deterministic refusal (exit 2) stops after one attempt.
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        (sb.root / "external" / "VibeVoice").mkdir(parents=True)
        r = sb.run("run_pipeline.sh", BOOK, *tts_only, "--tts-backend", "vibevoice",
                   FAKE_UV_S4_CODES="2")
        assert r.returncode == 2, r.stdout + r.stderr
        (s4,) = sb.lnvox("s4")
        assert s4["env"]["LNVOX_TTS_BACKEND"] == "vibevoice"
        assert not sb.lnvox("s5")
    # A retry that eventually succeeds still mixes.
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("run_pipeline.sh", BOOK, *tts_only, "--max-retries", "3",
                   LNVOX_S4_STAGED="0", FAKE_UV_S4_CODES="1,0")
        assert r.returncode == 0, r.stdout + r.stderr
        assert len(sb.lnvox("s4")) == 2 and len(sb.lnvox("s5")) == 1


def test_run_step_does_not_retry_deterministic_refusals():
    after_tts = ("--skip-llm", "--skip-tts", "--skip-sync")
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("run_pipeline.sh", BOOK, *after_tts, FAKE_UV_EXIT_s5="2")
        assert r.returncode == 2 and "not retrying" in r.stderr, r.stderr
        assert len(sb.lnvox("s5")) == 1
    with tempfile.TemporaryDirectory() as tmp:  # an ordinary failure still uses the budget
        sb = Sandbox(tmp)
        r = sb.run("run_pipeline.sh", BOOK, *after_tts, FAKE_UV_EXIT_s5="1")
        assert r.returncode == 1 and "failed after 3 attempt(s)" in r.stderr, r.stderr
        assert len(sb.lnvox("s5")) == 3
    with tempfile.TemporaryDirectory() as tmp:  # staged s4 passes a refusal through as 2
        sb = Sandbox(tmp)
        r = sb.run("run_pipeline.sh", BOOK, "--skip-llm", "--skip-sync", "--vllm-url", "http://127.0.0.1:9/v1",
                   FAKE_UV_S4_CODES="2")
        assert r.returncode == 2 and "staged s4 refused its input" in r.stderr, r.stderr
        (s4,) = sb.lnvox("s4")
        assert "--staged" in s4["args"] and not sb.lnvox("s5")


# ------------------------------------------------------------- s4_retry.sh


def test_s4_retry_requires_a_book():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("s4_retry.sh")
        assert r.returncode == 2 and "Usage" in r.stderr, r.stderr
        assert not sb.calls()


def test_s4_retry_reports_real_exit_codes_and_honours_artifacts_dir():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("s4_retry.sh", BOOK, FAKE_UV_S4_CODES="1,0", LNVOX_ARTIFACTS_DIR="arts")
        assert r.returncode == 0, r.stdout
        log = (sb.root / "arts" / BOOK / "_s4_full.log").read_text(encoding="utf-8")
        # This branch was unreachable under pipefail before.
        assert "Exit 1 on attempt 1; retrying in 0s" in log, log
        assert "Completed on attempt 2" in log
        assert "==================== Attempt 2 @ " in log
        assert not (sb.root / "artifacts").exists()


def test_s4_retry_does_not_retry_deterministic_refusals():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("s4_retry.sh", BOOK, FAKE_UV_S4_CODES="2", MAX_ATTEMPTS="5")
        assert r.returncode == 2, r.stdout
        assert len(sb.lnvox("s4")) == 1
        assert "deterministic refusal, not retrying" in (sb.root / "artifacts" / BOOK / "_s4_full.log").read_text(encoding="utf-8")


def test_s4_retry_retries_crashes_until_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        sb = Sandbox(tmp)
        r = sb.run("s4_retry.sh", BOOK, FAKE_UV_S4_CODES="137,139,1", MAX_ATTEMPTS="3")
        assert r.returncode == 1, r.stdout
        assert len(sb.lnvox("s4")) == 3
        log = (sb.root / "artifacts" / BOOK / "_s4_full.log").read_text(encoding="utf-8")
        for line in ("Crashed on attempt 1;", "Crashed on attempt 2;", "Exit 1 on attempt 3;",
                     "Gave up after 3 attempts"):
            assert line in log, (line, log)


# ------------------------------------------------------ voicebank_studio.py


_STUDIO = None


def _load_studio():
    """Import scripts/voicebank_studio.py, stubbing PySide6 if it's absent.

    Only module-level code runs at import (class bodies declare Signals), so
    a stub that accepts any attribute and call is enough — no GUI is built.
    """
    global _STUDIO
    if _STUDIO is not None:
        return _STUDIO
    if "PySide6" not in sys.modules and importlib.util.find_spec("PySide6") is None:
        class _Stub:
            def __init__(self, *_a, **_k):
                pass

            def __getattr__(self, _name):
                return _Stub()

            def __call__(self, *_a, **_k):
                return _Stub()

        def _stub_attr(attr):
            # Dunders stay missing: inspect (and numba, under librosa) walk
            # sys.modules probing __file__ & co., and a class there breaks
            # them in every later test.
            if attr.startswith("__"):
                raise AttributeError(attr)
            return type(attr, (_Stub,), {})

        for name in ("PySide6", "PySide6.QtCore", "PySide6.QtMultimedia", "PySide6.QtWidgets"):
            mod = types.ModuleType(name)
            mod.__getattr__ = _stub_attr
            sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("voicebank_studio", SCRIPTS / "voicebank_studio.py")
    studio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(studio)
    _STUDIO = studio
    return studio


def test_studio_recast_refreshes_a_stale_voice_descriptor():
    studio = _load_studio()
    from lnvox.voices.matcher import descriptor_from_clip
    from lnvox.voices.schema import CharacterCasting, VoiceClip, VoiceTarget

    def clip(cid, gender, age="adult", accent="us"):
        return VoiceClip(id=cid, source="manual", clip_path=f"clips/{cid}.wav",
                         duration_seconds=10.0, gender=gender, age_band=age, accent=accent)

    def cast(name, clip_id, descriptor):
        return CharacterCasting(character_name=name, target=VoiceTarget(gender="male", age_band="adult"),
                                assigned_clip_id=clip_id, voice_descriptor=descriptor)

    old, new = clip("cv_old", "female", accent="england"), clip("cv_new", "male", "elder")

    # Narrator: a clip-derived descriptor — re-derived for the new clip. Every
    # hand edit is locked, so the launcher's next `voice cast` keeps it.
    narrator = cast("Narrator", old.id, descriptor_from_clip(old))
    note = studio.recast(narrator, new.id, new)
    assert narrator.assigned_clip_id == new.id and narrator.locked
    assert narrator.voice_descriptor == descriptor_from_clip(new), narrator.voice_descriptor
    assert "re-derived" in note and "locked" in note

    # A voice reused from a prior volume: cleared, so s3 composes a fresh one.
    hero = cast("Hero", old.id, "adult, English, female, bright and quick")
    note = studio.recast(hero, new.id, new)
    assert (hero.assigned_clip_id, hero.voice_descriptor) == (new.id, "")
    assert "cleared" in note

    # Narrator re-cast to a clip missing from the bank: nothing to derive from.
    narrator = cast("Narrator", old.id, descriptor_from_clip(old))
    studio.recast(narrator, "cv_elsewhere", None)
    assert (narrator.assigned_clip_id, narrator.voice_descriptor) == ("cv_elsewhere", "")

    # No descriptor to go stale: only the lock. No actual change: untouched.
    plain = cast("Sidekick", old.id, "")
    note = studio.recast(plain, new.id, new)
    assert plain.voice_descriptor == "" and plain.locked and "descriptor" not in note
    same = cast("Narrator", old.id, "kept as is")
    assert studio.recast(same, old.id, old) == ""
    assert same.voice_descriptor == "kept as is" and not same.locked


def test_studio_paths_follow_the_cli_settings():
    """--voicebank / --artifacts defaults: env var, else `.env` (both through
    lnvox's Settings, like the CLI), else the repo's own directories."""
    studio = _load_studio()
    saved_env, saved_cwd = dict(os.environ), os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.chdir(tmp)  # .env is read from the CWD, as the CLI reads it
            for k in list(os.environ):
                if k.startswith("LNVOX_"):
                    del os.environ[k]
            assert studio.default_voicebank_dir() == REPO / "voicebank"
            assert studio.default_artifacts_dir() == REPO / "artifacts"

            Path(".env").write_text("LNVOX_VOICEBANK=vb-fr\nLNVOX_ARTIFACTS_DIR=/data/arts\n")
            assert studio.default_voicebank_dir() == Path("vb-fr")
            assert studio.default_artifacts_dir() == Path("/data/arts")

            os.environ["LNVOX_VOICEBANK"] = "vb-demo"  # env beats .env
            assert studio.default_voicebank_dir() == Path("vb-demo")
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

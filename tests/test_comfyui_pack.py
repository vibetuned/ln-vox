"""Tests for the ComfyUI node pack in comfyui/ (no ComfyUI or torch needed).

Covers the parts that can break silently: the launcher-equivalent process
plumbing (retries, signal handling, LLM server lifecycle against a fake
OpenAI-style server), the hand-casting edit, and the example workflows
staying in step with the nodes' inputs.

Runnable directly (`python tests/test_comfyui_pack.py`) or under pytest.
"""

import importlib.util
import json
import socket
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACK = REPO / "comfyui"


def _load(name: str, path: Path, package: bool = False):
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pack = _load("comfyui_lnvox", PACK / "__init__.py", package=True)
rt = sys.modules["comfyui_lnvox.runtime"]
nodes = sys.modules["comfyui_lnvox.nodes"]
workflows = _load("lnvox_build_workflows", PACK / "tools" / "build_workflows.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAKE_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import http.server, json, os
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": os.environ.get("LNVOX_LLM_MODEL", "?")}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", int(os.environ["LNVOX_LLM_PORT"])), H).serve_forever()
    """
)


def _fake_repo(tmp: Path) -> Path:
    """Just enough of a checkout for find_repo, plus a fake serve script."""
    (tmp / "src" / "lnvox").mkdir(parents=True)
    scripts = tmp / "scripts"
    scripts.mkdir()
    (scripts / "run_pipeline.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    serve = scripts / "serve_llama.sh"
    serve.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {scripts / 'fake_server.py'}\n")
    (scripts / "fake_server.py").write_text(FAKE_SERVER)
    for p in (scripts / "run_pipeline.sh", serve):
        p.chmod(0o755)
    return tmp


def _book(repo: Path, **llm) -> dict:
    return {
        "repo": str(repo), "book_id": "series/volume-01", "mode": "narration", "title": "T",
        "novels_root": "novels", "voicebank": "voicebank", "epub": "", "scenario_file": "",
        "artifacts_dir": "artifacts", "llm": llm, "tts_backend": "", "mix": {}, "env": {},
    }


def test_find_repo_resolves_the_checkout_holding_the_pack():
    assert rt.find_repo() == REPO


def test_derive_never_mutates_its_input():
    book = _book(REPO)
    out = rt.derive(book, mix={"intra": 0.5}, title="X")
    assert book["mix"] == {} and book["title"] == "T"
    assert out["mix"] == {"intra": 0.5} and out["title"] == "X"


def test_run_step_retries_until_success():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        counter = repo / "count"
        # Fails twice, then succeeds — a resumable stage hitting a flaky server.
        cmd = ["bash", "-c", f'n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}; [ "$n" -ge 2 ]']
        hooks = []
        rt.run_step(cmd, book=_book(repo), desc="flaky", retries=3, retry_delay=0,
                    before_retry=lambda: hooks.append(1))
        assert counter.read_text().strip() == "3"
        assert len(hooks) == 2  # the pre-retry hook ran before each retry


def test_run_step_gives_up_after_budget():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        try:
            rt.run_step(["bash", "-c", "echo boom; exit 3"], book=_book(repo), desc="always-fails",
                        retries=2, retry_delay=0)
        except rt.LnVoxError as e:
            assert "failed after 2 attempt(s) (exit 3)" in str(e) and "boom" in str(e)
        else:
            raise AssertionError("expected LnVoxError")


def test_run_step_does_not_retry_a_signal_death():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        counter = repo / "count"
        cmd = ["bash", "-c", f"echo x >> {counter}; kill -TERM $$"]
        try:
            rt.run_step(cmd, book=_book(repo), desc="killed", retries=5, retry_delay=0)
        except rt.LnVoxError as e:
            assert "signal" in str(e)
        else:
            raise AssertionError("expected LnVoxError")
        assert len(counter.read_text().splitlines()) == 1


def test_run_step_does_not_retry_a_deterministic_refusal():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        counter = repo / "count"
        cmd = ["bash", "-c", f"echo x >> {counter}; echo 'bad knob' >&2; exit 2"]
        try:
            rt.run_step(cmd, book=_book(repo), desc="refused", retries=5, retry_delay=0)
        except rt.LnVoxError as e:
            assert "refused (exit 2)" in str(e) and "bad knob" in str(e)
        else:
            raise AssertionError("expected LnVoxError")
        assert len(counter.read_text().splitlines()) == 1


def test_book_env_exports_what_the_launcher_exports():
    env = rt.book_env(_book(REPO, backend="llama", model="m", endpoint="http://127.0.0.1:9/v1", max_model_len=32768))
    assert env["LNVOX_LLM_BACKEND"] == "llama"
    assert env["LNVOX_LLM_MODEL"] == "m"
    assert env["LNVOX_LLM__ENDPOINT"] == "http://127.0.0.1:9/v1"
    # Both names: serve scripts read _MAX_LEN, the python client _MAX_MODEL_LEN.
    assert env["LNVOX_LLM_MAX_LEN"] == env["LNVOX_LLM_MAX_MODEL_LEN"] == "32768"
    assert env["LNVOX_VOICEBANK"] == "voicebank"
    assert "VIRTUAL_ENV" not in env


def test_llm_server_lifecycle_start_stop_relaunch():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        endpoint = f"http://127.0.0.1:{_free_port()}/v1"
        book = _book(repo, backend="llama", model="fake-model", endpoint=endpoint,
                     external=False, prepare_env=False)
        try:
            rt.ensure_server(book)
            assert rt.server_ready(endpoint)
            assert rt.served_models(endpoint) == ["fake-model"]
            assert (repo / "logs" / "comfyui_llm_llama.log").exists()

            assert rt.stop_server(book).startswith("Stopped")
            assert not rt.server_ready(endpoint)

            # The launcher's ensure_vllm: a later LLM stage relaunches it.
            rt.ensure_server(book)
            assert rt.server_ready(endpoint)
        finally:
            rt.stop_server(book)


def test_external_server_is_checked_not_launched():
    book = _book(REPO, backend="llama", endpoint=f"http://127.0.0.1:{_free_port()}/v1", external=True)
    try:
        rt.ensure_server(book)
    except rt.LnVoxError as e:
        assert "not responding" in str(e)
    else:
        raise AssertionError("expected LnVoxError")
    assert rt.stop_server(book).startswith("External")


def test_set_voice_edits_casting_and_clears_stale_descriptor():
    with tempfile.TemporaryDirectory() as d:
        repo = _fake_repo(Path(d))
        book = _book(repo)
        (repo / "voicebank").mkdir()
        (repo / "voicebank" / "manifest.json").write_text(json.dumps({"clips": [
            {"id": "cv_a", "clip_path": "clips/cv_a.wav", "gender": "female", "age_band": "adult", "duration_seconds": 12},
            {"id": "cv_b", "clip_path": "clips/cv_b.wav", "gender": "male", "age_band": "elder", "duration_seconds": 12},
        ]}))
        bdir = rt.book_dir(book)
        bdir.mkdir(parents=True)
        casting = bdir / "04_voice_assignments.json"
        casting.write_text(json.dumps({"book_id": book["book_id"], "castings": [
            {"character_name": "Narrator", "assigned_clip_id": "cv_a", "voice_descriptor": "adult woman", "locked": False},
            {"character_name": "Old Man", "assigned_clip_id": "", "locked": False},
        ]}))
        out = nodes.LnVoxSetVoice().run(book, "narrator = cv_b\nOld Man=cv_a  # comment", lock=True, note="n")
        data = json.loads(casting.read_text())
        narrator, old = data["castings"]
        assert narrator["assigned_clip_id"] == "cv_b" and narrator["locked"] and narrator["voice_descriptor"] == ""
        assert old["assigned_clip_id"] == "cv_a" and old["audit_note"] == "n"
        assert "Narrator: cv_a → cv_b (locked)" in out["ui"]["text"][0]

        for bad in ("Nobody = cv_a", "Narrator = cv_missing", "no equals sign"):
            try:
                nodes.LnVoxSetVoice().run(book, bad)
            except rt.LnVoxError:
                pass
            else:
                raise AssertionError(f"expected LnVoxError for {bad!r}")


def test_set_voice_rederives_the_narrator_descriptor_like_the_studio():
    import shutil

    if not shutil.which("uv"):
        return  # needs the real ln-vox venv
    clip = {"id": "cv_z", "source": "manual", "clip_path": "clips/cv_z.wav", "duration_seconds": 12.0,
            "gender": "male", "age_band": "elder", "accent": "scotland"}
    derived = nodes._narrator_descriptor(_book(REPO), clip)
    assert derived.startswith("elder, ") and " male, " in derived, derived
    assert nodes._narrator_descriptor(_book(REPO), None) == ""


def test_nodes_refuse_the_wrong_mode():
    book = dict(_book(REPO), mode="lecture")
    try:
        nodes.LnVoxCharacters().run(book)
    except rt.LnVoxError as e:
        assert "narration" in str(e)
    else:
        raise AssertionError("expected LnVoxError")


def test_every_node_is_registered_with_a_display_name_and_category():
    assert set(pack.NODE_CLASS_MAPPINGS) == set(pack.NODE_DISPLAY_NAME_MAPPINGS)
    for name, cls in pack.NODE_CLASS_MAPPINGS.items():
        assert cls.CATEGORY.startswith("ln-vox"), name  # web/lnvox.js keys on this
        assert len(cls.RETURN_TYPES) == len(cls.RETURN_NAMES), name
        assert callable(getattr(cls, cls.FUNCTION)), name


def test_example_workflows_match_node_inputs_and_are_up_to_date():
    for name, build in workflows.WORKFLOWS.items():
        graph = build()
        prompt = graph.api()
        for nid, node in prompt.items():
            cls = pack.NODE_CLASS_MAPPINGS.get(node["class_type"])
            if cls is None:
                continue  # core node (PreviewAudio)
            spec = cls.INPUT_TYPES()
            declared = {**spec.get("required", {}), **spec.get("optional", {})}
            assert set(node["inputs"]) <= set(declared), (name, node["class_type"])
            for req in spec.get("required", {}):
                assert req in node["inputs"], (name, node["class_type"], req)
            for value in node["inputs"].values():
                if isinstance(value, list):
                    assert value[0] in prompt, (name, nid, value)
        on_disk = json.loads((PACK / "example_workflows" / f"{name}.json").read_text(encoding="utf-8"))
        assert on_disk == json.loads(json.dumps(graph.ui())), f"{name}: run comfyui/tools/build_workflows.py"


if __name__ == "__main__":
    failed = 0
    for fn_name, fn in sorted(globals().items()):
        if fn_name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {fn_name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn_name}: {e!r}")
    sys.exit(1 if failed else 0)

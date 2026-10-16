"""`lnvox remote-tts` — the box's agent, the file mirror, the decisions.

No box needed: the agent's functions run on temp folders, it runs once the
way the box runs it (source on stdin), the tar transfer goes through a fake
`ssh` that runs the command locally, and the decision flow drives a fake
Remote. Runnable directly (`python tests/test_remote.py`) or under pytest.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox import remote, remote_agent as agent


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _book(repo: Path, book: str = "series/vol-01") -> Path:
    d = repo / "artifacts" / book
    beats = [{"text": f"Beat {i}.", "source_span": f"Beat {i}."} for i in range(3)]
    _write(d / "03_directed" / "01.json", {"chapter_id": "01", "scenes": [{"scene_id": "01_s1", "beats": beats}]})
    _write(d / "03_directed" / "02.json", {"chapter_id": "02", "scenes": [{"scene_id": "02_s1", "beats": beats[:2]}]})
    _write(d / "04_voice_assignments.json", {"castings": [{"character_name": "Narrator", "assigned_clip_id": "v1"}]})
    _write(d / "01_characters.json", {"characters": []})
    _write(d / "00_book_meta.json", {"title": "The Lantern Keepers"})
    return d


# ----- the agent ----------------------------------------------------------------


def test_progress_reads_the_staged_phases_from_disk():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        d = _book(repo)
        root = d / "05_audio" / "_staged"
        plan = {"renders": [
            {"cache_key": "aaa", "cached": True, "chunks": [{"item_id": "a1"}]},
            {"cache_key": "bbb", "chunks": [{"item_id": "b1"}, {"item_id": "b2"}]},
            {"cache_key": "ccc", "chunks": [{"item_id": "c1"}]},
        ]}
        _write(root / "plan.json", plan)
        _write(repo / "cache" / "tts" / "aaa.wav", "x")  # finished: its chunks count as done
        for item in ("b1", "b2", "c1"):
            _write(root / "ctx" / f"{item}.safetensors", "x")
        _write(root / "latents" / "b1.safetensors", "x")
        old = time.time() - 4000  # written long ago: not in the rate window
        os.utime(root / "ctx" / "b1.safetensors", (old, old))
        _write(d / "05_audio" / "01" / "manifest.json", {"beats": [{}, {}, {}]})

        p = agent.progress(repo, d)
        assert (p["chapters"], p["beats"], p["placed_chapters"], p["placed_beats"]) == (2, 5, 1, 3)
        assert p["phases"]["ctx"] == {"done": 4, "total": 4, "recent": 2}
        assert p["phases"]["denoise"] == {"done": 2, "total": 4, "recent": 1}
        assert p["phases"]["decode"]["done"] == 1 and p["phases"]["decode"]["total"] == 3
        assert p["cached_at_start"] == 1
        # A relaunched job counts only what it wrote itself.
        relaunched = agent.progress(repo, d, since=time.time() + 60)
        assert relaunched["phases"]["denoise"]["recent"] == 0 and relaunched["phases"]["denoise"]["done"] == 2


def test_job_states_and_the_queue():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        a, b = _book(repo, "a/v1"), _book(repo, "b/v1")
        saved = agent.pid_alive
        agent.pid_alive = lambda pid: pid == 4242
        try:
            _write(a / "_remote" / "state.json", {"state": "running", "pid": 4242})
            _write(b / "_remote" / "state.json", {"state": "running", "pid": 99})
            assert agent.job_state(a)["alive"] is True
            assert agent.job_state(b)["state"] == "interrupted"  # its process is gone
            assert agent.running_jobs(repo) == [{"book": "a/v1", "pid": 4242}]

            # The GPU is busy with a/v1: launching b/v1 queues it, keeping its place.
            queued = agent.cmd_launch({"repo": str(repo), "book": "b/v1", "queue": True, "fingerprint": "f1"})
            assert queued == {"queued": 1, "behind": "a/v1"}
            _book(repo, "c/v1")
            assert agent.cmd_launch({"repo": str(repo), "book": "c/v1", "queue": True})["queued"] == 2
            assert agent.cmd_launch({"repo": str(repo), "book": "b/v1", "queue": True, "fingerprint": "f2"})["queued"] == 1
            assert [e["book"] for e in agent.load_queue(repo)] == ["b/v1", "c/v1"]
            assert agent.load_queue(repo)[0]["fingerprint"] == "f2"
            assert agent.job_state(b)["state"] == "queued"
            # Without "queue" a busy GPU is an error.
            assert "busy" in agent.cmd_launch({"repo": str(repo), "book": "c/v1"})["error"]
            # Stopping a queued book takes it out of the queue.
            assert agent.cmd_stop({"repo": str(repo), "book": "c/v1"}) == {"stopped": True, "dequeued": True}
            assert [e["book"] for e in agent.load_queue(repo)] == ["b/v1"]
            # While a/v1 renders, `next` leaves the queue alone.
            assert agent.cmd_next({"repo": str(repo)})["launched"] is None
            assert [e["book"] for e in agent.load_queue(repo)] == ["b/v1"]
            # Probe shows the queue; a queued book the queue lost reads as interrupted.
            (repo / ".git").mkdir()
            probe = agent.cmd_probe({"repo": str(repo), "book": "b/v1"})
            assert (probe["queue"], probe["job"]["state"], probe["busy"]) == (["b/v1"], "queued", [{"book": "a/v1", "pid": 4242}])
            agent.save_queue(repo, [])
            assert agent.cmd_probe({"repo": str(repo), "book": "b/v1"})["job"]["state"] == "interrupted"
        finally:
            agent.pid_alive = saved


def test_list_and_delete_stay_inside_their_root():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        d = _book(repo)
        _write(d / "05_audio" / "_staged" / "plan.json", {})
        _write(d / "05_audio" / "01" / "x.wav.tmp", "partial")
        _write(d / "05_audio" / "01" / "01_s1_b0000.wav", "wav")
        listed = agent.cmd_list({"repo": str(repo), "root": "artifacts/series/vol-01/05_audio", "exclude": ["_staged"]})
        assert list(listed["files"]) == ["01/01_s1_b0000.wav"]
        listed = agent.cmd_list({"repo": str(repo), "root": "artifacts/series/vol-01", "paths": ["03_directed", "04_voice_assignments.json"]})
        assert sorted(listed["files"]) == ["03_directed/01.json", "03_directed/02.json", "04_voice_assignments.json"]
        root = "artifacts/series/vol-01"
        gone = agent.cmd_delete({"repo": str(repo), "root": root, "files": ["03_directed/02.json", "../../../escape.txt"]})
        assert gone == {"removed": 1} and not (d / "03_directed" / "02.json").exists()


def test_the_agent_runs_from_stdin_like_on_the_box():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _book(repo)
        proc = subprocess.run(
            [sys.executable, "-", "probe", json.dumps({"repo": str(repo), "book": "series/vol-01"})],
            input=remote._agent_source(), capture_output=True, check=True,
        )
        probe = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        assert probe["repo"] is False  # no .git there
        assert probe["repo_path"] == str(repo)


# ----- the local side -------------------------------------------------------------


def test_inputs_fingerprint_and_the_quick_check():
    with tempfile.TemporaryDirectory() as tmp:
        d = _book(Path(tmp))
        _write(d / "05_audio" / "01" / "manifest.json", {})
        assert remote.book_inputs(d) == ["00_book_meta.json", "01_characters.json", "03_directed", "04_voice_assignments.json"]
        fp = remote.fingerprint(d)
        _write(d / "00_book_meta.json", {"title": "Other"})  # not something Stage 4 renders from
        assert remote.fingerprint(d) == fp
        _write(d / "03_directed" / "02.json", {"scenes": []})
        assert remote.fingerprint(d) != fp

        ours = remote.list_files(d, remote.book_inputs(d))
        assert remote.changed(ours, ours) == []
        theirs = {k: list(v) for k, v in ours.items()}
        theirs["03_directed/01.json"][1] -= 5  # older there
        del theirs["01_characters.json"]
        assert remote.changed(ours, theirs) == ["01_characters.json", "03_directed/01.json"]


def test_check_inputs_names_what_is_missing():
    with tempfile.TemporaryDirectory() as tmp:
        d = _book(Path(tmp))
        vb = Path(tmp) / "voicebank"
        for broken, expect in (
            (lambda: None, "no voicebank"),
            (lambda: _write(vb / "manifest.json", {"clips": []}), "not in"),
            (lambda: _write(vb / "manifest.json", {"clips": [{"id": "v1", "clip_path": "clips/v1.wav"}]}), "missing"),
        ):
            broken()
            try:
                remote.check_inputs(d, vb)
            except remote.RemoteError as e:
                assert expect in str(e), str(e)
            else:
                raise AssertionError(expect)
        _write(vb / "clips" / "v1.wav", "wav")
        remote.check_inputs(d, vb)


def test_describe_and_next_step():
    probe = {
        "job": {"state": "running", "started": 1000, "fingerprint": "old"},
        "progress": {
            "chapters": 2, "beats": 5, "placed_chapters": 0, "placed_beats": 0, "renders": 3,
            "cached_at_start": 1, "rate_window": 1800,
            "phases": {
                "ctx": {"done": 4, "total": 4, "recent": 0},
                "denoise": {"done": 1, "total": 4, "recent": 30},
                "decode": {"done": 1, "total": 3, "recent": 0},
            },
        },
        "queue": ["other/v2"],
        "log": ["phase denoise: 3 pending (attempt 1)"],
    }
    lines = remote.describe("series/vol-01", probe, "new", now=1000 + 3600)
    text = "\n".join(lines)
    assert "rendering — started" in text and "changed here since" in text
    assert "1.0/min, ~3 min left in this phase  ◀" in text
    assert "queued on this box: other/v2" in text and "phase denoise" in text
    queued = remote.describe("other/v2", {"job": {"state": "queued", "queued": 1000}, "queue": ["other/v2"],
                                          "busy": [{"book": "series/vol-01"}]}, "x", now=2000)
    assert "position 1, after series/vol-01 renders" in queued[0]
    with tempfile.TemporaryDirectory() as tmp:
        d = _book(Path(tmp))
        assert remote.next_step("series/vol-01", d) == (
            "./scripts/run_pipeline.sh series/vol-01 --skip-llm --skip-tts --mode lecture "
            "--book-title 'The Lantern Keepers'"
        )


class _FakeRemote:
    """Answers like a box in a given state; records what was asked."""

    def __init__(self, job=None, busy=(), audio=None, git_error=None):
        self.calls, self.sent, self.fetched = [], [], {}
        self.git_error = git_error
        self.job, self.busy, self.audio = job, list(busy), audio or {}

    def agent(self, command, **args):
        self.calls.append(command)
        if command == "probe":
            return {"hostname": "box", "repo": True, "head": "abc1234", "branch": "main", "repo_path": "/home/u/ln-vox",
                    "job": self.job, "busy": self.busy, "queue": [], "progress": {}, "log": []}
        if command == "git":
            return {"head": "abc1234", "branch": args["branch"], "error": self.git_error}
        if command == "list":
            return {"files": self.audio if args["root"].endswith("05_audio") else {}}
        if command == "launch":
            return {"queued": 1, "behind": self.busy[0]["book"]} if self.busy else {"pid": 77}
        return {}

    def send(self, local_root, remote_root, files, say):
        self.sent.append((remote_root, len(files)))

    def fetch(self, remote_root, local_root, files, say):
        self.fetched.update(files)


def _drive(fake, tmp, git=False, **opts):
    d = _book(Path(tmp) / "repo")
    vb = Path(tmp) / "voicebank"
    _write(vb / "manifest.json", {"clips": [{"id": "v1", "clip_path": "clips/v1.wav"}]})
    _write(vb / "clips" / "v1.wav", "wav")
    said = []
    code = remote.remote_tts(
        "series/vol-01", artifacts_dir=d.parents[1], voicebank=vb, host="box", branch="comfy",
        options=remote.Options(git=git, **opts), say=said.append, remote_factory=lambda h, r: fake,
    )
    return code, "\n".join(said), d


def test_flow_launches_watches_and_fetches():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRemote()
        code, said, d = _drive(fake, tmp)
        assert code == remote.RENDERING and "Launched series/vol-01" in said
        assert [r for r, _ in fake.sent] == ["artifacts/series/vol-01", str(Path(tmp) / "voicebank").replace("\\", "/").split("/")[-1], "artifacts/_remote"]
        assert json.loads((d / "_remote" / "local.json").read_text())["host"] == "box"

    with tempfile.TemporaryDirectory() as tmp:  # rendering: nothing is sent
        fake = _FakeRemote(job={"state": "running", "started": 1})
        code, said, _ = _drive(fake, tmp)
        assert code == remote.RENDERING and fake.sent == [] and "Still rendering" in said

    with tempfile.TemporaryDirectory() as tmp:  # done: only finished chapters are fetched
        audio = {"01/manifest.json": [10, 1], "01/01_s1_b0000.wav": [99, 1], "02/02_s1_b0000.wav": [99, 1]}
        fake = _FakeRemote(job={"state": "done", "finished": 1}, audio=audio)
        fake.job["fingerprint"] = remote.fingerprint(_book(Path(tmp) / "repo"))
        code, said, _ = _drive(fake, tmp)
        assert code == 0 and sorted(fake.fetched) == ["01/01_s1_b0000.wav", "01/manifest.json"]
        assert "--skip-llm --skip-tts --mode lecture" in said

    with tempfile.TemporaryDirectory() as tmp:  # failed: not relaunched without --restart
        fake = _FakeRemote(job={"state": "failed", "exit_code": 1, "finished": 1})
        code, said, _ = _drive(fake, tmp)
        assert code == 1 and "launch" not in fake.calls and "--restart" in said
        code, said, _ = _drive(fake, tmp, restart=True)
        assert code == remote.RENDERING and "launch" in fake.calls

    with tempfile.TemporaryDirectory() as tmp:  # the box can't update its checkout: it renders with it
        fake = _FakeRemote(git_error="git fetch origin --prune: fatal: Could not resolve host: github.com")
        code, said, _ = _drive(fake, tmp, git=True)
        assert code == remote.RENDERING and "couldn't update the box's checkout" in said and "launch" in fake.calls

    with tempfile.TemporaryDirectory() as tmp:  # another book renders: queued behind it
        fake = _FakeRemote(busy=[{"book": "other/v2", "pid": 5}])
        code, said, _ = _drive(fake, tmp)
        assert code == remote.RENDERING and "Queued series/vol-01 on the box: position 1, after other/v2" in said


def _fake_ssh(tmp: Path) -> tuple:
    """An `ssh` that runs the remote command with bash here, HOME = tmp."""
    script = tmp / "fake_ssh.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "env = dict(os.environ, HOME=sys.argv[1])\n"
        "sys.exit(subprocess.call(['bash', '-c', sys.argv[3]], env=env))\n",
        encoding="utf-8",
    )
    return (sys.executable, str(script), str(tmp / "home"))


def test_send_and_fetch_mirror_files_and_mtimes():
    import pytest

    if not (shutil.which("bash") and shutil.which("tar")):
        pytest.skip("needs bash and tar")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "home" / "ln-vox").mkdir(parents=True)
        box = remote.Remote("box", "ln-vox", ssh=_fake_ssh(tmp))
        src = tmp / "here"
        a = _write(src / "clips" / "a.wav", "A" * 3000)
        _write(src / "manifest.json", {"clips": []})
        os.utime(a, (1_700_000_000.7, 1_700_000_000.7))
        box.send(src, "voicebank", ["clips/a.wav", "manifest.json"], lambda _m: None)
        there = tmp / "home" / "ln-vox" / "voicebank"
        assert (there / "clips" / "a.wav").read_text() == "A" * 3000
        assert remote.list_files(there) == remote.list_files(src)  # sizes and mtimes: nothing left to send

        back = tmp / "back"
        box.fetch("voicebank", back, remote.list_files(there), lambda _m: None)
        assert remote.list_files(back) == remote.list_files(src)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

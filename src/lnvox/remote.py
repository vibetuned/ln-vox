"""Render Stage 4 on another machine: `lnvox remote-tts <book>`.

The LLM stages run here; the TTS render runs on a GPU box reached over SSH —
a DGX Spark, a cloud instance — so this machine can sleep or shut down while
it renders. One command, run as often as you like, does what the book's
state calls for:

    nothing launched yet  clone or update the box's checkout, mirror the
                          book's Stage 0-4 inputs and the voicebank there,
                          start the render
    rendering             show its progress; with --pull, fetch the chapters
                          already placed
    finished              fetch 05_audio, so Stage 5 and 6 run here
    interrupted           (the box rebooted) mirror and start again — the
                          render resumes from its checkpoints and the cache
    failed or stopped     show why; --restart starts it again

The box's half is `remote_agent.py`, piped to its python3 on every call, so
the box needs nothing installed beyond git, tar, python3 and uv. Files move
as tar streams over ssh, and only those whose size or mtime differ — rsync's
quick check, without needing rsync (Git for Windows has none).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

STATE_DIR = "_remote"
AUDIO_DIR = "05_audio"
# The book's inputs to Stage 4 and what produced them: 00_text.jsonl,
# 01_characters.json, 02_scenes/, 03_directed/, 04_voice_assignments.json, …
_INPUT_RE = re.compile(r"^0[0-4]")
PHASES = ("ctx", "denoise", "decode")
RENDERING = 3  # exit code: launched, or still rendering — come back later


class RemoteError(RuntimeError):
    pass


def _agent_source() -> bytes:
    text = Path(__file__).with_name("remote_agent.py").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").encode("utf-8")


@dataclass
class Remote:
    """One render box: an SSH host and the ln-vox checkout on it (relative
    to the remote home, or absolute)."""

    host: str
    repo: str = "ln-vox"
    ssh: tuple[str, ...] = (
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
    )

    def _cmd(self, remote_command: str) -> list[str]:
        return [*self.ssh, self.host, remote_command]

    def _cd(self) -> str:
        if self.repo.startswith("/"):
            return f"cd {shlex.quote(self.repo)}"
        return f"cd ~/{shlex.quote(self.repo)}"  # the ~ stays unquoted, so it expands

    def agent(self, command: str, **args) -> dict:
        """Run one `remote_agent` command on the box → its JSON answer."""
        proc = subprocess.run(
            self._cmd(f"python3 - {command} {shlex.quote(json.dumps(args))}"),
            input=_agent_source(),
            capture_output=True,
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not out:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RemoteError(f"{self.host}: '{command}' failed — {err[-800:] or f'exit {proc.returncode}'}")
        return json.loads(out.splitlines()[-1])

    def send(self, local_root: Path, remote_root: str, files: list[str], say: Callable[[str], None]) -> None:
        """Copy `files` (relative to both roots) to the box, mtimes kept."""
        if not files:
            return
        cmd = self._cmd(
            f"{self._cd()} && mkdir -p {shlex.quote(remote_root)} && tar -xf - -C {shlex.quote(remote_root)}"
        )
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        errors = _drain(proc.stderr)
        meter = _Meter(sum((local_root / f).stat().st_size for f in files), len(files), say)
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.GNU_FORMAT) as tar:
                for rel in files:
                    path = local_root / rel
                    info = tar.gettarinfo(str(path), arcname=rel)
                    info.mtime = int(info.mtime)
                    info.mode, info.uid, info.gid, info.uname, info.gname = 0o644, 0, 0, "", ""
                    with open(path, "rb") as f:
                        tar.addfile(info, f)
                    meter.add(info.size)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
        if proc.wait() != 0:
            raise RemoteError(f"{self.host}: upload to {remote_root} failed — {errors()[-800:]}")

    def fetch(self, remote_root: str, local_root: Path, files: dict[str, list[int]], say: Callable[[str], None]) -> None:
        """Copy `files` ({relative path: [size, mtime]}) from the box."""
        if not files:
            return
        cmd = self._cmd(f"{self._cd()} && cd {shlex.quote(remote_root)} && tar -cf - -T -")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        errors = _drain(proc.stderr)
        names = ("\n".join(files) + "\n").encode("utf-8")

        def feed() -> None:  # a thread: tar starts answering before it has read every name
            try:
                proc.stdin.write(names)
            finally:
                proc.stdin.close()

        threading.Thread(target=feed, daemon=True).start()
        meter = _Meter(sum(size for size, _ in files.values()), len(files), say)
        root = local_root.resolve()
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                dest = (root / member.name).resolve()
                if root not in dest.parents:
                    raise RemoteError(f"refusing to write outside {root}: {member.name}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".part")
                with tar.extractfile(member) as src, open(part, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.replace(part, dest)
                os.utime(dest, (member.mtime, member.mtime))
                meter.add(member.size)
        if proc.wait() != 0:
            raise RemoteError(f"{self.host}: download from {remote_root} failed — {errors()[-800:]}")


def _drain(stream) -> Callable[[], str]:
    """Read a pipe in the background (so it never fills and blocks the
    transfer) → a function returning what it said."""
    chunks: list[bytes] = []
    thread = threading.Thread(target=lambda: chunks.extend(iter(lambda: stream.read(4096), b"")), daemon=True)
    thread.start()

    def text() -> str:
        thread.join(timeout=5)
        return b"".join(chunks).decode("utf-8", "replace").strip()

    return text


class _Meter:
    """Prints a transfer's progress about every 10%."""

    def __init__(self, total: int, count: int, say: Callable[[str], None]):
        self.total, self.count, self.say = max(total, 1), count, say
        self.done = self.files = 0
        self.next = 0.1
        say(f"    {count} file(s), {_size(total)}")

    def add(self, size: int) -> None:
        self.done += size
        self.files += 1
        if self.count > 20 and self.done / self.total >= self.next:
            self.say(f"    {self.done / self.total:.0%}  ({self.files}/{self.count} files, {_size(self.done)})")
            self.next = int(self.done / self.total * 10 + 1) / 10


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ----- local files ------------------------------------------------------------


def list_files(root: Path, paths: list[str] | None = None, exclude: tuple[str, ...] = ()) -> dict[str, list[int]]:
    """Files under `root` (only `paths` in it, if given) → [size, mtime],
    keyed by POSIX relative path — the agent's `list`, here."""
    files: dict[str, list[int]] = {}
    for start in [root / p for p in (paths or ["."])]:
        if start.is_file():
            st = start.stat()
            files[start.relative_to(root).as_posix()] = [st.st_size, int(st.st_mtime)]
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in exclude]
            for name in filenames:
                if name.endswith((".tmp", ".part")) or ".tmp." in name:
                    continue
                p = Path(dirpath) / name
                st = p.stat()
                files[p.relative_to(root).as_posix()] = [st.st_size, int(st.st_mtime)]
    return files


def changed(source: dict[str, list[int]], target: dict[str, list[int]]) -> list[str]:
    """Files to copy so `target` matches `source`: missing there, or of
    another size or mtime."""
    return sorted(rel for rel, meta in source.items() if target.get(rel) != meta)


def book_inputs(book_dir: Path) -> list[str]:
    """The book's Stage 4 inputs, as names under its artifacts folder."""
    return sorted(p.name for p in book_dir.iterdir() if _INPUT_RE.match(p.name)) if book_dir.is_dir() else []


def fingerprint(book_dir: Path) -> str:
    """What Stage 4 renders from — the directed beats and the voice
    assignments — so a finished render is known to be stale."""
    h = hashlib.sha256()
    files = sorted((book_dir / "03_directed").glob("*.json")) + [book_dir / "04_voice_assignments.json"]
    for f in files:
        if f.is_file():
            h.update(f.relative_to(book_dir).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def check_inputs(book_dir: Path, voicebank: Path) -> None:
    """Refuse to ship a book Stage 4 can't render."""
    if not list((book_dir / "03_directed").glob("*.json")):
        raise RemoteError(
            f"no directed beats in {book_dir / '03_directed'} — run the LLM stages here first "
            "(the launcher with --skip-tts)"
        )
    assignments = book_dir / "04_voice_assignments.json"
    if not assignments.is_file():
        raise RemoteError(f"no {assignments} — run voice cast first")
    manifest = voicebank / "manifest.json"
    if not manifest.is_file():
        raise RemoteError(f"no voicebank at {voicebank} (manifest.json missing)")
    clips = {c["id"]: c for c in json.loads(manifest.read_text(encoding="utf-8")).get("clips", [])}
    cast = json.loads(assignments.read_text(encoding="utf-8")).get("castings", [])
    for c in cast:
        clip = c.get("assigned_clip_id")
        if not clip:
            continue
        if clip not in clips:
            raise RemoteError(f"{c.get('character_name')}'s clip {clip!r} is not in {manifest}")
        if not (voicebank / clips[clip]["clip_path"]).is_file():
            raise RemoteError(f"{c.get('character_name')}'s clip file {voicebank / clips[clip]['clip_path']} is missing")


def _git_local(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ----- presentation -----------------------------------------------------------


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "?"
    return f"{datetime.fromtimestamp(ts):%d %b %H:%M} ({_span(now - ts)} ago)"


def _span(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def describe(book_id: str, probe: dict, local_fingerprint: str, now: float | None = None) -> list[str]:
    """The job's state, progress and log tail, as lines to print."""
    now = time.time() if now is None else now
    job = probe.get("job") or {}
    prog = probe.get("progress") or {}
    state = job.get("state")
    lines: list[str] = []
    if not state:
        lines.append(f"{book_id}: nothing launched on this box yet")
    elif state in ("starting", "running"):
        step = " (setting up the TTS environment)" if job.get("step") == "setup" else ""
        lines.append(f"{book_id}: rendering{step} — started {_ago(job.get('started'), now)}")
    elif state == "queued":
        queue = probe.get("queue") or []
        place = queue.index(book_id) + 1 if book_id in queue else "?"
        busy = (probe.get("busy") or [{}])[0].get("book")
        after = f", after {busy} renders" if busy else " (nothing rendering: it starts on the next run)"
        lines.append(f"{book_id}: queued {_ago(job.get('queued'), now)} — position {place}{after}")
    elif state == "done":
        lines.append(f"{book_id}: finished {_ago(job.get('finished'), now)}")
    elif state == "interrupted":
        lines.append(f"{book_id}: interrupted — its process is gone (the box restarted?); started {_ago(job.get('started'), now)}")
    else:
        code = f", exit {job['exit_code']}" if "exit_code" in job else ""
        lines.append(f"{book_id}: {state}{code} — {_ago(job.get('finished') or job.get('updated'), now)}")
    if state and job.get("fingerprint") and job["fingerprint"] != local_fingerprint:
        lines.append("  ! the directed beats or voices changed here since this render was launched")

    phases = prog.get("phases")
    if phases:
        current = next((p for p in PHASES if phases[p]["done"] < phases[p]["total"]), None)
        cached = prog.get("cached_at_start", 0)
        lines.append(
            f"  {prog.get('renders', 0)} renders ({cached} cached before this run), "
            f"{phases['ctx']['total']} chunks"
        )
        for name in PHASES:
            ph = phases[name]
            pct = ph["done"] / ph["total"] if ph["total"] else 1.0
            line = f"  {name:<8}{ph['done']:>6}/{ph['total']:<6} {pct:>4.0%}"
            if name == current and state in ("starting", "running"):
                window = prog.get("rate_window", 1800)
                rate = ph["recent"] / (window / 60) if window >= 300 else 0  # 5 min of data, at least
                if rate > 0:
                    line += f"  {rate:.1f}/min, ~{_span((ph['total'] - ph['done']) / rate * 60)} left in this phase"
                line += "  ◀"
            lines.append(line)
    if prog.get("chapters"):
        lines.append(
            f"  chapters placed: {prog.get('placed_chapters', 0)}/{prog['chapters']} "
            f"({prog.get('placed_beats', 0)}/{prog.get('beats', 0)} beats)"
        )
    others = [b for b in probe.get("queue") or [] if b != book_id]
    if others:
        lines.append(f"  queued on this box: {', '.join(others)}")
    if probe.get("log") and state not in (None, "done", "queued"):
        lines.append("  log:")
        lines += [f"    {ln[:160]}" for ln in probe["log"]]
    return lines


def next_step(book_id: str, book_dir: Path) -> str:
    """The launcher line that mixes and syncs the fetched render here."""
    if (book_dir / "00_script.json").exists():
        mode = "scenario"
    elif _read_json(book_dir / "01_characters.json").get("characters") == []:
        mode = "lecture"
    else:
        mode = "narration"
    title = _read_json(book_dir / "00_book_meta.json").get("title") or book_id
    return (
        f"./scripts/run_pipeline.sh {book_id} --skip-llm --skip-tts --mode {mode} "
        f"--book-title {shlex.quote(title)}"
    )


# ----- the command --------------------------------------------------------------


@dataclass
class Options:
    status: bool = False
    pull: bool = False
    restart: bool = False
    stop: bool = False
    wait: bool = False
    interval: int = 300
    git: bool = True
    log_lines: int = 12


def remote_tts(
    book_id: str,
    *,
    artifacts_dir: Path,
    voicebank: Path,
    host: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    options: Options | None = None,
    say: Callable[[str], None] = print,
    remote_factory: Callable[[str, str], Remote] = Remote,
) -> int:
    """Drive the book's remote Stage 4 one step forward. → exit code:
    0 the render is here (fetched), or a --status/--stop answered;
    3 launched, or still rendering; 1 failed or stopped (see the log)."""
    options = options or Options()
    book_dir = artifacts_dir / book_id
    local_state_path = book_dir / STATE_DIR / "local.json"
    local = _read_json(local_state_path)
    host = host or local.get("host") or os.environ.get("LNVOX_REMOTE_HOST")
    if not host:
        raise RemoteError("which box? pass --host (an ssh alias or user@host) or set LNVOX_REMOTE_HOST")
    repo = repo or local.get("repo") or "ln-vox"
    remote = remote_factory(host, repo)

    probe = remote.agent("probe", repo=repo, book=book_id, log_lines=options.log_lines)
    local_state_path.parent.mkdir(parents=True, exist_ok=True)
    local_state_path.write_text(json.dumps({"host": host, "repo": repo}, indent=2), encoding="utf-8")
    gpu = f", {probe['gpu']}" if probe.get("gpu") else ""
    head = f" @ {probe['head'][:7]} ({probe.get('branch')})" if probe.get("repo") else " (no checkout yet)"
    say(f"{host} — {probe.get('hostname')}{gpu} — {probe.get('repo_path')}{head}")

    if options.stop:
        stopped = remote.agent("stop", repo=repo, book=book_id).get("stopped")
        say(f"{book_id}: stopped" if stopped else f"{book_id}: no job running")
        return 0

    local_fp = fingerprint(book_dir)
    job = probe.get("job") or {}
    while options.wait and job.get("state") in ("starting", "running"):
        for line in describe(book_id, {**probe, "log": []}, local_fp):
            say(line)
        say(f"  (checking again in {options.interval // 60} min — Ctrl+C to leave it rendering)")
        time.sleep(options.interval)
        probe = remote.agent("probe", repo=repo, book=book_id, log_lines=options.log_lines)
        job = probe.get("job") or {}

    for line in describe(book_id, probe, local_fp):
        say(line)
    state = job.get("state")
    if options.status:
        return 0

    if state in ("starting", "running"):
        if options.pull:
            _pull(remote, repo, book_id, book_dir, say)
        else:
            say("Still rendering — run this again later (--wait to stay connected, --pull for the finished chapters).")
        return RENDERING
    stale = bool(job.get("fingerprint")) and job.get("fingerprint") != local_fp
    if state == "queued" and probe.get("busy") and not stale:
        say("Queued — it starts on its own when the current render ends. You can shut down this machine.")
        return RENDERING
    if state == "done" and not options.restart and not stale:
        _pull(remote, repo, book_id, book_dir, say)
        say("Render fetched. Mix and sync it here:")
        say(f"  {next_step(book_id, book_dir)}")
        return 0
    if state in ("failed", "stopped") and not options.restart:
        say(f"Not relaunching a {state} job on its own: fix the cause (log above), then run again with --restart.")
        return 1
    if state == "done" and stale:
        say("The inputs changed since this render: launching again (unchanged beats come from the box's cache).")
    elif state == "interrupted":
        say("Relaunching: the render resumes from its checkpoints.")

    _launch(remote, repo, book_id, book_dir, voicebank, branch, probe, local_fp, options, say)
    return RENDERING


def _pull(remote: Remote, repo: str, book_id: str, book_dir: Path, say: Callable[[str], None]) -> None:
    root = f"artifacts/{book_id}/{AUDIO_DIR}"
    theirs = remote.agent("list", repo=repo, root=root, exclude=["_staged"])["files"]
    # Only finished chapters: a chapter's WAVs count once its manifest is written.
    done = {rel.split("/")[0] for rel in theirs if rel.endswith("/manifest.json")}
    theirs = {rel: meta for rel, meta in theirs.items() if rel.split("/")[0] in done}
    local_dir = book_dir / AUDIO_DIR
    ours = list_files(local_dir, exclude=("_staged",)) if local_dir.is_dir() else {}
    todo = changed(theirs, ours)
    say(f"Fetching {AUDIO_DIR}: {len(done)} chapter(s) on the box, {len(todo)} file(s) new or changed")
    remote.fetch(root, local_dir, {rel: theirs[rel] for rel in todo}, say)


def _launch(
    remote: Remote,
    repo: str,
    book_id: str,
    book_dir: Path,
    voicebank: Path,
    branch: str | None,
    probe: dict,
    local_fp: str,
    options: Options,
    say: Callable[[str], None],
) -> None:
    check_inputs(book_dir, voicebank)

    if options.git:
        url = _git_local("remote", "get-url", "origin")
        branch = branch or _git_local("rev-parse", "--abbrev-ref", "HEAD")
        if not url or not branch:
            raise RemoteError("can't tell this checkout's origin URL and branch; pass --branch, or --no-git")
        say(f"{'Cloning' if not probe.get('repo') else 'Updating'} the box's checkout: {branch} from {url}")
        git = remote.agent("git", repo=repo, url=url, branch=branch)
        if git.get("error"):
            if not probe.get("repo") or not git.get("head"):
                raise RemoteError(f"git on the box: {git['error']}")
            # A checkout is there (a network blip, its own commits): an
            # unattended queue renders with it rather than stopping.
            say(f"  ! couldn't update the box's checkout: {git['error'].strip().splitlines()[-1]}")
        mine = _git_local("rev-parse", "HEAD")
        say(f"  box at {git['head'][:7]} ({git['branch']})")
        if mine and mine != git["head"]:
            say(f"  ! this checkout is at {mine[:7]}: push it first if the change matters to Stage 4")
        if _git_local("status", "--porcelain", "--untracked-files=no"):
            say("  ! uncommitted changes here don't reach the box")
    elif not probe.get("repo"):
        raise RemoteError(f"no checkout at {probe.get('repo_path')} — drop --no-git to clone it")

    inputs = book_inputs(book_dir)
    root = f"artifacts/{book_id}"
    ours = list_files(book_dir, inputs)
    theirs = remote.agent("list", repo=repo, root=root, paths=inputs)["files"] if inputs else {}
    todo = changed(ours, theirs)
    say(f"Syncing the book's inputs ({', '.join(inputs)}): {len(todo)} of {len(ours)} file(s) new or changed")
    remote.send(book_dir, root, todo, say)
    # Chapters dropped here must not be rendered there.
    gone = [rel for rel in theirs if rel.startswith("03_directed/") and rel not in ours]
    if gone:
        remote.agent("delete", repo=repo, root=root, files=gone)
        say(f"  removed {len(gone)} directed chapter(s) no longer here")

    vb_remote = voicebank.as_posix() if not voicebank.is_absolute() else voicebank.name
    ours = list_files(voicebank)
    theirs = remote.agent("list", repo=repo, root=vb_remote)["files"]
    todo = changed(ours, theirs)
    say(f"Syncing the voicebank ({voicebank}): {len(todo)} of {len(ours)} file(s) new or changed")
    remote.send(voicebank, vb_remote, todo, say)

    # The box's own copy of the agent: a finishing job runs it to start the
    # next queued book.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "agent.py").write_bytes(_agent_source())
        remote.send(Path(tmp), "artifacts/_remote", ["agent.py"], lambda _m: None)

    env = {"LNVOX_VOICEBANK": vb_remote} if vb_remote != "voicebank" else {}
    started = remote.agent(
        "launch", repo=repo, book=book_id, fingerprint=local_fp, env=env, queue=True,
        launched_from=_git_local("config", "user.name") or os.environ.get("USERNAME", ""),
    )
    if started.get("error"):
        raise RemoteError(started["error"])
    if started.get("queued"):
        say(
            f"Queued {book_id} on the box: position {started['queued']}, after {started['behind']} — "
            "it starts on its own when that render ends. You can shut down this machine."
        )
    else:
        say(f"Launched {book_id} on the box (pid {started['pid']}). You can disconnect or shut down this machine.")
    say(f"Check on it with: uv run lnvox remote-tts {book_id}")

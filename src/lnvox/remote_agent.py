"""The render box's half of `lnvox remote-tts` (see `lnvox.remote`).

This file never runs here: `lnvox.remote` pipes its source to the box as
`python3 - <command> '<json args>'`, which prints one JSON object. Standard
library only and Python 3.8 syntax, so it works on a fresh box before any
venv exists (DGX OS, a stock cloud image).

Commands:

    probe    tools, the checkout, the book's job and its progress, the queue
    list     files under a directory → {relative path: [size, mtime]}
    delete   remove files under a directory
    git      clone the repo if missing, else fetch and fast-forward a branch
    launch   start the book's Stage 4 job, detached from the SSH session —
             or, with "queue" while another book renders, queue it
    next     start the first queued book (a finishing job calls it)
    stop     stop the book's job, or take it out of the queue

A job is `artifacts/<book>/_remote/job.sh`, run in its own session: the
first time on a box it installs the TTS environment and Dramabox, then runs
`scripts/run_pipeline.sh <book> --skip-llm --skip-mix --skip-sync` — Stage 4
only, with the launcher's own env preparation and crash retries. It records
its state in `_remote/state.json` and logs to `_remote/job.log`.

One GPU renders one book at a time: the box keeps a queue in
`artifacts/_remote/queue.json`, and a job that ends — done or failed, not
stopped — starts the next queued book through a copy of this file that
`lnvox remote-tts` keeps at `artifacts/_remote/agent.py`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = "_remote"
QUEUE = "artifacts/_remote/queue.json"  # in the checkout
AGENT = "artifacts/_remote/agent.py"  # this file, for a finishing job to call
EXTRA_PATH = ["~/.local/bin", "~/.cargo/bin"]  # where uv's installer puts it
# Rendering rate is measured over this many recent seconds of output files.
RATE_WINDOW = 1800

JOB_SCRIPT = """#!/usr/bin/env bash
# Written by `lnvox remote-tts`: renders one book's Stage 4, detached from
# any SSH session. State: {state}  Log: {log}
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd {repo}
BOOK={book}
STATE={state}

note() {{
    python3 - "$STATE" "$@" <<'PY'
import json, os, sys, time
path = sys.argv[1]
try:
    with open(path) as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {{}}
for item in sys.argv[2:]:
    key, _, value = item.partition("=")
    state[key] = int(value) if value.lstrip("-").isdigit() else value
state["updated"] = int(time.time())
with open(path + ".tmp", "w") as f:
    json.dump(state, f, indent=2)
os.replace(path + ".tmp", path)
PY
}}

finish() {{
    if [ "$1" -eq 0 ]; then
        note state=done exit_code=0 finished="$(date +%s)"
    else
        note state=failed exit_code="$1" finished="$(date +%s)"
    fi
    # Hand the GPU to the next queued book, if any.
    if [ -f {agent} ]; then
        echo "== $(date -Iseconds): $BOOK ended (exit $1); next in the queue:"
        python3 {agent} next {next_args} || true
    fi
    exit "$1"
}}

trap 'note state=stopped finished="$(date +%s)"; exit 143' TERM INT

echo "== lnvox remote-tts: $BOOK on $(hostname), $(date -Iseconds), commit $(git rev-parse --short HEAD)"
note state=running step=setup pid=$$
if [ ! -f external/DramaBox/requirements.txt ]; then
    echo "== First run on this box: TTS environment + Dramabox (weights download on the first render)"
    uv sync --extra voice --extra tts || finish $?
    ./scripts/setup_dramabox.sh || finish $?
fi
note step=render
./scripts/run_pipeline.sh "$BOOK" --skip-llm --skip-mix --skip-sync
finish $?
"""


def _repo(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else Path.home() / p


def _paths() -> str:
    return os.pathsep.join([os.path.expanduser(p) for p in EXTRA_PATH] + [os.environ.get("PATH", "")])


def _run(cmd: list, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(str(tmp), str(path))


def pid_alive(pid) -> bool:
    """A live process that is one of our jobs (a pid reused after a reboot
    belongs to something else)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path("/proc/{}/cmdline".format(pid))
    if cmdline.exists():
        try:
            return b"job.sh" in cmdline.read_bytes()
        except OSError:
            return False
    return True


def job_state(book_dir: Path):
    """The book's job record, with `state` "interrupted" for a job recorded
    as running whose process is gone (the box rebooted, it was killed)."""
    state = _read_json(book_dir / STATE_DIR / "state.json")
    if not state:
        return None
    if state.get("state") in ("starting", "running"):
        state["alive"] = pid_alive(state.get("pid"))
        if not state["alive"]:
            state["state"] = "interrupted"
    else:
        state["alive"] = False
    return state


def load_queue(repo: Path) -> list:
    return _read_json(repo / QUEUE) or []


def save_queue(repo: Path, queue: list) -> None:
    _write_json(repo / QUEUE, queue)


def enqueue(repo: Path, book: str, args: dict, behind: str) -> dict:
    """Put `book` in the queue (keeping its place if it's already there,
    with its entry refreshed) → its 1-based position."""
    queue = load_queue(repo)
    entry = {
        "book": book,
        "fingerprint": args.get("fingerprint", ""),
        "env": args.get("env") or {},
        "launched_from": args.get("launched_from", ""),
        "queued": int(time.time()),
    }
    for i, old in enumerate(queue):
        if old["book"] == book:
            entry["queued"] = old.get("queued", entry["queued"])
            queue[i] = entry
            break
    else:
        queue.append(entry)
    save_queue(repo, queue)
    _write_json(repo / "artifacts" / book / STATE_DIR / "state.json", {
        "state": "queued",
        "book": book,
        "queued": entry["queued"],
        "fingerprint": entry["fingerprint"],
    })
    return {"queued": [e["book"] for e in queue].index(book) + 1, "behind": behind}


def dequeue(repo: Path, book: str) -> bool:
    queue = load_queue(repo)
    kept = [e for e in queue if e["book"] != book]
    if len(kept) != len(queue):
        save_queue(repo, kept)
        return True
    return False


def running_jobs(repo: Path) -> list:
    """Every book with a live job in this checkout: one GPU, one render."""
    found = []
    for path in sorted((repo / "artifacts").glob("**/{}/state.json".format(STATE_DIR))):
        book_dir = path.parent.parent
        state = job_state(book_dir)
        if state and state.get("alive"):
            found.append({"book": book_dir.relative_to(repo / "artifacts").as_posix(), "pid": state.get("pid")})
    return found


def _count_beats(directed: Path) -> tuple:
    chapters = beats = 0
    for f in sorted(directed.glob("*.json")):
        data = _read_json(f) or {}
        chapters += 1
        beats += sum(len(s.get("beats", [])) for s in data.get("scenes", []))
    return chapters, beats


def _recent(paths: list, start: float) -> int:
    """How many of `paths` were written since `start`."""
    n = 0
    for p in paths:
        try:
            if p.stat().st_mtime >= start:
                n += 1
        except OSError:
            pass
    return n


def progress(repo: Path, book_dir: Path, now=None, since: float = 0) -> dict:
    """How far the book's Stage 4 has got, read from disk.

    The staged renderer sweeps the whole book one phase at a time — prompt
    encoding (ctx), the diffusion loop (denoise, the bulk of the time), then
    decode — marking each item done by writing a file, and placing each
    chapter's WAVs once decode reaches it. Rates are counted from the files
    each phase wrote in the last half hour — and only since `since`, the
    job's start: a relaunched render finds its earlier output on disk.
    """
    now = time.time() if now is None else now
    start = max(now - RATE_WINDOW, since)
    audio = book_dir / "05_audio"
    chapters, beats = _count_beats(book_dir / "03_directed")
    placed_chapters = placed_beats = 0
    for manifest in sorted(audio.glob("*/manifest.json")):
        data = _read_json(manifest) or {}
        placed_chapters += 1
        placed_beats += len(data.get("beats", []))
    out = {
        "chapters": chapters,
        "beats": beats,
        "placed_chapters": placed_chapters,
        "placed_beats": placed_beats,
        "phases": None,
    }
    root = audio / "_staged"
    plan = _read_json(root / "plan.json")
    if not plan:
        return out
    cache = repo / "cache" / "tts"
    renders = plan.get("renders", [])
    finished = [r for r in renders if (cache / "{}.wav".format(r["cache_key"])).exists()]
    live = [r for r in renders if not (cache / "{}.wav".format(r["cache_key"])).exists()]
    chunks = sum(len(r.get("chunks", [])) for r in renders)
    live_items = [c["item_id"] for r in live for c in r.get("chunks", [])]
    ctx_files = [root / "ctx" / "{}.safetensors".format(i) for i in live_items]
    latent_files = [root / "latents" / "{}.safetensors".format(i) for i in live_items]
    ctx_have = [p for p in ctx_files if p.exists()]
    latent_have = [p for p in latent_files if p.exists()]
    wav_files = [cache / "{}.wav".format(r["cache_key"]) for r in finished]
    out["phases"] = {
        "ctx": {"done": chunks - len(ctx_files) + len(ctx_have), "total": chunks,
                "recent": _recent(ctx_have, start)},
        "denoise": {"done": chunks - len(latent_files) + len(latent_have), "total": chunks,
                    "recent": _recent(latent_have, start)},
        "decode": {"done": len(finished), "total": len(renders),
                   "recent": _recent(wav_files, start)},
    }
    out["renders"] = len(renders)
    out["cached_at_start"] = sum(1 for r in renders if r.get("cached"))
    out["rate_window"] = int(now - start)
    return out


def _tail(path: Path, n: int) -> list:
    try:
        with open(str(path), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ln for ln in re.split(r"[\r\n]+", data) if ln.strip()]
    return lines[-n:] if n > 0 else []


def _git(repo: Path, *args) -> str:
    proc = _run(["git", "-C", str(repo)] + list(args))
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _gpu() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    proc = _run([exe, "--query-gpu=name,utilization.gpu", "--format=csv,noheader"])
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""


# ----- commands -------------------------------------------------------------


def cmd_probe(args: dict) -> dict:
    repo = _repo(args["repo"])
    book_dir = repo / "artifacts" / args["book"]
    out = {
        "hostname": socket.gethostname(),
        "repo_path": str(repo),
        "repo": (repo / ".git").exists(),
        "tools": {t: bool(shutil.which(t, path=_paths())) for t in ("git", "uv", "tar", "nvidia-smi")},
        "gpu": _gpu(),
    }
    if out["repo"]:
        out["head"] = _git(repo, "rev-parse", "HEAD")
        out["branch"] = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        out["dramabox"] = (repo / "external" / "DramaBox" / "requirements.txt").exists()
        out["job"] = job_state(book_dir)
        out["busy"] = [j for j in running_jobs(repo) if j["book"] != args["book"]]
        out["queue"] = [e["book"] for e in load_queue(repo)]
        if out["job"] and out["job"].get("state") == "queued" and args["book"] not in out["queue"]:
            out["job"]["state"] = "interrupted"  # queued once, but the queue lost it
        out["progress"] = progress(repo, book_dir, since=(out["job"] or {}).get("started") or 0)
        out["log"] = _tail(book_dir / STATE_DIR / "job.log", int(args.get("log_lines", 12)))
    return out


def cmd_list(args: dict) -> dict:
    """Files under repo/root (only `paths` in it, if given) → [size, mtime]."""
    root = _repo(args["repo"]) / args["root"]
    skip = set(args.get("exclude", []))
    files = {}
    starts = [root / p for p in args.get("paths") or ["."]]
    for start in starts:
        if start.is_file():
            st = start.stat()
            files[start.relative_to(root).as_posix()] = [st.st_size, int(st.st_mtime)]
            continue
        for dirpath, dirnames, filenames in os.walk(str(start)):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                if name.endswith(".tmp") or ".tmp." in name:
                    continue
                p = Path(dirpath) / name
                st = p.stat()
                files[p.relative_to(root).as_posix()] = [st.st_size, int(st.st_mtime)]
    return {"files": files}


def cmd_delete(args: dict) -> dict:
    root = (_repo(args["repo"]) / args["root"]).resolve()
    removed = 0
    for rel in args.get("files", []):
        p = (root / rel).resolve()
        if root in p.parents and p.is_file():
            p.unlink()
            removed += 1
    return {"removed": removed}


def cmd_git(args: dict) -> dict:
    """Clone when missing; else fetch and fast-forward `branch`. Never
    discards local changes: a checkout that can't fast-forward is reported."""
    repo = _repo(args["repo"])
    branch = args["branch"]
    out = {"cloned": False, "error": None}
    if not (repo / ".git").exists():
        if repo.exists() and any(repo.iterdir()):
            out["error"] = "{} exists and is not a git checkout".format(repo)
            return out
        proc = _run(["git", "clone", "--branch", branch, args["url"], str(repo)])
        if proc.returncode != 0:
            out["error"] = proc.stderr.strip()[-800:]
            return out
        out["cloned"] = True
    else:
        steps = [["fetch", "origin", "--prune"]]
        if _git(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            has_local = _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch]).returncode == 0
            steps.append(["checkout", branch] if has_local else ["checkout", "-b", branch, "--track", "origin/" + branch])
        steps.append(["merge", "--ff-only", "origin/" + branch])
        for step in steps:
            proc = _run(["git", "-C", str(repo)] + step)
            if proc.returncode != 0:
                out["error"] = "git {}: {}".format(" ".join(step), (proc.stderr or proc.stdout).strip()[-800:])
                break
    out["head"] = _git(repo, "rev-parse", "HEAD")
    out["branch"] = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    out["dirty"] = _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
    return out


def cmd_launch(args: dict) -> dict:
    repo = _repo(args["repo"])
    book = args["book"]
    state_dir = repo / "artifacts" / book / STATE_DIR
    current = job_state(state_dir.parent)
    if current and current.get("alive"):
        return {"error": "a job for {} is already running (pid {})".format(book, current.get("pid"))}
    busy = [j for j in running_jobs(repo) if j["book"] != book]
    if busy:
        if args.get("queue"):
            return enqueue(repo, book, args, behind=busy[0]["book"])
        return {"error": "the GPU is busy rendering {} (pid {})".format(busy[0]["book"], busy[0]["pid"])}
    dequeue(repo, book)  # starting now: no longer waiting
    state_dir.mkdir(parents=True, exist_ok=True)
    job, log, state = state_dir / "job.sh", state_dir / "job.log", state_dir / "state.json"
    if log.exists() and log.stat().st_size:
        os.replace(str(log), str(state_dir / "job.prev.log"))
    job.write_text(
        JOB_SCRIPT.format(
            repo=shlex.quote(str(repo)),
            book=shlex.quote(book),
            state=shlex.quote(str(state)),
            log=str(log),
            agent=shlex.quote(str(repo / AGENT)),
            next_args=shlex.quote(json.dumps({"repo": str(repo), "after": book})),
        )
    )
    job.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = _paths()
    env.pop("LNVOX_VOICEBANK", None)  # a queued book started by another must not inherit its voicebank
    env.update({k: str(v) for k, v in (args.get("env") or {}).items()})
    record = {
        "state": "starting",
        "book": book,
        "started": int(time.time()),
        "commit": _git(repo, "rev-parse", "HEAD"),
        "fingerprint": args.get("fingerprint", ""),
        "launched_from": args.get("launched_from", ""),
    }
    _write_json(state, record)  # before the job starts: it merges its own notes in
    with open(str(log), "ab") as out, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            ["bash", str(job)], cwd=str(repo), stdin=devnull, stdout=out, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
    record = _read_json(state) or record
    record["pid"] = proc.pid
    _write_json(state, record)
    return {"pid": proc.pid}


def cmd_stop(args: dict) -> dict:
    repo = _repo(args["repo"])
    book_dir = repo / "artifacts" / args["book"]
    state = job_state(book_dir)
    if state and state.get("state") == "queued":
        dequeue(repo, args["book"])
        state.update({"state": "stopped", "finished": int(time.time())})
        state.pop("alive", None)
        _write_json(book_dir / STATE_DIR / "state.json", state)
        return {"stopped": True, "dequeued": True}
    if not state or not state.get("alive"):
        return {"stopped": False}
    pid = state["pid"]
    try:
        os.killpg(pid, signal.SIGTERM)  # the job leads its own session: one signal reaches the render
    except ProcessLookupError:
        pass
    for _ in range(60):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    record = _read_json(book_dir / STATE_DIR / "state.json") or {}
    record.update({"state": "stopped", "finished": int(time.time())})
    _write_json(book_dir / STATE_DIR / "state.json", record)
    return {"stopped": True}


def cmd_next(args: dict) -> dict:
    """Start the first queued book that can start; a book whose launch
    fails is marked failed and the next one tried."""
    repo = _repo(args["repo"])
    while True:
        queue = load_queue(repo)
        if not queue:
            print("   (queue empty)")
            return {"launched": None}
        entry = queue[0]
        result = cmd_launch({"repo": str(repo), "book": entry["book"], "fingerprint": entry.get("fingerprint", ""),
                             "env": entry.get("env") or {}, "launched_from": entry.get("launched_from", "")})
        if result.get("pid"):
            print("   started {} (pid {})".format(entry["book"], result["pid"]))
            return {"launched": entry["book"], "pid": result["pid"]}
        print("   could not start {}: {}".format(entry["book"], result.get("error")))
        if "busy" in (result.get("error") or ""):
            return {"launched": None, "error": result["error"]}  # it waits for the next turn
        dequeue(repo, entry["book"])
        _write_json(repo / "artifacts" / entry["book"] / STATE_DIR / "state.json",
                    {"state": "failed", "book": entry["book"], "error": result.get("error"),
                     "finished": int(time.time())})


COMMANDS = {
    "probe": cmd_probe,
    "list": cmd_list,
    "delete": cmd_delete,
    "git": cmd_git,
    "launch": cmd_launch,
    "next": cmd_next,
    "stop": cmd_stop,
}


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(json.dumps({"error": "usage: python3 - <{}> '<json>'".format("|".join(COMMANDS))}))
        return 2
    args = json.loads(argv[2]) if len(argv) > 2 else {}
    print(json.dumps(COMMANDS[argv[1]](args)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

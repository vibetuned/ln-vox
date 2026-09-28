"""Process plumbing for the ln-vox ComfyUI nodes.

The nodes never import `lnvox`. ComfyUI runs in its own interpreter with its
own torch, while ln-vox needs *two* mutually incompatible venv states (the
LLM phase and the Dramabox phase pin different torch builds — see
`prepare_llm_env` / `prepare_tts_env` in scripts/run_pipeline.sh). So every
stage runs as `uv run lnvox …` inside the ln-vox checkout, exactly as the
launcher does, and this module supplies what the launcher's bash supplies:

  - `run_logged`     — run a command, stream its output live to the node
                       and the ComfyUI console, honour ComfyUI's Cancel.
  - `run_step`       — the launcher's `run_step`: bounded retries with an
                       optional pre-retry hook, no retry after a signal.
  - `ensure_server` / `stop_server`
                     — the launcher's start_vllm / ensure_vllm / stop_vllm
                       for whichever OpenAI-compatible backend is selected.
  - `prepare_env`    — delegates to `run_pipeline.sh --prepare-env <phase>`
                       so the venv swap logic lives in exactly one place.

ComfyUI-specific imports (`server`, `comfy.model_management`) are optional:
without them (unit tests, a plain Python shell) the helpers still work, they
just don't push live logs or observe the Cancel button.
"""

from __future__ import annotations

import atexit
import collections
import copy
import json
import os
import platform
import queue
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

PACK_DIR = Path(__file__).resolve().parent

# Per-backend model defaults — keep in sync with scripts/run_pipeline.sh.
DEFAULT_LLM_MODELS = {
    "llama": "google/gemma-4-12B-it-qat-q4_0-gguf",
    "vllm": "google/gemma-4-E4B-it",
    "mlx": "mlx-community/gemma-4-12B-it-qat-4bit",
}
SERVE_SCRIPTS = {
    "llama": "scripts/serve_llama.sh",
    "vllm": "scripts/serve_vllm.sh",
    "mlx": "scripts/serve_mlx.sh",
}
DEFAULT_ENDPOINT = "http://localhost:8000/v1"
SERVER_READY_TIMEOUT = 600  # seconds — same budget as start_vllm

LOG_TAIL_LINES = 60
LOG_PUSH_INTERVAL = 0.5  # seconds between live-log pushes to the browser


class LnVoxError(RuntimeError):
    """A pipeline step failed; the message is shown on the ComfyUI node."""


# ----- repo / book ----------------------------------------------------------


def find_repo(explicit: str = "") -> Path:
    """Locate the ln-vox checkout.

    Order: the Book node's `repo_path` widget, `$LNVOX_REPO`, then the
    directory this pack lives in (`<repo>/comfyui/`, resolved through the
    custom_nodes symlink).
    """
    candidates = [explicit.strip(), os.environ.get("LNVOX_REPO", "").strip(), str(PACK_DIR.parent)]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser().resolve()
        if (p / "scripts" / "run_pipeline.sh").is_file() and (p / "src" / "lnvox").is_dir():
            return p
    raise LnVoxError(
        "Could not find the ln-vox checkout. Symlink this folder into "
        "ComfyUI/custom_nodes (so it can resolve <repo>/comfyui), set "
        "$LNVOX_REPO, or fill the Book node's repo_path."
    )


def derive(book: dict, **changes) -> dict:
    """Return a deep copy of `book` with `changes` applied.

    Nodes must never mutate their input: ComfyUI caches node outputs, so an
    in-place edit would silently rewrite an upstream node's cached result.
    """
    out = copy.deepcopy(book)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def book_dir(book: dict) -> Path:
    return Path(book["repo"]) / book.get("artifacts_dir", "artifacts") / book["book_id"]


def repo_path(book: dict, path: str) -> Path:
    """A user-supplied path: absolute as given, else relative to the repo."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else Path(book["repo"]) / p


# Where uv / ffmpeg / llama-server usually live. A ComfyUI started from a
# desktop launcher often inherits a bare PATH without them.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin")


def tool_env(base: Optional[dict] = None) -> dict:
    """`base` (default: os.environ) minus ComfyUI's own Python, plus tool dirs."""
    env = dict(os.environ if base is None else base)
    # uv must resolve the ln-vox project's .venv, not ComfyUI's interpreter.
    for var in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        env.pop(var, None)
    parts = env.get("PATH", "").split(os.pathsep)
    for d in _EXTRA_BIN_DIRS:
        d = os.path.expanduser(d)
        if os.path.isdir(d) and d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(p for p in parts if p)
    return env


def require_mode(book: dict, allowed: Iterable[str], node: str) -> None:
    allowed = tuple(allowed)
    if book.get("mode") not in allowed:
        raise LnVoxError(
            f"{node} runs in {' / '.join(allowed)} mode, but this book is in "
            f"'{book.get('mode')}' mode. Change the Book node's mode or use the "
            f"matching node for this mode."
        )


def book_env(book: dict) -> dict:
    """Environment for `uv run lnvox …` — what the launcher exports."""
    env = tool_env()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("COLUMNS", "140")  # rich tables wrap at 80 without a TTY
    env["LNVOX_VOICEBANK"] = book.get("voicebank") or "voicebank"
    if book.get("artifacts_dir") and book["artifacts_dir"] != "artifacts":
        env["LNVOX_ARTIFACTS_DIR"] = book["artifacts_dir"]
    llm = book.get("llm") or {}
    if llm.get("backend"):
        # LLMClient keys Gemma's thinking-channel switch on this (client.py).
        env["LNVOX_LLM_BACKEND"] = llm["backend"]
    if llm.get("model"):
        env["LNVOX_LLM_MODEL"] = llm["model"]
    if llm.get("endpoint"):
        env["LNVOX_LLM__ENDPOINT"] = llm["endpoint"]
    if llm.get("max_model_len"):
        env["LNVOX_LLM_MAX_LEN"] = str(llm["max_model_len"])
        env["LNVOX_LLM_MAX_MODEL_LEN"] = str(llm["max_model_len"])
    for key, value in (book.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


# ----- ComfyUI hooks (optional) --------------------------------------------


def _interrupted() -> bool:
    try:
        import comfy.model_management as mm  # type: ignore

        return bool(mm.processing_interrupted())
    except Exception:
        return False


def _raise_interrupt() -> None:
    try:
        import comfy.model_management as mm  # type: ignore

        raise mm.InterruptProcessingException()
    except ImportError:
        raise KeyboardInterrupt


def push_log(node_id: Optional[str], text: str) -> None:
    """Send the live log tail to the browser (web/lnvox.js renders it)."""
    if not node_id:
        return
    try:
        from server import PromptServer  # type: ignore

        PromptServer.instance.send_sync("lnvox.log", {"node": str(node_id), "text": text})
    except Exception:
        pass


def free_comfy_vram() -> None:
    """Unload ComfyUI's own models before a TTS render claims the GPU."""
    try:
        import comfy.model_management as mm  # type: ignore

        mm.unload_all_models()
        mm.soft_empty_cache()
    except Exception:
        pass


# ----- subprocess runner ----------------------------------------------------


def _with_caffeinate(cmd: list[str], long_running: bool) -> list[str]:
    # Same reason the launcher re-execs under caffeinate: macOS power
    # management throttles/sleeps multi-hour renders.
    if long_running and platform.system() == "Darwin" and _which("caffeinate"):
        return ["caffeinate", "-dis", *cmd]
    return cmd


def _which(name: str) -> Optional[str]:
    from shutil import which

    return which(name, path=tool_env().get("PATH"))


def _kill_group(proc: subprocess.Popen, grace: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict,
    node_id: Optional[str] = None,
    title: str = "",
    log_file: Optional[Path] = None,
    long_running: bool = False,
) -> tuple[int, str]:
    """Run `cmd`, streaming merged stdout/stderr. Returns (exit code, tail).

    Output goes to the ComfyUI console (prefixed), the node's live log and,
    optionally, `log_file` (appended). Cancel in ComfyUI terminates the whole
    process group and raises ComfyUI's interrupt exception.
    """
    cmd = _with_caffeinate(cmd, long_running)
    tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
    header = f"$ {' '.join(shlex.quote(c) for c in cmd)}"
    tail.append(header)
    print(f"[ln-vox] {title or 'run'}: {header}", flush=True)

    log_fh = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_file, "a", encoding="utf-8")
        log_fh.write(f"\n==================== {time.strftime('%Y-%m-%dT%H:%M:%S')} {header}\n")

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,  # own process group → clean Cancel
    )
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    last_push = 0.0
    done_reading = False
    try:
        while True:
            try:
                line = lines.get(timeout=0.25)
                if line is None:
                    done_reading = True
                else:
                    tail.append(line)
                    print(f"[ln-vox] {line}", flush=True)
                    if log_fh:
                        log_fh.write(line + "\n")
            except queue.Empty:
                pass
            now = time.time()
            if now - last_push >= LOG_PUSH_INTERVAL:
                push_log(node_id, "\n".join(tail))
                last_push = now
            if _interrupted():
                _kill_group(proc)
                tail.append("[cancelled from ComfyUI]")
                push_log(node_id, "\n".join(tail))
                _raise_interrupt()
            if done_reading and proc.poll() is not None:
                break
            if done_reading:
                time.sleep(0.05)
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        if log_fh:
            log_fh.close()

    rc = proc.returncode
    push_log(node_id, "\n".join(tail))
    return rc, "\n".join(tail)


def _killed_by_signal(rc: int) -> bool:
    # Popen reports a signal death as -N; a shell reports it as 128+N.
    return rc < 0 or rc >= 128


def run_step(
    cmd: list[str],
    *,
    book: dict,
    desc: str,
    node_id: Optional[str] = None,
    retries: int = 3,
    retry_delay: float = 5.0,
    before_retry: Optional[Callable[[], None]] = None,
    log_file: Optional[Path] = None,
    long_running: bool = False,
    ok_codes: tuple[int, ...] = (0,),
    fatal_codes: tuple[int, ...] = (2,),
    retry_signals: bool = False,
) -> str:
    """The launcher's `run_step`, for one node.

    The stages are idempotent (finished chapters/beats are cached), so a
    retry resumes rather than redoes. A signal death is not retried — it's
    almost always an intentional stop — and neither is exit 2, which lnvox
    uses for deterministic refusals (bad option, bad knob value, a render
    path that can't work on this device) that a retry would only repeat.
    `retry_signals` makes a signal death retryable too — for the s4 render
    loop, where a SIGKILL is usually the OOM killer / CUDA fragmentation that
    s4_retry.sh restarts through (ComfyUI's own Cancel never gets here).
    """
    repo = Path(book["repo"])
    env = book_env(book)
    attempts = max(1, int(retries))
    tail = ""
    for attempt in range(1, attempts + 1):
        rc, tail = run_logged(
            cmd, cwd=repo, env=env, node_id=node_id, title=desc,
            log_file=log_file, long_running=long_running,
        )
        if rc in ok_codes:
            return tail
        if _killed_by_signal(rc) and not retry_signals:
            raise LnVoxError(f"{desc} terminated by signal (exit {rc}); not retrying.\n\n{tail}")
        if rc in fatal_codes:
            raise LnVoxError(f"{desc} refused (exit {rc}); retrying would not help.\n\n{tail}")
        if attempt >= attempts:
            raise LnVoxError(f"{desc} failed after {attempts} attempt(s) (exit {rc}).\n\n{tail}")
        print(f"[ln-vox] WARN: {desc} failed (exit {rc}). Retry {attempt}/{attempts - 1} in {retry_delay:.0f}s…", flush=True)
        _sleep_interruptibly(retry_delay)
        if before_retry is not None:
            before_retry()
    return tail


def _sleep_interruptibly(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _interrupted():
            _raise_interrupt()
        time.sleep(0.2)


def lnvox(*args: str) -> list[str]:
    return ["uv", "run", "lnvox", *args]


# ----- venv phases ----------------------------------------------------------

# Which venv state each checkout was last prepared for, so an LLM node that
# runs after a TTS node (e.g. a re-run of s3 after s4) swaps back first.
_ENV_STATE: dict[str, str] = {}
_ENV_LOCK = threading.Lock()


def prepare_env(book: dict, phase: str, node_id: Optional[str] = None) -> str:
    """Run `scripts/run_pipeline.sh --prepare-env <phase>` (llm|tts|vibevoice)."""
    repo = Path(book["repo"])
    llm = book.get("llm") or {}
    cmd = ["./scripts/run_pipeline.sh", "--prepare-env", phase]
    if llm.get("backend"):
        cmd += ["--llm-backend", llm["backend"]]
    env = book_env(book)
    env["LNVOX_CAFFEINATED"] = "1"  # no caffeinate re-exec for a short env prep
    with _ENV_LOCK:
        rc, tail = run_logged(cmd, cwd=repo, env=env, node_id=node_id, title=f"prepare {phase} env")
        if rc != 0:
            raise LnVoxError(f"Environment preparation for '{phase}' failed (exit {rc}).\n\n{tail}")
        _ENV_STATE[str(repo)] = phase
    return tail


def env_state(book: dict) -> Optional[str]:
    return _ENV_STATE.get(str(Path(book["repo"])))


# ----- LLM server lifecycle ------------------------------------------------


class _Server:
    def __init__(self, proc: subprocess.Popen, log_path: Path, backend: str, endpoint: str):
        self.proc = proc
        self.log_path = log_path
        self.backend = backend
        self.endpoint = endpoint


_SERVERS: dict[str, _Server] = {}  # keyed by endpoint
_SERVER_LOCK = threading.Lock()


def _models_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/v1/models"


def server_ready(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(_models_url(endpoint), timeout=3) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def served_models(endpoint: str) -> list[str]:
    try:
        with urllib.request.urlopen(_models_url(endpoint), timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        return []


def _log_tail(path: Path, n: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def ensure_server(book: dict, node_id: Optional[str] = None) -> None:
    """Make sure the book's LLM endpoint answers /v1/models.

    External servers are only checked. A managed one is (re)launched with the
    matching serve script — after swapping the venv back to the LLM phase if
    a TTS node prepared it for Dramabox in the meantime.
    """
    llm = book.get("llm") or {}
    if not llm:
        raise LnVoxError(
            "This node needs an LLM, but the book has no LLM configuration. "
            "Connect it after an 'ln-vox LLM Server' node."
        )
    endpoint = llm.get("endpoint") or DEFAULT_ENDPOINT
    if llm.get("external"):
        if not server_ready(endpoint):
            raise LnVoxError(f"External LLM server at {endpoint} is not responding to /v1/models.")
        return
    if server_ready(endpoint):
        return

    backend = llm.get("backend") or "llama"
    if llm.get("prepare_env", True) and env_state(book) != "llm":
        prepare_env(book, "llm", node_id=node_id)

    repo = Path(book["repo"])
    script = repo / SERVE_SCRIPTS[backend]
    env = book_env(book)
    port = urlparse(endpoint).port
    if port:
        env["LNVOX_LLM_PORT"] = str(port)

    with _SERVER_LOCK:
        if server_ready(endpoint):
            return
        old = _SERVERS.pop(endpoint, None)
        if old is not None:
            _kill_group(old.proc, grace=5)
        logs_dir = repo / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_path = logs_dir / f"comfyui_llm_{backend}.log"
        log_fh = open(log_path, "w", encoding="utf-8")
        push_log(node_id, f"Starting {backend} server ({script.name}) — log: {log_path}")
        print(f"[ln-vox] starting {backend} server: {script} (log: {log_path})", flush=True)
        proc = subprocess.Popen(
            [str(script)],
            cwd=str(repo),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log_fh.close()  # the child holds its own descriptor
        _SERVERS[endpoint] = _Server(proc, log_path, backend, endpoint)

    started = time.time()
    while not server_ready(endpoint):
        if proc.poll() is not None:
            raise LnVoxError(
                f"{backend} server died during startup (exit {proc.returncode}). "
                f"Last lines of {log_path}:\n\n{_log_tail(log_path)}"
            )
        waited = time.time() - started
        if waited >= SERVER_READY_TIMEOUT:
            stop_server(book)
            raise LnVoxError(
                f"{backend} server not ready within {SERVER_READY_TIMEOUT}s. "
                f"Last lines of {log_path}:\n\n{_log_tail(log_path)}"
            )
        if _interrupted():
            stop_server(book)
            _raise_interrupt()
        push_log(node_id, f"Waiting for {backend} server at {endpoint}… {waited:.0f}s\n\n{_log_tail(log_path, 12)}")
        time.sleep(3)
    push_log(node_id, f"{backend} server ready at {endpoint} after {time.time() - started:.0f}s")


def stop_server(book: dict) -> str:
    """Stop the managed server for this book's endpoint (no-op for external)."""
    llm = book.get("llm") or {}
    endpoint = llm.get("endpoint") or DEFAULT_ENDPOINT
    if llm.get("external"):
        return f"External server at {endpoint} left running."
    with _SERVER_LOCK:
        srv = _SERVERS.pop(endpoint, None)
    if srv is None:
        return f"No managed server at {endpoint}."
    _kill_group(srv.proc, grace=30)
    return f"Stopped {srv.backend} server at {endpoint}."


@atexit.register
def _stop_all_servers() -> None:
    for srv in list(_SERVERS.values()):
        _kill_group(srv.proc, grace=5)
    _SERVERS.clear()

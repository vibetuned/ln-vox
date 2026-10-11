"""ComfyUI nodes for the ln-vox pipeline.

Every node takes and returns an `LNVOX_BOOK` — a small dict naming the book,
its mode and the settings later stages need (LLM server, TTS backend, the mix
silences s6 must reproduce). Chaining nodes through it is what orders the
stages, exactly as scripts/run_pipeline.sh orders them:

    Book → Ingest → LLM Server → Characters (s1) → Scenes (s2) → Voice Cast
         → Casting Audit → Director (s3) → Stop LLM Server → TTS (s4)
         → Mix (s5) → EPUB Sync (s6)

Each stage node runs the matching `uv run lnvox …` command inside the ln-vox
checkout (see runtime.py for why nothing is imported in-process). ComfyUI
caches node results, so re-queueing a finished graph does nothing; bump a
node's `rerun` counter to run it (and everything after it) again — e.g. after
editing an artifact by hand. The stages themselves are idempotent and
cache-backed, so a re-run resumes rather than repeats.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import audio as audio_mod
from . import inspect_views
from . import runtime as rt
from .runtime import (
    DEFAULT_ENDPOINT,
    DEFAULT_LLM_MODELS,
    LnVoxError,
    book_dir,
    book_env,
    derive,
    ensure_server,
    find_repo,
    lnvox,
    repo_path,
    require_mode,
    run_logged,
    run_step,
    server_ready,
    served_models,
    stop_server,
)

BOOK = "LNVOX_BOOK"
MODES = ["narration", "lecture", "scenario"]
STEP_RETRIES = int(os.environ.get("STEP_RETRIES", "3"))  # same knob as the launcher

# Stage 5 silence layouts. Scenario pacing is the launcher's
# `--inter-chapter 0.75 --lead-in 0.35` (DESIGN.md §17.4).
BOOK_PACING = {"intra": 0.25, "inter_scene": 1.0, "inter_chapter": 2.0, "lead_in": 0.0}
SCENARIO_PACING = {"intra": 0.25, "inter_scene": 1.0, "inter_chapter": 0.75, "lead_in": 0.35}

RERUN = (
    "INT",
    {
        "default": 0,
        "min": 0,
        "max": 1_000_000,
        "tooltip": "Change this number to force the node (and everything after it) to run "
        "again — e.g. after editing an artifact by hand. The stage resumes from its caches.",
    },
)
HIDDEN = {"hidden": {"unique_id": "UNIQUE_ID"}}


def _done(text: str, *result):
    return {"ui": {"text": [text]}, "result": result}


def _chapters_args(chapters: str) -> list[str]:
    chapters = (chapters or "").strip()
    return ["--chapters", chapters] if chapters else []


def _llm_hook(book: dict, unique_id):
    return lambda: ensure_server(book, node_id=unique_id)


def _need_llm(book: dict, node: str, unique_id) -> None:
    if not (book.get("llm") or {}):
        raise LnVoxError(
            f"{node} calls the LLM. Put an 'ln-vox LLM Server' node before it in the chain."
        )
    ensure_server(book, node_id=unique_id)


class _Node:
    CATEGORY = "ln-vox"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = (BOOK, "STRING")
    RETURN_NAMES = ("book", "log")


# ============================================================ setup / ingest


class LnVoxBook(_Node):
    DESCRIPTION = "Start an ln-vox chain: which book, which mode, where its inputs live."
    CATEGORY = "ln-vox/setup"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book_id": ("STRING", {"default": "series/volume-01", "tooltip": "<series>/<volume-NN> (or a flat id). Artifacts go to artifacts/<book_id>/."}),
                "mode": (MODES, {"default": "narration", "tooltip": "narration: multi-voice novel. lecture: single-voice non-fiction. scenario: theater script."}),
            },
            "optional": {
                "book_title": ("STRING", {"default": "", "tooltip": "Title of the final .m4b (default: book_id)."}),
                "novels_root": ("STRING", {"default": "novels", "tooltip": "Where <book_id>/ chapter .txt folders live (relative to the repo)."}),
                "voicebank": ("STRING", {"default": "", "tooltip": "Voicebank directory (default: $LNVOX_VOICEBANK, else voicebank)."}),
                "epub": ("STRING", {"default": "", "tooltip": "Source EPUB (Stage 0a input, Stage 6 target). Default: epubs/<book_id>.epub."}),
                "scenario_file": ("STRING", {"default": "", "tooltip": "Scenario mode: the theater-script markdown."}),
                "repo_path": ("STRING", {"default": "", "tooltip": "ln-vox checkout (default: $LNVOX_REPO, else the folder this node pack lives in)."}),
            },
        }

    def run(self, book_id, mode, book_title="", novels_root="novels", voicebank="", epub="", scenario_file="", repo_path=""):
        book_id = book_id.strip().strip("/")
        if not book_id or ".." in Path(book_id).parts:
            raise LnVoxError(f"Invalid book id '{book_id}'. Use <series>/<volume-NN>.")
        repo = find_repo(repo_path)
        book = {
            "repo": str(repo),
            "book_id": book_id,
            "mode": mode,
            "title": book_title.strip() or book_id,
            "novels_root": novels_root.strip() or "novels",
            "voicebank": voicebank.strip() or os.environ.get("LNVOX_VOICEBANK", "voicebank"),
            # Same override lnvox's Settings honours (LNVOX_ARTIFACTS_DIR).
            "artifacts_dir": os.environ.get("LNVOX_ARTIFACTS_DIR", "artifacts"),
            "epub": epub.strip(),
            "scenario_file": scenario_file.strip(),
            "llm": {},
            "tts_backend": "",
            "mix": {},
            "env": {},
        }
        notes = []
        novel_dir = repo / book["novels_root"] / book_id
        if mode != "scenario" and not novel_dir.is_dir():
            notes.append(f"note: {novel_dir} doesn't exist yet — run 'Ingest EPUB (0a)' first or add chapter .txt files.")
        if mode == "scenario" and not book["scenario_file"] and not (book_dir(book) / "00_script.json").exists():
            notes.append("note: scenario mode needs scenario_file (or a prior ingest at artifacts/<id>/00_script.json).")
        vb = repo / book["voicebank"]
        if not (vb / "manifest.json").exists():
            notes.append(f"note: no manifest.json in {vb} — seed a voicebank before Voice Cast.")
        text = inspect_views.render("summary", repo, book_dir(book), book)
        if notes:
            text = "\n".join(notes) + "\n\n" + text
        return _done(text, book, text)


class LnVoxIngestEpub(_Node):
    DESCRIPTION = "Stage 0a: extract an EPUB into novels/<book_id>/ (chapter .txt, images/, .epub_meta.json)."
    CATEGORY = "ln-vox/ingest"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "epub_path": ("STRING", {"default": "", "tooltip": "Default: the Book node's epub, else epubs/<book_id>.epub."}),
                "ingest_classifier": (["fallback", "none"], {"default": "fallback", "tooltip": "Lecture mode: 'fallback' asks the LLM only about untagged blocks; 'none' is rules-only."}),
                "render": ("BOOLEAN", {"default": True, "tooltip": "Lecture mode: rasterize code/tables to PNG (needs the render extra)."}),
                "split_chapters": ("BOOLEAN", {"default": False, "tooltip": "Cut spine pages into chapters at chapter markers (DESIGN.md §18)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, epub_path="", ingest_classifier="fallback", render=True, split_chapters=False, rerun=0, unique_id=None):
        require_mode(book, ("narration", "lecture"), "Ingest EPUB")
        epub = epub_path.strip() or book.get("epub") or f"epubs/{book['book_id']}.epub"
        if not repo_path(book, epub).exists():
            raise LnVoxError(f"EPUB not found: {repo_path(book, epub)}")
        out_dir = f"{book['novels_root']}/{book['book_id']}"
        cmd = lnvox("ingest-epub", epub, out_dir, "--mode", book["mode"])
        if book["mode"] == "lecture":
            cmd += ["--ingest-classifier", ingest_classifier]
            if not render:
                cmd.append("--no-render")
            if ingest_classifier == "fallback":
                _need_llm(book, "Ingest EPUB (lecture, fallback classifier)", unique_id)
        if split_chapters:
            cmd.append("--split-chapters")
        log = run_step(cmd, book=book, desc="Stage 0a (ingest-epub)", node_id=unique_id, retries=1)
        out = derive(book, epub=book.get("epub") or epub)
        return _done(log, out, log)


class LnVoxIngest(_Node):
    DESCRIPTION = "Stage 0: parse novels/<book_id>/*.txt into artifacts/<book_id>/00_text.jsonl."
    CATEGORY = "ln-vox/ingest"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"book": (BOOK,)}, "optional": {"rerun": RERUN}, **HIDDEN}

    def run(self, book, rerun=0, unique_id=None):
        require_mode(book, ("narration", "lecture"), "Ingest")
        novel_dir = f"{book['novels_root']}/{book['book_id']}"
        if not (Path(book["repo"]) / novel_dir).is_dir():
            raise LnVoxError(f"Novel dir not found: {Path(book['repo']) / novel_dir}")
        cmd = lnvox("ingest", novel_dir, "--book-id", book["book_id"], "--mode", book["mode"])
        log = run_step(cmd, book=book, desc="Stage 0 (ingest)", node_id=unique_id, retries=STEP_RETRIES)
        return _done(log, book, log)


class LnVoxIngestScenario(_Node):
    DESCRIPTION = "Scenario Stage 0: LLM-structure a theater script → 00_script.json, 00_text.jsonl, 01_characters.json."
    CATEGORY = "ln-vox/ingest"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "scenario_file": ("STRING", {"default": "", "tooltip": "Default: the Book node's scenario_file. Leave both empty to reuse a prior ingest."}),
                "language": ("STRING", {"default": "fr", "tooltip": "Script language tag stored in 00_script.json."}),
                "no_cache": ("BOOLEAN", {"default": False, "tooltip": "Bypass cache/scenario/ to re-roll the structuring."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, scenario_file="", language="fr", no_cache=False, rerun=0, unique_id=None):
        require_mode(book, ("scenario",), "Ingest Scenario")
        md = scenario_file.strip() or book.get("scenario_file", "")
        if not md:
            if (book_dir(book) / "00_script.json").exists():
                text = "Ingest skipped: reusing existing 00_script.json (no scenario_file given)."
                return _done(text, book, text)
            raise LnVoxError("Scenario mode needs a scenario_file (or a prior ingest).")
        _need_llm(book, "Ingest Scenario", unique_id)
        cmd = lnvox("ingest-scenario", md, "--id", book["book_id"], "--language", language)
        if book.get("title") and book["title"] != book["book_id"]:
            cmd += ["--title", book["title"]]
        if no_cache:
            cmd.append("--no-cache")
        log = run_step(cmd, book=book, desc="Stage 0 (ingest-scenario)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=_llm_hook(book, unique_id))
        return _done(log, derive(book, scenario_file=md), log)


# ================================================================ LLM phase


class LnVoxLLMServer(_Node):
    DESCRIPTION = (
        "Configure (and start) the OpenAI-compatible LLM server the LLM stages talk to — "
        "llama-server, vLLM or mlx_lm.server, via scripts/serve_*.sh. Downstream nodes relaunch it if it dies."
    )
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "backend": (["llama", "vllm", "mlx"], {"default": "llama", "tooltip": "llama (llama.cpp, any OS — default), vllm (Linux+CUDA), mlx (Apple Silicon)."}),
            },
            "optional": {
                "model": ("STRING", {"default": "", "tooltip": "Blank = the backend's default (same as run_pipeline.sh)."}),
                "external_url": ("STRING", {"default": "", "tooltip": "Use an already-running server at this URL instead of launching one (the launcher's --vllm-url)."}),
                "max_model_len": ("INT", {"default": 0, "min": 0, "max": 1_048_576, "tooltip": "Context window override (0 = backend default)."}),
                "prepare_env": ("BOOLEAN", {"default": True, "tooltip": "Swap the ln-vox venv to the LLM-phase state first (run_pipeline.sh --prepare-env llm)."}),
                "start_now": ("BOOLEAN", {"default": True, "tooltip": "Start it here (fail fast). Off: start lazily on the first LLM stage."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, backend, model="", external_url="", max_model_len=0, prepare_env=True, start_now=True, rerun=0, unique_id=None):
        external = bool(external_url.strip())
        llm = {
            "backend": backend,
            "model": model.strip() or DEFAULT_LLM_MODELS[backend],
            "endpoint": external_url.strip() or DEFAULT_ENDPOINT,
            "external": external,
            "max_model_len": int(max_model_len or 0),
            "prepare_env": bool(prepare_env) and not external,
        }
        out = derive(book, llm=llm)
        out["llm"] = llm  # replace wholesale, don't merge with a previous server config
        if start_now:
            ensure_server(out, node_id=unique_id)
        state = "ready" if server_ready(llm["endpoint"]) else "not started (lazy)"
        served = ", ".join(served_models(llm["endpoint"])) if state == "ready" else "—"
        text = (
            f"backend:  {backend}{' (external)' if external else ''}\n"
            f"model:    {llm['model']}\n"
            f"endpoint: {llm['endpoint']}  [{state}]\n"
            f"serving:  {served}"
        )
        return _done(text, out, text)


class LnVoxStopLLMServer(_Node):
    DESCRIPTION = "Stop the managed LLM server to free the GPU for TTS (the launcher's hand-off between phases)."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"book": (BOOK,)}, "optional": {"rerun": RERUN}, **HIDDEN}

    def run(self, book, rerun=0, unique_id=None):
        text = stop_server(book)
        if text.startswith("Stopped"):
            time.sleep(5)  # let the driver release VRAM before Dramabox claims it
        return _done(text, book, text)


class LnVoxCharacters(_Node):
    DESCRIPTION = "Stage 1: extract and merge the cast → 01_characters.json (merges prior volumes of the series)."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, chapters="", rerun=0, unique_id=None):
        require_mode(book, ("narration",), "Characters (s1)")
        _need_llm(book, "Characters (s1)", unique_id)
        cmd = lnvox("s1", book["book_id"], *_chapters_args(chapters))
        log = run_step(cmd, book=book, desc="Stage 1 (cast extraction)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=_llm_hook(book, unique_id))
        return _done(log, book, log)


class LnVoxScenes(_Node):
    DESCRIPTION = "Stage 2: scene boundaries + narration/dialogue beats with speakers → 02_scenes/."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, chapters="", rerun=0, unique_id=None):
        require_mode(book, ("narration",), "Scenes (s2)")
        _need_llm(book, "Scenes (s2)", unique_id)
        cmd = lnvox("s2", book["book_id"], *_chapters_args(chapters))
        log = run_step(cmd, book=book, desc="Stage 2 (scene segmentation)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=_llm_hook(book, unique_id))
        return _done(log, book, log)


class LnVoxVoiceCast(_Node):
    DESCRIPTION = "Stage V: match every character (and the Narrator) to a voicebank clip → 04_voice_assignments.json."
    CATEGORY = "ln-vox/casting"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "narrator_clip": ("STRING", {"default": "", "tooltip": "Voicebank clip id for the Narrator. Blank: inherit from a prior volume, else auto-cast."}),
                "top_n": ("INT", {"default": 3, "min": 1, "max": 10, "tooltip": "Candidates ranked per character."}),
                "fresh": ("BOOLEAN", {"default": False, "tooltip": "Ignore this volume's unlocked assignments (re-cast characters with no prior-volume match). Locked castings are always kept."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, narrator_clip="", top_n=3, fresh=False, rerun=0, unique_id=None):
        narrator_clip = narrator_clip.strip()
        # Same NEED_LLM rule as the launcher: a lecture book with a hand-picked
        # narrator makes zero LLM calls in voice cast.
        needs_llm = not (book["mode"] == "lecture" and narrator_clip)
        hook = None
        if needs_llm:
            _need_llm(book, "Voice Cast", unique_id)
            hook = _llm_hook(book, unique_id)
        cmd = lnvox("voice", "cast", book["book_id"], "--top-n", str(int(top_n)))
        if narrator_clip:
            cmd += ["--narrator-clip", narrator_clip]
        if fresh:
            cmd.append("--fresh")
        log = run_step(cmd, book=book, desc="Stage V (voice cast)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=hook)
        return _done(log, book, log)


class LnVoxVoiceAudit(_Node):
    DESCRIPTION = (
        "Stage A: flag characters who share scenes but got near-identical voices; optionally reassign. "
        "Never fails the chain (a collision is a quality warning)."
    )
    CATEGORY = "ln-vox/casting"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "action": (["report", "fix", "off"], {"default": "report", "tooltip": "report, fix (reassign the lower-speaking side; locked castings are kept), or off."}),
            },
            "optional": {
                "min_semitones": ("FLOAT", {"default": 2.5, "min": 0.0, "max": 24.0, "step": 0.1, "tooltip": "Flag scene-sharing pairs closer than this in pitch."}),
                "min_shared": ("INT", {"default": 3, "min": 0, "max": 10_000, "tooltip": "Ignore pairs with less shared-scene dialogue than this."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, action, min_semitones=2.5, min_shared=3, rerun=0, unique_id=None):
        if action == "off":
            text = "Casting audit off."
            return _done(text, book, text)
        if book["mode"] == "scenario":
            text = "Casting audit skipped in scenario mode (as in run_pipeline.sh) — check the casting by ear."
            return _done(text, book, text)
        cmd = lnvox("voice", "audit", book["book_id"],
                    "--min-semitones", str(min_semitones), "--min-shared", str(int(min_shared)))
        if action == "fix":
            cmd.append("--fix")
        rc, log = run_logged(cmd, cwd=Path(book["repo"]), env=book_env(book), node_id=unique_id, title="Stage A (casting audit)")
        if rc < 0 or rc >= 128:
            raise LnVoxError(f"Casting audit terminated by signal (exit {rc}).\n\n{log}")
        return _done(log, book, log)


def _narrator_descriptor(book: dict, clip: dict | None) -> str:
    """`voices.matcher.descriptor_from_clip` for one clip, computed in the
    ln-vox venv (it depends on the matcher's accent table). "" on failure,
    which falls back to clearing — `voice cast` rebuilds it later."""
    if not clip:
        return ""
    import subprocess

    code = (
        "import sys; from lnvox.voices.matcher import descriptor_from_clip; "
        "from lnvox.voices.schema import VoiceClip; "
        "print(descriptor_from_clip(VoiceClip.model_validate_json(sys.stdin.read())))"
    )
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-c", code], input=json.dumps(clip), capture_output=True,
            text=True, cwd=book["repo"], env=book_env(book), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


class LnVoxSetVoice(_Node):
    DESCRIPTION = (
        "Hand-cast: set characters' voicebank clips in 04_voice_assignments.json (the --stop-after-audit fix-up). "
        "Run before Director (s3) so descriptors match the new voices."
    )
    CATEGORY = "ln-vox/casting"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "assignments": ("STRING", {"multiline": True, "default": "", "tooltip": "One per line:  Character Name = cv_clipid"}),
            },
            "optional": {
                "lock": ("BOOLEAN", {"default": True, "tooltip": "Mark these castings locked so 'Casting Audit: fix' leaves them alone."}),
                "note": ("STRING", {"default": "set by hand in ComfyUI", "tooltip": "Stored as audit_note on each edited casting."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, assignments, lock=True, note="set by hand in ComfyUI", rerun=0, unique_id=None):
        path = book_dir(book) / "04_voice_assignments.json"
        if not path.exists():
            raise LnVoxError(f"No {path} — run Voice Cast first.")
        edits = []
        for n, raw in enumerate(assignments.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                raise LnVoxError(f"Line {n}: expected 'Character Name = clip_id', got '{raw}'.")
            name, clip = (p.strip() for p in line.rsplit("=", 1))
            edits.append((name, clip))
        if not edits:
            text = "No assignments given — casting unchanged."
            return _done(text, book, text)

        vb_manifest = Path(book["repo"]) / book["voicebank"] / "manifest.json"
        clips: dict[str, dict] = {}
        if vb_manifest.exists():
            clips = {c["id"]: c for c in json.loads(vb_manifest.read_text(encoding="utf-8")).get("clips", [])}
        known = set(clips)
        data = json.loads(path.read_text(encoding="utf-8"))
        by_name = {c["character_name"].casefold(): c for c in data.get("castings", [])}
        lines = []
        for name, clip in edits:
            if known and clip not in known:
                raise LnVoxError(f"Clip '{clip}' is not in {vb_manifest} (voicebank '{book['voicebank']}').")
            entry = by_name.get(name.casefold())
            if entry is None:
                names = ", ".join(sorted(c["character_name"] for c in data.get("castings", [])))
                raise LnVoxError(f"No casting for '{name}'. Known characters: {names}")
            old = entry.get("assigned_clip_id") or "(none)"
            changed = old != clip
            entry["assigned_clip_id"] = clip
            entry["locked"] = bool(lock)
            if note:
                entry["audit_note"] = note
            extra = ""
            if changed and entry.get("voice_descriptor"):
                # A non-empty descriptor is used verbatim downstream, so one
                # written for the old clip would contradict the new voice.
                # Same rule as the Studio's recast(): the Narrator gets a
                # fresh clip-derived one (lecture mode never composes one),
                # anyone else is cleared so s3 composes one for this clip.
                derived = _narrator_descriptor(book, clips.get(clip)) if entry["character_name"] == "Narrator" else ""
                entry["voice_descriptor"] = derived
                extra = ", descriptor re-derived" if derived else ", old descriptor cleared"
            lines.append(f"{entry['character_name']}: {old} → {clip}{' (locked)' if lock else ''}{extra}")
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        text = "\n".join(lines) + f"\n\nWrote {path}. Re-run Casting Audit to re-check, then Director (s3)."
        return _done(text, book, text)


# ============================================================ direction


class LnVoxDirector(_Node):
    DESCRIPTION = "Stage 3 (narration): per-beat voice + performance directions matched to the cast clips → 03_directed/."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "regen_profiles": ("BOOLEAN", {"default": False, "tooltip": "Force-regenerate EVERY voice descriptor. Not needed after a recast: recast characters' profiles refresh on their own and their chapters are re-voiced. Forcing re-voices every chapter, so s4 re-renders the book."}),
                "fresh_directions": ("BOOLEAN", {"default": False, "tooltip": "Delete the selected chapters' 03_directed/ files first to get fresh performance cues from the LLM (a recast doesn't need this)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, chapters="", regen_profiles=False, fresh_directions=False, rerun=0, unique_id=None):
        require_mode(book, ("narration",), "Director (s3)")
        _need_llm(book, "Director (s3)", unique_id)
        removed = []
        if fresh_directions:
            wanted = {c.strip() for c in chapters.split(",") if c.strip()}
            for path in sorted((book_dir(book) / "03_directed").glob("*.json")):
                if not wanted or json.loads(path.read_text(encoding="utf-8")).get("chapter_id") in wanted:
                    path.unlink()
                    removed.append(path.name)
        cmd = lnvox("s3", book["book_id"], *_chapters_args(chapters))
        if regen_profiles:
            cmd.append("--regen-profiles")
        log = run_step(cmd, book=book, desc="Stage 3 (director)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=_llm_hook(book, unique_id))
        if removed:
            log = f"Re-directed {len(removed)} chapter(s): {', '.join(removed)}\n\n" + log
        return _done(log, book, log)


class LnVoxLecture(_Node):
    DESCRIPTION = "Stage L (lecture): split prose into narration beats + speech-normalize → 03_directed/ (replaces s1/s2/s3)."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "normalize": ("BOOLEAN", {"default": True, "tooltip": "LLM speech-normalize pass ('1973' → 'nineteen seventy-three'). Off: read verbatim, no LLM."}),
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, normalize=True, chapters="", rerun=0, unique_id=None):
        require_mode(book, ("lecture",), "Lecture (L)")
        hook = None
        if normalize:
            _need_llm(book, "Lecture (speech-normalize)", unique_id)
            hook = _llm_hook(book, unique_id)
        cmd = lnvox("lecture", book["book_id"], *_chapters_args(chapters))
        if not normalize:
            cmd.append("--no-normalize")
        log = run_step(cmd, book=book, desc="Stage L (lecture)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=hook)
        return _done(log, book, log)


class LnVoxScenarioDirection(_Node):
    DESCRIPTION = "Stage S (scenario): per-line emotion + cue in the script's language → 03_directed/."
    CATEGORY = "ln-vox/llm"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "regen_profiles": ("BOOLEAN", {"default": False, "tooltip": "Force-regenerate EVERY voice descriptor (recast characters refresh on their own)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, regen_profiles=False, rerun=0, unique_id=None):
        require_mode(book, ("scenario",), "Scenario Direction (S)")
        _need_llm(book, "Scenario Direction", unique_id)
        cmd = lnvox("scenario", book["book_id"])
        if regen_profiles:
            cmd.append("--regen-profiles")
        log = run_step(cmd, book=book, desc="Stage S (scenario)", node_id=unique_id,
                       retries=STEP_RETRIES, before_retry=_llm_hook(book, unique_id))
        return _done(log, book, log)


# ================================================================ TTS / mix


class LnVoxTTS(_Node):
    DESCRIPTION = (
        "Stage 4: render every directed beat. Dramabox (staged by default, like the launcher) → 05_audio/, "
        "or VibeVoice sessions → 05_audio_v2/. Stops a managed LLM server first."
    )
    CATEGORY = "ln-vox/tts"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "backend": (["dramabox", "vibevoice"], {"default": "dramabox"}),
                "staged": ("BOOLEAN", {"default": True, "tooltip": "Dramabox only: crash-isolated staged phases with built-in resume (DESIGN.md §15). Off: monolithic + retry loop."}),
                "device": (["auto", "cuda", "mps", "cpu"], {"default": "auto"}),
            },
            "optional": {
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "limit": ("INT", {"default": 0, "min": 0, "max": 1_000_000, "tooltip": "Render at most N beats (smoke test). 0 = all."}),
                "no_trim": ("BOOLEAN", {"default": False, "tooltip": "Keep engine-emitted edge silence (trim is on by default)."}),
                "max_retries": ("INT", {"default": 30, "min": 1, "max": 1000, "tooltip": "Monolithic / VibeVoice: restarts before giving up (the launcher's --max-retries)."}),
                "max_session_chars": ("INT", {"default": 3000, "min": 200, "max": 100_000, "tooltip": "VibeVoice only: characters per generation session."}),
                "keep_staged": ("BOOLEAN", {"default": False, "tooltip": "Staged only: keep _staged/ intermediates (debugging)."}),
                "prepare_env": ("BOOLEAN", {"default": True, "tooltip": "Swap the venv to the TTS state first (run_pipeline.sh --prepare-env tts|vibevoice)."}),
                "free_comfy_vram": ("BOOLEAN", {"default": True, "tooltip": "Unload ComfyUI's own models before rendering."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, backend, staged, device, chapters="", limit=0, no_trim=False, max_retries=30,
            max_session_chars=3000, keep_staged=False, prepare_env=True, free_comfy_vram=True, rerun=0, unique_id=None):
        notes = []
        if (book.get("llm") or {}) and not book["llm"].get("external"):
            notes.append(stop_server(book))
            if notes[-1].startswith("Stopped"):
                time.sleep(5)
        if free_comfy_vram:
            rt.free_comfy_vram()
        if prepare_env:
            rt.prepare_env(book, "vibevoice" if backend == "vibevoice" else "tts", node_id=unique_id)

        args = [book["book_id"], *_chapters_args(chapters)]
        if device != "auto":
            args += ["--device", device]
        if limit:
            args += ["--limit", str(int(limit))]
        if no_trim:
            args.append("--no-trim")

        log_file = book_dir(book) / "_s4_full.log"
        if backend == "dramabox" and staged:
            if keep_staged:
                args.append("--keep-staged")
            # The staged driver owns crash retries (per-phase subprocesses with
            # file-level resume, DESIGN.md §15.4): no outer loop.
            log = run_step(lnvox("s4", *args, "--staged"), book=book, desc="Stage 4 (Dramabox, staged)",
                           node_id=unique_id, retries=1, log_file=log_file, long_running=True)
        else:
            if backend == "vibevoice":
                if staged:
                    notes.append("staged ignored: VibeVoice is a single-model boot (DESIGN.md §16.2).")
                args += ["--tts-backend", "vibevoice", "--max-session-chars", str(int(max_session_chars))]
            # s4_retry.sh semantics: restart until clean; the content-hash
            # cache makes each restart a resume.
            log = run_step(lnvox("s4", *args), book=book, desc=f"Stage 4 ({backend})", node_id=unique_id,
                           retries=max_retries, log_file=log_file, long_running=True, retry_signals=True)
        text = ("\n".join(notes) + "\n\n" if notes else "") + log
        return _done(text, derive(book, tts_backend=backend), text)


class LnVoxMix(_Node):
    DESCRIPTION = "Stage 5: concatenate beats with silences, loudness-normalize, AAC-encode → 06_final/<title>.m4b with chapters + cover."
    CATEGORY = "ln-vox/output"
    RETURN_TYPES = (BOOK, "STRING", "STRING")
    RETURN_NAMES = ("book", "m4b_path", "log")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "pacing": (["auto", "custom"], {"default": "auto", "tooltip": "auto: book pacing (0.25/1/2 s), or play pacing in scenario mode (inter-chapter 0.75, lead-in 0.35). custom: the four values below."}),
            },
            "optional": {
                "intra": ("FLOAT", {"default": 0.25, "min": 0.0, "max": 10.0, "step": 0.05}),
                "inter_scene": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.05}),
                "inter_chapter": ("FLOAT", {"default": 2.0, "min": 0.0, "max": 30.0, "step": 0.05}),
                "lead_in": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05}),
                "lufs": ("FLOAT", {"default": -18.0, "min": -40.0, "max": -5.0, "step": 0.5}),
                "kbps": ("INT", {"default": 96, "min": 32, "max": 320}),
                "cover": ("STRING", {"default": "", "tooltip": "Cover image (default: from 00_book_meta.json)."}),
                "images_dir": ("STRING", {"default": "", "tooltip": "Extra images to embed (default: novels/<book>/images/)."}),
                "chapters": ("STRING", {"default": "", "tooltip": "Comma-separated chapter ids (default: all)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, pacing, intra=0.25, inter_scene=1.0, inter_chapter=2.0, lead_in=0.0, lufs=-18.0, kbps=96,
            cover="", images_dir="", chapters="", rerun=0, unique_id=None):
        if pacing == "auto":
            mix = dict(SCENARIO_PACING if book["mode"] == "scenario" else BOOK_PACING)
        else:
            mix = {"intra": intra, "inter_scene": inter_scene, "inter_chapter": inter_chapter, "lead_in": lead_in}
        mix["chapters"] = (chapters or "").strip()
        v2 = book.get("tts_backend") == "vibevoice" or (
            not (book_dir(book) / "05_audio").exists() and (book_dir(book) / "05_audio_v2").exists()
        )
        cmd = lnvox(
            "s5", book["book_id"], "--title", book["title"],
            "--intra", str(mix["intra"]), "--inter-scene", str(mix["inter_scene"]),
            "--inter-chapter", str(mix["inter_chapter"]), "--lead-in", str(mix["lead_in"]),
            "--lufs", str(lufs), "--kbps", str(int(kbps)), "--novels-root", book["novels_root"],
            *_chapters_args(chapters),
        )
        if v2:
            cmd.append("--v2")
        if cover.strip():
            cmd += ["--cover", cover.strip()]
        if images_dir.strip():
            cmd += ["--images-dir", images_dir.strip()]
        log = run_step(cmd, book=book, desc="Stage 5 (mix)", node_id=unique_id, retries=STEP_RETRIES)
        safe = book["title"].replace("/", "_").replace("\\", "_")
        m4b = str(book_dir(book) / "06_final" / f"{safe}.m4b")
        return _done(log, derive(book, mix=mix), m4b, log)


class LnVoxSync(_Node):
    DESCRIPTION = "Stage 6: re-align beats onto the source EPUB → synced EPUB + sync_manifest.json, timed from the mix's own layout sidecar."
    CATEGORY = "ln-vox/output"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"book": (BOOK,)},
            "optional": {
                "epub": ("STRING", {"default": "", "tooltip": "Source EPUB (default: the Book node's epub, else epubs/<book_id>.epub)."}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, epub="", rerun=0, unique_id=None):
        if book["mode"] == "scenario":
            raise LnVoxError("Scenario mode has its own timeline — use 'ln-vox Scenario Sync' instead of EPUB Sync.")
        bdir = book_dir(book)
        if book.get("tts_backend") == "vibevoice":
            text = "Sync skipped: VibeVoice v2 audio has no per-beat timing (DESIGN.md §16.10)."
            return _done(text, book, text)
        epub_path = epub.strip() or book.get("epub") or f"epubs/{book['book_id']}.epub"
        if not repo_path(book, epub_path).exists():
            text = f"Sync skipped: no source EPUB at {epub_path}. Set the Book node's epub to enable it."
            return _done(text, book, text)
        if not (bdir / "05_audio").is_dir():
            text = f"Sync skipped: no rendered audio at {bdir / '05_audio'}."
            return _done(text, book, text)
        # s6 reads 06_final/<title>.timings.json — the pads, lead-in, chapter
        # list and embedded pictures of that exact mix — so naming the mix is
        # all it needs; no silence flags to keep in step.
        cmd = lnvox("s6", book["book_id"], "--epub", epub_path, "--novels-root", book["novels_root"],
                    "--title", book["title"])
        log = run_step(cmd, book=book, desc="Stage 6 (sync)", node_id=unique_id, retries=STEP_RETRIES)
        return _done(log, book, log)


class LnVoxScenarioSync(_Node):
    DESCRIPTION = "Scenario Stage Y: timed sync files (07_sync/<scene>.json, play.json, play.srt), laid out from the mix's timings sidecar."
    CATEGORY = "ln-vox/output"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"book": (BOOK,)}, "optional": {"rerun": RERUN}, **HIDDEN}

    def run(self, book, rerun=0, unique_id=None):
        require_mode(book, ("scenario",), "Scenario Sync")
        if book.get("tts_backend") == "vibevoice":
            text = "Scenario sync skipped: VibeVoice sessions have no per-line timing (DESIGN.md §17.4)."
            return _done(text, book, text)
        # Like s6, scenario-sync follows the mix's timings sidecar (pads,
        # lead-in, scene list), so the play timeline and the m4b agree.
        cmd = lnvox("scenario-sync", book["book_id"], "--title", book["title"])
        log = run_step(cmd, book=book, desc="Scenario sync", node_id=unique_id, retries=STEP_RETRIES)
        return _done(log, book, log)


# ========================================================= one-shot launcher


class LnVoxRunPipeline(_Node):
    DESCRIPTION = "Run scripts/run_pipeline.sh end-to-end for the book — the exact CLI behaviour, as one node."
    CATEGORY = "ln-vox/setup"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "llm_backend": (["llama", "vllm", "mlx"], {"default": "llama"}),
                "tts_backend": (["dramabox", "vibevoice"], {"default": "dramabox"}),
            },
            "optional": {
                "llm_model": ("STRING", {"default": ""}),
                "vllm_url": ("STRING", {"default": "", "tooltip": "Already-running LLM server URL (skips auto start/stop)."}),
                "max_model_len": ("INT", {"default": 0, "min": 0, "max": 1_048_576}),
                "narrator_clip": ("STRING", {"default": ""}),
                "audit_cast": (["report", "fix", "off"], {"default": "report"}),
                "stop_after_audit": ("BOOLEAN", {"default": False, "tooltip": "Stop after the casting audit, before s3 — the hand-casting checkpoint."}),
                "no_normalize": ("BOOLEAN", {"default": False, "tooltip": "Lecture mode: skip the speech-normalize LLM pass."}),
                "staged_tts": ("BOOLEAN", {"default": True, "tooltip": "Dramabox staged s4 (launcher default). Off sets LNVOX_S4_STAGED=0."}),
                "skip_llm": ("BOOLEAN", {"default": False}),
                "skip_tts": ("BOOLEAN", {"default": False}),
                "skip_mix": ("BOOLEAN", {"default": False}),
                "skip_sync": ("BOOLEAN", {"default": False}),
                "max_retries": ("INT", {"default": 30, "min": 1, "max": 1000}),
                "step_retries": ("INT", {"default": 3, "min": 1, "max": 100}),
                "rerun": RERUN,
            },
            **HIDDEN,
        }

    def run(self, book, llm_backend, tts_backend, llm_model="", vllm_url="", max_model_len=0, narrator_clip="",
            audit_cast="report", stop_after_audit=False, no_normalize=False, staged_tts=True, skip_llm=False,
            skip_tts=False, skip_mix=False, skip_sync=False, max_retries=30, step_retries=3, rerun=0, unique_id=None):
        cmd = rt.script_cmd(
            "./scripts/run_pipeline.sh", book["book_id"],
            "--mode", book["mode"], "--book-title", book["title"], "--novels-root", book["novels_root"],
            "--llm-backend", llm_backend, "--tts-backend", tts_backend, "--audit-cast", audit_cast,
            "--max-retries", str(int(max_retries)), "--step-retries", str(int(step_retries)),
        )
        for flag, value in (("--llm-model", llm_model), ("--vllm-url", vllm_url), ("--narrator-clip", narrator_clip),
                            ("--epub", book.get("epub", "")), ("--scenario-file", book.get("scenario_file", ""))):
            if value and value.strip():
                cmd += [flag, value.strip()]
        if max_model_len:
            cmd += ["--max-model-len", str(int(max_model_len))]
        for flag, on in (("--stop-after-audit", stop_after_audit), ("--no-normalize", no_normalize),
                         ("--skip-llm", skip_llm), ("--skip-tts", skip_tts), ("--skip-mix", skip_mix),
                         ("--skip-sync", skip_sync)):
            if on:
                cmd.append(flag)
        run_book = derive(book)
        # The launcher honours LNVOX_LLM_MODEL / LNVOX_LLM__ENDPOINT from the
        # environment; don't let an upstream LLM Server node's settings leak
        # in and contradict this node's own llm_backend / llm_model / vllm_url.
        run_book["llm"] = {}
        if not staged_tts:
            run_book["env"]["LNVOX_S4_STAGED"] = "0"
        if not skip_tts:
            rt.free_comfy_vram()
        # The launcher re-execs itself under caffeinate on macOS; no wrapper here.
        log = run_step(cmd, book=run_book, desc="run_pipeline.sh", node_id=unique_id, retries=1)
        out = derive(book, tts_backend=tts_backend,
                     mix=dict(SCENARIO_PACING if book["mode"] == "scenario" else BOOK_PACING))
        return _done(log, out, log)


# ================================================================= viewers


class LnVoxInspect(_Node):
    DESCRIPTION = "Show a book's artifacts as text: stage status, cast, casting, scenes, directed prompts, audio, final m4b, sync, voicebank."
    CATEGORY = "ln-vox/tools"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "view": (list(inspect_views.VIEWS), {"default": "summary"}),
            },
            "optional": {
                "filter": ("STRING", {"default": "", "tooltip": "Narrow the view: a chapter id, speaker, character name, or voicebank terms like 'female adult england'."}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")  # always re-read the disk

    def run(self, book, view, filter=""):
        text = inspect_views.render(view, Path(book["repo"]), book_dir(book), book, filter)
        return _done(text, text)


class LnVoxLoadAudio(_Node):
    DESCRIPTION = "Load ln-vox audio as ComfyUI AUDIO (windowed): the final m4b (optionally from a chapter), a rendered beat, a voicebank clip, or any file."
    CATEGORY = "ln-vox/tools"
    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "path")
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "book": (BOOK,),
                "source": (list(audio_mod.SOURCES), {"default": "final_m4b"}),
            },
            "optional": {
                "item": ("STRING", {"default": "", "tooltip": "beat: beat id (02_s1_b0003) or an index with chapter_id. voicebank_clip: clip id. file: a path. final_m4b: optional file name."}),
                "chapter_id": ("STRING", {"default": "", "tooltip": "final_m4b: seek to this chapter's start. beat: restrict to this chapter."}),
                "start_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 360_000.0, "step": 1.0}),
                "duration_seconds": ("FLOAT", {"default": 60.0, "min": 0.0, "max": 36_000.0, "step": 1.0, "tooltip": "0 = to the end (careful with whole books)."}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")  # the file on disk may have been re-rendered

    def run(self, book, source, item="", chapter_id="", start_seconds=0.0, duration_seconds=60.0):
        path, offset = audio_mod.resolve(book, source, item, chapter_id)
        audio = audio_mod.decode(path, start=offset + float(start_seconds), duration=float(duration_seconds))
        return (audio, str(path))


NODE_CLASS_MAPPINGS = {
    "LnVoxBook": LnVoxBook,
    "LnVoxIngestEpub": LnVoxIngestEpub,
    "LnVoxIngest": LnVoxIngest,
    "LnVoxIngestScenario": LnVoxIngestScenario,
    "LnVoxLLMServer": LnVoxLLMServer,
    "LnVoxCharacters": LnVoxCharacters,
    "LnVoxScenes": LnVoxScenes,
    "LnVoxVoiceCast": LnVoxVoiceCast,
    "LnVoxVoiceAudit": LnVoxVoiceAudit,
    "LnVoxSetVoice": LnVoxSetVoice,
    "LnVoxDirector": LnVoxDirector,
    "LnVoxLecture": LnVoxLecture,
    "LnVoxScenarioDirection": LnVoxScenarioDirection,
    "LnVoxStopLLMServer": LnVoxStopLLMServer,
    "LnVoxTTS": LnVoxTTS,
    "LnVoxMix": LnVoxMix,
    "LnVoxSync": LnVoxSync,
    "LnVoxScenarioSync": LnVoxScenarioSync,
    "LnVoxRunPipeline": LnVoxRunPipeline,
    "LnVoxInspect": LnVoxInspect,
    "LnVoxLoadAudio": LnVoxLoadAudio,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LnVoxBook": "ln-vox Book",
    "LnVoxIngestEpub": "ln-vox Ingest EPUB (0a)",
    "LnVoxIngest": "ln-vox Ingest (0)",
    "LnVoxIngestScenario": "ln-vox Ingest Scenario (0)",
    "LnVoxLLMServer": "ln-vox LLM Server",
    "LnVoxCharacters": "ln-vox Characters (s1)",
    "LnVoxScenes": "ln-vox Scenes (s2)",
    "LnVoxVoiceCast": "ln-vox Voice Cast (V)",
    "LnVoxVoiceAudit": "ln-vox Casting Audit (A)",
    "LnVoxSetVoice": "ln-vox Set Voice",
    "LnVoxDirector": "ln-vox Director (s3)",
    "LnVoxLecture": "ln-vox Lecture (L)",
    "LnVoxScenarioDirection": "ln-vox Scenario Direction (S)",
    "LnVoxStopLLMServer": "ln-vox Stop LLM Server",
    "LnVoxTTS": "ln-vox TTS (s4)",
    "LnVoxMix": "ln-vox Mix m4b (s5)",
    "LnVoxSync": "ln-vox EPUB Sync (s6)",
    "LnVoxScenarioSync": "ln-vox Scenario Sync (Y)",
    "LnVoxRunPipeline": "ln-vox Run Pipeline (launcher)",
    "LnVoxInspect": "ln-vox Inspect",
    "LnVoxLoadAudio": "ln-vox Load Audio",
}

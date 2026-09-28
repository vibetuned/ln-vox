"""Decode ln-vox audio into ComfyUI's AUDIO type.

ComfyUI passes audio between nodes as
`{"waveform": Tensor[batch, channels, samples], "sample_rate": int}`. Books are
hours long (a 9 h m4b is ~12 GB as float32), so decoding is always windowed:
ffmpeg seeks and cuts, and only that window becomes a tensor. ffmpeg is
already a hard dependency of Stage 5.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .runtime import LnVoxError, book_dir, tool_env

SOURCES = ("final_m4b", "beat", "voicebank_clip", "file")


def _probe(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels", "-of", "json", str(path),
        ],
        capture_output=True, text=True, env=tool_env(),
    )
    if proc.returncode != 0:
        raise LnVoxError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        raise LnVoxError(f"No audio stream in {path}")
    return int(streams[0]["sample_rate"]), int(streams[0]["channels"])


def decode(path: Path, start: float = 0.0, duration: float = 0.0) -> dict:
    """ffmpeg-decode `path` (optionally a [start, start+duration) window)."""
    import numpy as np
    import torch

    if not path.exists():
        raise LnVoxError(f"Audio file not found: {path}")
    sample_rate, channels = _probe(path)
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, env=tool_env())
    if proc.returncode != 0:
        raise LnVoxError(f"ffmpeg failed on {path}: {proc.stderr.decode(errors='replace').strip()}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise LnVoxError(f"Decoded no audio from {path} (start={start}s is past the end?)")
    samples = samples[: samples.size - samples.size % channels].reshape(-1, channels).T.copy()
    waveform = torch.from_numpy(samples).unsqueeze(0)  # [1, C, T]
    return {"waveform": waveform, "sample_rate": sample_rate}


def resolve(book: dict, source: str, item: str, chapter_id: str) -> tuple[Path, float]:
    """Map a Load Audio selection to (file, extra start offset in seconds)."""
    repo = Path(book["repo"])
    bdir = book_dir(book)
    item = (item or "").strip()
    chapter_id = (chapter_id or "").strip()

    if source == "final_m4b":
        m4bs = sorted((bdir / "06_final").glob("*.m4b"))
        if not m4bs:
            raise LnVoxError(f"No .m4b under {bdir / '06_final'} — run Mix (s5) first.")
        title = (book.get("title") or "").replace("/", "_").replace("\\", "_")
        m4b = next((p for p in m4bs if p.stem == title), m4bs[0])
        if item:
            m4b = next((p for p in m4bs if p.name == item or p.stem == item), m4b)
        offset = 0.0
        if chapter_id:
            timings = m4b.with_suffix(".timings.json")
            if not timings.exists():
                raise LnVoxError(f"{timings.name} missing — can't seek to chapter {chapter_id}.")
            chapters = json.loads(timings.read_text(encoding="utf-8")).get("chapters", [])
            hit = next((c for c in chapters if c.get("chapter_id") == chapter_id), None)
            if hit is None:
                ids = ", ".join(c.get("chapter_id", "") for c in chapters)
                raise LnVoxError(f"Chapter '{chapter_id}' not in {timings.name} (have: {ids}).")
            offset = float(hit.get("start_seconds", 0.0))
        return m4b, offset

    if source == "beat":
        roots = [bdir / "05_audio", bdir / "05_audio_v2"]
        manifests = [m for r in roots for m in sorted(r.glob("*/manifest.json"))]
        if chapter_id:
            manifests = [m for m in manifests if m.parent.name == chapter_id]
        if not manifests:
            raise LnVoxError("No rendered beats found — run TTS (s4) first.")
        for m in manifests:
            data = json.loads(m.read_text(encoding="utf-8"))
            beats = data.get("beats", [])
            if not item:
                if beats:
                    return bdir / beats[0]["wav_path"], 0.0
                continue
            if item.isdigit() and chapter_id:
                idx = int(item)
                if idx >= len(beats):
                    raise LnVoxError(f"Chapter {chapter_id} has {len(beats)} beat(s); index {idx} is out of range.")
                return bdir / beats[idx]["wav_path"], 0.0
            for b in beats:
                if b.get("beat_id") == item:
                    return bdir / b["wav_path"], 0.0
        raise LnVoxError(f"Beat '{item}' not found (give a beat id like 02_s1_b0003, or chapter_id + an index).")

    if source == "voicebank_clip":
        if not item:
            raise LnVoxError("Give a voicebank clip id (see Inspect → voicebank).")
        vb = repo / (book.get("voicebank") or "voicebank")
        manifest = vb / "manifest.json"
        if manifest.exists():
            for c in json.loads(manifest.read_text(encoding="utf-8")).get("clips", []):
                if c.get("id") == item:
                    return vb / c["clip_path"], 0.0
        guess = vb / "clips" / f"{item}.wav"
        if guess.exists():
            return guess, 0.0
        raise LnVoxError(f"Clip '{item}' not in {manifest}.")

    if source == "file":
        if not item:
            raise LnVoxError("Give a file path (absolute, or relative to the ln-vox repo).")
        p = Path(item).expanduser()
        return (p if p.is_absolute() else repo / p), 0.0

    raise LnVoxError(f"Unknown source '{source}'.")

"""Thin wrapper around Dramabox's TTSServer.

Dramabox is not pip-installable; it must be cloned to `external/DramaBox/`
(use `scripts/setup_dramabox.sh`). We import its `src` package via sys.path
injection so the rest of the codebase doesn't have to care where it lives.

On first construction this calls Dramabox's bundled `model_downloader.get_all_paths()`
which fetches three artifacts from HuggingFace:
  - `ResembleAI/Dramabox`            (dit-v1 + audio-components)
  - `unsloth/gemma-3-12b-it-bnb-4bit` (the prompt-conditioning text encoder)
  - `nvidia/RE-USE`                   (vocoder code + weights, fetched lazily by Dramabox itself)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


# Project root → src/lnvox/tts/dramabox_client.py is 3 levels deep under it.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DRAMABOX_ROOT = _PROJECT_ROOT / "external" / "DramaBox"


def _ensure_path() -> None:
    if not _DRAMABOX_ROOT.exists():
        raise RuntimeError(
            f"Dramabox not found at {_DRAMABOX_ROOT}.\n"
            "Run `scripts/setup_dramabox.sh` to clone the repo and install its deps."
        )
    # Both `src` and the repo root need to be importable. The `src` package
    # uses bare imports like `from model_downloader import ...`, so we add
    # the inner `src/` directory.
    src = str(_DRAMABOX_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    root = str(_DRAMABOX_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _patch_torchaudio_save() -> None:
    """Bypass torchaudio.save's torchcodec routing.

    From torchaudio 2.9 onwards `torchaudio.save()` defaults to
    `save_with_torchcodec`, which requires the `torchcodec` package AND a
    matching ffmpeg ABI on the system. We can't pin a single torchaudio
    version that satisfies BOTH vLLM (needs ≥2.10) AND Dramabox's
    requirements.txt (pins ==2.8). To stay version-agnostic we swap in a
    soundfile-based shim that writes WAVs directly, no torchcodec needed.

    Idempotent — calling twice has no effect.
    """
    import torchaudio
    if getattr(torchaudio, "_lnvox_save_patched", False):
        return

    import numpy as np
    import soundfile as sf
    import torch

    def _save_shim(uri, src, sample_rate, *args, **kwargs):
        # torchaudio.save signature: save(uri, src, sample_rate=, channels_first=True, ...)
        channels_first = kwargs.get("channels_first", True)
        if isinstance(src, torch.Tensor):
            arr = src.detach().cpu().numpy()
        else:
            arr = np.asarray(src)
        # Soundfile wants (frames, channels). torchaudio default is
        # (channels, frames) (`channels_first=True`).
        if arr.ndim == 2 and channels_first:
            arr = arr.T
        sf.write(str(uri), arr, int(sample_rate))

    torchaudio.save = _save_shim  # type: ignore[assignment]
    torchaudio._lnvox_save_patched = True  # type: ignore[attr-defined]


# DramaBox's downloader only ever fetches the PRE-QUANTIZED encoder
# (unsloth/gemma-3-12b-it-bnb-4bit). Its packed 4-bit weights are stored as
# flattened [N, 1] uint8 blobs that only bitsandbytes (CUDA-only) can load —
# on the bnb_4bit=False path (MPS) they fail with per-layer size-mismatch
# errors. Upstream's own `--no-bnb-4bit` help text says to point
# `--gemma-root` at an unquantized checkpoint, so that's what we do:
# unsloth's ungated fp16/bf16 mirror of the same model (~24 GB, one-time
# download into the standard HF cache).
_GEMMA_UNQUANTIZED_REPO = "unsloth/gemma-3-12b-it"


def resolve_gemma_root(default_root: str, use_bnb_4bit: bool) -> str:
    """Pick the Gemma encoder checkpoint that matches the bnb setting."""
    if use_bnb_4bit:
        return default_root
    from huggingface_hub import snapshot_download

    return snapshot_download(_GEMMA_UNQUANTIZED_REPO)


# Render-quality knobs. The Mac-path recipe (unquantized bf16 encoder, 22 s
# chunk cap, fp32 vocoder) beat the original recipe in a blind A/B on CUDA —
# no audible artifacts AND ~20% faster denoise (MAC_TEST_REPORT.md) — and is
# the DEFAULT for the staged path since 2026-08-04. Env overrides:
#
#   LNVOX_S4_ENCODER_PRECISION  bf16 (default: unquantized encoder, one-time
#                               ~23 GB download) | bnb4 (old quantized
#                               encoder — lighter on disk/VRAM, audibly
#                               worse; still the MONOLITHIC-path default,
#                               which can't fit the bf16 encoder alongside
#                               the DiT + vocoder in one process)
#   LNVOX_S4_CHUNK_CAP          seconds (default 22, target = cap - 4, with
#                               word-split fallback) | 0/none/off to restore
#                               the chunker's 45 s (staged only)
#   LNVOX_S4_DECODE_DTYPE       fp32 (default) | auto = per-device knobs,
#                               the old behavior (staged only)
#
# Cache keys: s4_variant_tag() encodes deviations from the ORIGINAL v1
# recipe, NOT from the current defaults — so the tag is stable across
# default flips. Default staged runs are tagged +enc-bf16+cap22+dec-fp32
# (byte-identical keys to renders made when these were opt-in flags),
# old-recipe renders stay untagged, and MODEL_VERSION never needs a
# compatibility bump.


def s4_encoder_precision(staged: bool = True) -> str:
    val = os.environ.get("LNVOX_S4_ENCODER_PRECISION", "").strip().lower()
    if not val:
        return "bf16" if staged else "bnb4"
    if val not in ("bnb4", "bf16"):
        raise ValueError(
            f"LNVOX_S4_ENCODER_PRECISION must be 'bnb4' or 'bf16' (got {val!r})"
        )
    return val


def s4_chunk_cap() -> float | None:
    val = os.environ.get("LNVOX_S4_CHUNK_CAP", "").strip().lower()
    if val in ("0", "none", "off"):
        return None
    if not val:
        return 22.0
    return float(val)


def s4_decode_dtype() -> str | None:
    val = os.environ.get("LNVOX_S4_DECODE_DTYPE", "").strip().lower()
    if val == "auto":
        return None
    if not val:
        return "fp32"
    if val != "fp32":
        raise ValueError(
            f"LNVOX_S4_DECODE_DTYPE must be 'fp32' or 'auto' (got {val!r})"
        )
    return val


def s4_variant_tag(staged: bool = True) -> str:
    """Cache-key suffix for the effective render recipe, relative to the
    original v1 recipe (empty = v1). Stable across default changes."""
    parts = []
    if s4_encoder_precision(staged) != "bnb4":
        parts.append(f"enc-{s4_encoder_precision(staged)}")
    if staged:
        cap = s4_chunk_cap()
        if cap is not None:
            parts.append(f"cap{cap:g}")
        if s4_decode_dtype():
            parts.append(f"dec-{s4_decode_dtype()}")
    return ("+" + "+".join(parts)) if parts else ""


class DramaboxClient:
    """One Dramabox server instance held in VRAM for the duration of stage 4."""

    DEFAULT_PARAMS = {
        "cfg_scale": 2.5,
        "stg_scale": 1.5,
        "duration_multiplier": 1.1,
        "seed": 42,
        "denoise_ref": True,
        "watermark": False,
    }

    MODEL_VERSION = "dramabox-dit-v1-cfg2.5-stg1.5-dm1.1"

    def __init__(self, device: str = "cuda", **kwargs: Any) -> None:
        _ensure_path()
        _patch_torchaudio_save()
        from model_downloader import get_all_paths  # type: ignore[import-not-found]
        from inference_server import TTSServer  # type: ignore[import-not-found]

        # MPS-specific defaults (DESIGN.md §11.3). bitsandbytes is CUDA-only,
        # torch.compile is flaky on MPS for diffusion models, and bfloat16
        # has materially worse MPS coverage than fp16. Use `setdefault` so a
        # caller who explicitly passes one of these keeps that choice — only
        # the unset knobs get the per-device default.
        if device.startswith("mps"):
            kwargs.setdefault("dtype", "fp16")
            kwargs.setdefault("bnb_4bit", False)
            kwargs.setdefault("compile_model", False)
        # staged=False: the monolithic path defaults to the quantized encoder
        # (bf16 + DiT + vocoder don't fit one process on a 32 GB card); an
        # explicit LNVOX_S4_ENCODER_PRECISION=bf16 is still honored.
        if s4_encoder_precision(staged=False) == "bf16":
            kwargs.setdefault("bnb_4bit", False)

        paths = get_all_paths()
        self.device = device
        # TTSServer's own default is bnb_4bit=True, so "not in kwargs" means
        # the quantized path.
        gemma_root = resolve_gemma_root(
            paths["gemma_root"], kwargs.get("bnb_4bit", True)
        )
        self.server = TTSServer(
            checkpoint=paths["transformer"],
            full_checkpoint=paths["audio_components"],
            gemma_root=gemma_root,
            device=device,
            **kwargs,
        )

    def generate(
        self,
        *,
        prompt: str,
        output_path: Path,
        voice_ref: Path | None = None,
        seed: int | None = None,
        cfg_scale: float | None = None,
        stg_scale: float | None = None,
        duration_multiplier: float | None = None,
    ) -> None:
        """Render `prompt` to `output_path`. Optional `voice_ref` clones timbre."""
        params = dict(self.DEFAULT_PARAMS)
        if seed is not None:
            params["seed"] = seed
        if cfg_scale is not None:
            params["cfg_scale"] = cfg_scale
        if stg_scale is not None:
            params["stg_scale"] = stg_scale
        if duration_multiplier is not None:
            params["duration_multiplier"] = duration_multiplier

        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.server.generate_to_file(
            prompt=prompt,
            output=str(output_path),
            voice_ref=(str(voice_ref) if voice_ref else None),
            **params,
        )

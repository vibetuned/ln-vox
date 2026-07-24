"""Thin wrapper around VibeVoice-Large's inference classes (DESIGN.md §16).

Unlike Dramabox, VibeVoice is a real package: `scripts/setup_vibevoice.sh`
clones the community fork to `external/VibeVoice/` and pip-installs it
editable into the tts-phase venv, so this module just imports `vibevoice`.

Voice identity comes entirely from reference WAVs (zero-shot cloning) —
there is no descriptor-text channel. One `generate_session` call renders a
multi-speaker script (`Speaker 1: …` lines, up to 4 distinct speakers) in a
single pass; session planning lives in `stages/s4_vibevoice.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _resolve_model_path(explicit: str | None) -> str:
    """Explicit arg → LNVOX_VIBEVOICE_MODEL → local models/ dir → HF id."""
    if explicit:
        return explicit
    env = os.environ.get("LNVOX_VIBEVOICE_MODEL")
    if env:
        return env
    local = Path("models") / "VibeVoice-Large"
    if (local / "config.json").exists():
        return str(local)
    return "microsoft/VibeVoice-Large"


def _pick_attn_implementation(device: str) -> str:
    """flash_attention_2 on CUDA when importable; sdpa everywhere else.

    flash-attn ships no aarch64 wheel (DGX Spark) and doesn't exist on
    MPS/CPU, so sdpa is the portable fallback the fork itself uses.
    """
    if device.startswith("cuda"):
        try:
            import flash_attn  # noqa: F401

            return "flash_attention_2"
        except Exception:
            pass
    return "sdpa"


class VibeVoiceClient:
    """One VibeVoice-Large instance held in VRAM for the duration of stage 4."""

    DEFAULT_PARAMS = {
        "cfg_scale": 1.3,
        "ddpm_steps": 10,
        "seed": 42,
    }

    MODEL_VERSION = "vibevoice-large-cfg1.3-ddpm10-48k"

    # Everything downstream (s5 silence pads, concat demuxer) assumes
    # 48 kHz stereo (DESIGN.md §2.6/§16.4); VibeVoice emits 24 kHz mono,
    # so generate_session normalizes at save time.
    TARGET_SR = 48_000

    def __init__(
        self,
        device: str = "cuda",
        model_path: str | None = None,
        ddpm_steps: int | None = None,
    ) -> None:
        try:
            from vibevoice.modular.modeling_vibevoice_inference import (
                VibeVoiceForConditionalGenerationInference,
            )
            from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
        except ImportError as exc:
            raise RuntimeError(
                "The `vibevoice` package is not installed in this venv.\n"
                "Run `scripts/setup_vibevoice.sh` to clone the community fork "
                "and pip-install it (DESIGN.md §16.6)."
            ) from exc

        import torch

        self.device = device
        self.model_path = _resolve_model_path(model_path)
        self._ddpm_steps = ddpm_steps or int(self.DEFAULT_PARAMS["ddpm_steps"])

        # Per-device knobs (DESIGN.md §16.5): bf16 on CUDA; fp32 on MPS
        # (the fork's MPS branch — fp16 produces artifacts) and CPU.
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        attn = _pick_attn_implementation(device)

        self.processor = VibeVoiceProcessor.from_pretrained(self.model_path)

        load_kwargs: dict[str, Any] = {
            "torch_dtype": dtype,
            "attn_implementation": attn,
        }
        if device.startswith("cuda"):
            load_kwargs["device_map"] = device
        self.model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            self.model_path, **load_kwargs
        )
        if not device.startswith("cuda"):
            self.model.to(device)
        self.model.eval()
        self.model.set_ddpm_inference_steps(num_steps=self._ddpm_steps)

        # Native output rate, read from the processor when it exposes one.
        audio_proc = getattr(self.processor, "audio_processor", None)
        self.native_sr = int(getattr(audio_proc, "sampling_rate", 24_000) or 24_000)

    def generate_session(
        self,
        *,
        script: str,
        voice_refs: list[Path],
        output_path: Path,
        seed: int | None = None,
        cfg_scale: float | None = None,
    ) -> None:
        """Render a multi-speaker `script` to `output_path` (48 kHz stereo WAV).

        `script` is `Speaker N: text` lines, speakers numbered 1..len(voice_refs)
        in order of first appearance; `voice_refs[i]` is Speaker i+1's cloning
        reference. Seeded per call so re-renders (and re-rolls with a different
        seed) are reproducible.
        """
        import torch

        params = dict(self.DEFAULT_PARAMS)
        if seed is not None:
            params["seed"] = seed
        if cfg_scale is not None:
            params["cfg_scale"] = cfg_scale

        inputs = self.processor(
            text=[script],
            voice_samples=[[str(p) for p in voice_refs]],
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        inputs = {
            k: v.to(self.device) if hasattr(v, "to") else v for k, v in inputs.items()
        }

        torch.manual_seed(int(params["seed"]))
        outputs = self.model.generate(
            **inputs,
            max_new_tokens=None,
            cfg_scale=float(params["cfg_scale"]),
            tokenizer=self.processor.tokenizer,
            generation_config={"do_sample": False},
            verbose=False,
        )

        audio = outputs.speech_outputs[0]
        self._save_normalized(audio, output_path)

    def _save_normalized(self, audio, output_path: Path) -> None:
        """24 kHz mono model output → 48 kHz stereo WAV on disk."""
        import numpy as np
        import soundfile as sf
        import soxr
        import torch

        if isinstance(audio, torch.Tensor):
            arr = audio.detach().to(torch.float32).cpu().numpy()
        else:
            arr = np.asarray(audio, dtype=np.float32)
        arr = np.squeeze(arr)
        if arr.ndim != 1:
            raise RuntimeError(
                f"Expected mono model output, got array of shape {arr.shape}"
            )

        # Edge-silence trim (DESIGN.md §2.6) — before resampling, at native sr.
        # Session-internal pauses are untouched; only the outer edges go.
        if os.environ.get("LNVOX_S4_NO_TRIM") != "1":
            from lnvox.tts.trim import trim_array

            arr, _, _ = trim_array(arr, self.native_sr)

        if self.native_sr != self.TARGET_SR:
            arr = soxr.resample(arr, self.native_sr, self.TARGET_SR)
        stereo = np.stack([arr, arr], axis=1)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output_path), stereo, self.TARGET_SR)

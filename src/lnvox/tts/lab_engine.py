"""Warm, in-process, single-prompt TTS engine for the Studio's TTS lab.

Why not DramaboxClient there: its monolithic `TTSServer` builds every
component with ONE dtype, and no single dtype works on MPS — the Gemma
prompt-encoder returns all-NaN in fp16 (MAC_TEST_REPORT.md F10) while the
vocoder needs fp32 (F9). The staged s4 pipeline solved this with a
per-component dtype map, so this engine assembles the SAME components via
the staged helpers — encoder bf16-on-MPS, conditioner + DiT per `_knobs()`,
decoder fp32-on-MPS — and keeps them all warm for interactive A/B
auditioning (~32 GB resident: 23 GB bf16 encoder + 6 GB DiT + small VAEs;
fits the §11.3 36 GB budget).

Generation reuses `staged.denoise_chunk` (the exact euler recipe s4 runs),
the same prompt chunking policy as `build_plan` (`staged.apply_chunk_policy`
+ `staged.chunk_texts`: 22 s cap + word-split fallback by default, honoring
LNVOX_S4_CHUNK_CAP, with 22 s as the hard floor on MPS — F11), the same
encoder / decode dtype rules (`s4_effective_*`), and the same edge-trim as
s4 placement, so what the lab plays is what a book render will sound like.

CUDA is untouched: the Studio keeps using DramaboxClient there.
"""

from __future__ import annotations

from pathlib import Path


class StagedLabEngine:
    """Duck-types DramaboxClient's `generate()` for the Studio's TTS lab."""

    def __init__(self, device: str = "mps") -> None:
        from lnvox.tts.dramabox_client import (
            DramaboxClient,
            _ensure_path,
            _patch_torchaudio_save,
            resolve_gemma_root,
            s4_effective_decode_dtype,
            s4_effective_encoder_precision,
        )
        from lnvox.tts.staged import (
            PlanParams,
            _build_dit,
            _ensure_ltx_path,
            _knobs,
            apply_chunk_policy,
        )

        _ensure_path()
        _patch_torchaudio_save()
        _ensure_ltx_path()

        import torch
        from ltx_core.components.patchifiers import AudioPatchifier
        from ltx_pipelines.utils.blocks import AudioConditioner, AudioDecoder, PromptEncoder
        from model_downloader import get_all_paths

        from inference_server import DEFAULT_NEG

        self.device = device
        self._dev = torch.device(device)
        self._is_mps = device.startswith("mps")
        self.params = PlanParams(**DramaboxClient.DEFAULT_PARAMS)
        # The chunk policy build_plan applies (cap + word-split fallback) —
        # on MPS that also keeps the DiT's (T, T) attention mask out of the
        # broken boolean-indexing zone (F11).
        self._force_split = apply_chunk_policy(self.params, device)

        paths = get_all_paths()
        dtype, bnb_4bit, compile_model = _knobs(device)
        self._dtype = dtype

        # Encoder: bf16 on MPS (fp16 Gemma = all-NaN encodings, F10);
        # unquantized checkpoint whenever bnb is off (F8) or the staged-path
        # A/B default says bf16.
        enc_dtype = torch.bfloat16 if self._is_mps else dtype
        bnb_4bit = s4_effective_encoder_precision(device) == "bnb4"
        self._encoder = PromptEncoder(
            checkpoint_path=paths["audio_components"],
            gemma_root=resolve_gemma_root(paths["gemma_root"], bnb_4bit),
            dtype=enc_dtype,
            device=self._dev,
            warm=True,
            use_bnb_4bit=bnb_4bit,
            audio_only=True,
        )
        self._conditioner = AudioConditioner(
            checkpoint_path=paths["audio_components"],
            dtype=dtype,
            device=self._dev,
            warm=True,
        )
        self._dit = _build_dit(paths, self._dev, dtype, compile_model)
        self._patchifier = AudioPatchifier(patch_size=1)
        # Decoder: fp32 on MPS (fp16/bf16 hit the float-vs-half conv clash,
        # F9) and by default elsewhere — the staged decode phase's rule.
        self._dec_dtype = (
            torch.float32 if s4_effective_decode_dtype(device) == "fp32" else dtype
        )
        self._decoder = AudioDecoder(
            checkpoint_path=paths["audio_components"],
            dtype=self._dec_dtype,
            device=self._dev,
            warm=True,
        )

        with torch.inference_mode():
            # Cast out of the encoder's dtype (bf16 on MPS) into the DiT's:
            # mixing a bf16 negative context with an fp16 positive one trips
            # a hard Metal assertion (MPSNDArrayMatrixMultiplication requires
            # matching accumulator dtypes). The staged path gets this for
            # free by casting ctx tensors on load.
            self._neg_ctx = (
                self._encoder([DEFAULT_NEG], streaming_prefetch_count=None)[0]
                .audio_encoding.to(self._dev, dtype)
            )

        self._ref_cache: dict[str, object] = {}
        self._reuse = None  # lazily-built RE-USE denoiser (shared across refs)

    # -- internals ---------------------------------------------------------

    def _chunk_texts(self, prompt: str, duration_multiplier: float) -> list[str]:
        from lnvox.tts.staged import chunk_texts

        return chunk_texts(prompt, self.params, duration_multiplier, self._force_split)

    def _ref_latent(self, voice_ref: Path):
        key = str(voice_ref)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        from lnvox.tts.staged import _denoise_ref, _prep_ref_waveform

        from ltx_core.model.audio_vae import encode_audio as vae_encode_audio
        from ltx_pipelines.utils.media_io import decode_audio_from_file

        voice = decode_audio_from_file(
            str(voice_ref), self._dev, 0.0, self.params.ref_duration
        )
        if voice is None:
            raise RuntimeError(f"Could not load voice clip {voice_ref}")
        if self.params.denoise_ref:
            voice, self._reuse = _denoise_ref(voice, self._reuse, self._dev)
        voice = _prep_ref_waveform(voice, self.params.ref_duration)
        latent = self._conditioner(lambda enc: vae_encode_audio(voice, enc, None))
        self._ref_cache[key] = latent
        return latent

    # -- public API (mirrors DramaboxClient.generate) ------------------------

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
        import torch

        from lnvox.tts.staged import _estimate_duration, _n_frames, _write_wav, denoise_chunk
        from lnvox.tts.trim import trim_file

        from inference_server import _equal_power_crossfade

        p = self.params.model_copy(
            update={
                k: v
                for k, v in {
                    "seed": seed,
                    "cfg_scale": cfg_scale,
                    "stg_scale": stg_scale,
                    "duration_multiplier": duration_multiplier,
                }.items()
                if v is not None
            }
        )
        dm = p.duration_multiplier
        texts = self._chunk_texts(prompt, dm)

        ref_latent = self._ref_latent(voice_ref) if voice_ref is not None else None
        a_ctx_neg = self._neg_ctx if p.cfg_scale > 1.0 else None

        out = out_sr = None
        with torch.inference_mode():
            for text in texts:
                a_ctx = self._encoder([text], streaming_prefetch_count=None)[
                    0
                ].audio_encoding.to(self._dev, self._dtype)
                latent = denoise_chunk(
                    velocity_model=self._dit,
                    patchifier=self._patchifier,
                    n_frames=_n_frames(_estimate_duration(text, dm), p.fps),
                    a_ctx=a_ctx,
                    a_ctx_neg=a_ctx_neg,
                    ref_latent=ref_latent,
                    params=p,
                    dev=self._dev,
                    dtype=self._dtype,
                )
                decoded = self._decoder(latent.to(self._dev, self._dec_dtype))
                wav = decoded.waveform.cpu().float()
                if wav.dim() == 1:
                    wav = wav.unsqueeze(0)
                if out is None:
                    out, out_sr = wav, decoded.sampling_rate
                else:
                    if wav.shape[0] != out.shape[0]:
                        if wav.shape[0] == 1:
                            wav = wav.repeat(out.shape[0], 1)
                        elif out.shape[0] == 1:
                            out = out.repeat(wav.shape[0], 1)
                    out = _equal_power_crossfade(out, wav, out_sr, fade_ms=p.crossfade_ms)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(output_path, out, out_sr)
        # Same edge-silence trim s4 placement applies — the lab should sound
        # like the book will.
        trim_file(output_path)

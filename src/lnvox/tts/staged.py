"""Staged Dramabox TTS phases — DESIGN.md §15.

Splits `TTSServer.generate()` into four checkpointed GPU phases, each
loading exactly ONE model and sweeping every pending work item:

    refs     RE-USE denoise + VAE-encode each unique voice clip
    ctx      Gemma prompt encoding per chunk (+ the negative prompt, once)
    denoise  the LTX audio DiT 30-step euler loop per chunk
    decode   VAE decode + chunk crossfade + WAV/cache/manifest placement

Every phase body mirrors `external/DramaBox/src/inference_server.py`
step-for-step (same conditioning→noise order, same guider config, same
frame-513 silence-prior fix) so outputs are numerically identical to the
monolithic path. Dramabox source stays unpatched — its building blocks
are imported and composed here exactly the way `inference_server.py`
composes them.

"Done" is always a file existing on disk (written tmp + os.replace), so
a crashed phase resumes by re-listing pending items. No item is ever
skipped: crash-recovery policy lives in the driver (staged_driver.py).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from lnvox.stages.s4_tts import (
    _build_speaker_to_clip_path,
    _content_hash,
    _wav_duration,
)
from lnvox.tts.dramabox_client import (
    _DRAMABOX_ROOT,
    DramaboxClient,
    _ensure_path,
)

PHASES = ("refs", "ctx", "denoise", "decode")


# ----- plan model -------------------------------------------------------------


class PlanParams(BaseModel):
    """Sampling recipe. Field defaults mirror DramaboxClient.DEFAULT_PARAMS +
    the constants hard-coded in TTSServer.generate/generate_long."""

    cfg_scale: float = 2.5
    stg_scale: float = 1.5
    duration_multiplier: float = 1.1
    seed: int = 42
    denoise_ref: bool = True
    watermark: bool = False
    ref_duration: float = 10.0
    steps: int = 30
    fps: float = 25.0
    max_chunk_duration: float = 45.0
    target_chunk_duration: float = 37.0
    crossfade_ms: float = 50.0


class PlanChunk(BaseModel):
    """One denoise-sized unit of work (a whole short beat, or one
    text_chunker slice of a long one)."""

    item_id: str
    text: str
    gen_duration: float
    n_frames: int


class PlanRender(BaseModel):
    """One unique (prompt, voice) render — several beats may share it."""

    cache_key: str
    prompt: str
    ref_hash: Optional[str] = None
    cached: bool = False  # content-cache WAV existed at plan time
    chunks: list[PlanChunk] = []


class PlanBeat(BaseModel):
    """Placement of a render into the book, in playback order."""

    beat_id: str
    scene_id: str
    chapter_id: str
    type: str
    speaker: str
    cache_key: str


class PlanRefClip(BaseModel):
    ref_hash: str
    clip_path: str  # absolute


class StagedPlan(BaseModel):
    book_id: str
    model_version: str
    params: PlanParams
    refs: list[PlanRefClip] = []
    renders: list[PlanRender] = []
    beats: list[PlanBeat] = []


# ----- paths ------------------------------------------------------------------


def staged_root(book_dir: Path) -> Path:
    return book_dir / "05_audio" / "_staged"


def _plan_path(root: Path) -> Path:
    return root / "plan.json"


def _ref_file(root: Path, ref_hash: str) -> Path:
    return root / "refs" / f"{ref_hash}.safetensors"


def _ctx_file(root: Path, item_id: str) -> Path:
    return root / "ctx" / f"{item_id}.safetensors"


def _neg_file(root: Path) -> Path:
    return root / "ctx" / "neg.safetensors"


def _latent_file(root: Path, item_id: str) -> Path:
    return root / "latents" / f"{item_id}.safetensors"


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save_tensors(path: Path, tensors: dict) -> None:
    from safetensors.torch import save_file

    tmp = path.with_name(path.name + ".tmp")
    save_file({k: v.detach().cpu().contiguous() for k, v in tensors.items()}, str(tmp))
    os.replace(tmp, path)


def _load_tensor(path: Path, key: str):
    from safetensors.torch import load_file

    return load_file(str(path))[key]


def mark_inflight(root: Path, phase: str, item_id: str) -> None:
    """Record what we're about to attempt, so the driver can attribute a
    crash to a specific item (diagnostics only — never used to skip)."""
    _atomic_write_text(
        root / "inflight.json",
        json.dumps({"phase": phase, "item": item_id, "ts": time.time()}),
    )


def load_plan(root: Path) -> StagedPlan:
    return StagedPlan.model_validate_json(_plan_path(root).read_text(encoding="utf-8"))


def write_plan(root: Path, plan: StagedPlan) -> None:
    _atomic_write_text(_plan_path(root), plan.model_dump_json(indent=2))


# ----- plan building (CPU, deterministic) --------------------------------------


def _ensure_ltx_path() -> None:
    """Dramabox src/ + repo root (via dramabox_client) + its ltx2/ tree.
    inference_server.py inserts ltx2/ itself at import; the phases here
    import ltx blocks directly, so add it explicitly."""
    import sys

    _ensure_path()
    ltx2 = str(_DRAMABOX_ROOT / "ltx2")
    if ltx2 not in sys.path:
        sys.path.insert(0, ltx2)


def _estimate_duration(prompt: str, multiplier: float) -> float:
    # Mirrors inference_server.estimate_duration — reimplemented here (3
    # lines) because importing inference_server drags torch into the
    # CPU-only planning step.
    from duration_estimator import estimate_speech_duration

    return max(3.0, round(estimate_speech_duration(prompt) * multiplier, 1))


def _n_frames(gen_duration: float, fps: float) -> int:
    # Mirrors the frame sizing in TTSServer.generate().
    n = int(round(gen_duration * fps)) + 1
    return ((n - 1 + 4) // 8) * 8 + 1


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _slice_to_limit(chapters, limit: int):
    """Same beat-limit slicing as s4_tts.run (smoke-test mode)."""
    remaining = limit
    sliced = []
    for ch in chapters:
        if remaining <= 0:
            break
        kept_scenes = []
        for sc in ch.scenes:
            if remaining <= 0:
                break
            take = sc.beats[:remaining]
            if take:
                kept_scenes.append(sc.model_copy(update={"beats": take}))
                remaining -= len(take)
        if kept_scenes:
            sliced.append(ch.model_copy(update={"scenes": kept_scenes}))
    return sliced


def build_plan(
    book_id: str,
    chapters,
    casting,
    voicebank,
    voicebank_root: Path,
    cache_dir: Path,
    limit: Optional[int] = None,
) -> StagedPlan:
    _ensure_ltx_path()
    from text_chunker import chunk_prompt_for_duration

    params = PlanParams(**DramaboxClient.DEFAULT_PARAMS)
    model_version = DramaboxClient.MODEL_VERSION
    speaker_to_clip = _build_speaker_to_clip_path(casting, voicebank, voicebank_root)
    if limit is not None:
        chapters = _slice_to_limit(chapters, limit)

    ref_hash_by_clip: dict[Path, str] = {}
    refs: list[PlanRefClip] = []
    renders: dict[str, PlanRender] = {}
    beats: list[PlanBeat] = []

    for ch in chapters:
        for scene in ch.scenes:
            for idx, beat in enumerate(scene.beats):
                beat_id = f"{scene.scene_id}_b{idx:04d}"
                clip = speaker_to_clip.get(beat.speaker)
                ref_token = clip.name if clip else "no-ref"
                cache_key = _content_hash(beat.prompt, ref_token, model_version)

                ref_hash = None
                if clip is not None:
                    ref_hash = ref_hash_by_clip.get(clip)
                    if ref_hash is None:
                        ref_hash = _file_hash(clip)
                        ref_hash_by_clip[clip] = ref_hash
                        refs.append(PlanRefClip(ref_hash=ref_hash, clip_path=str(clip)))

                beats.append(
                    PlanBeat(
                        beat_id=beat_id,
                        scene_id=scene.scene_id,
                        chapter_id=ch.chapter_id,
                        type=beat.type,
                        speaker=beat.speaker,
                        cache_key=cache_key,
                    )
                )
                if cache_key in renders:
                    continue

                cached = (cache_dir / f"{cache_key}.wav").exists()
                chunks: list[PlanChunk] = []
                if not cached:
                    # Same routing as generate_to_file: over the chunk cap →
                    # text_chunker slices; each slice re-estimates its own
                    # duration exactly like generate_long's inner calls.
                    est = _estimate_duration(beat.prompt, params.duration_multiplier)
                    if est > params.max_chunk_duration:
                        texts = [
                            c.text
                            for c in chunk_prompt_for_duration(
                                beat.prompt,
                                max_duration_s=params.max_chunk_duration,
                                target_duration_s=params.target_chunk_duration,
                                duration_multiplier=params.duration_multiplier,
                            )
                        ]
                    else:
                        texts = [beat.prompt]
                    for ci, text in enumerate(texts):
                        gd = _estimate_duration(text, params.duration_multiplier)
                        chunks.append(
                            PlanChunk(
                                item_id=f"{cache_key}_c{ci:02d}",
                                text=text,
                                gen_duration=gd,
                                n_frames=_n_frames(gd, params.fps),
                            )
                        )
                renders[cache_key] = PlanRender(
                    cache_key=cache_key,
                    prompt=beat.prompt,
                    ref_hash=ref_hash,
                    cached=cached,
                    chunks=chunks,
                )

    return StagedPlan(
        book_id=book_id,
        model_version=model_version,
        params=params,
        refs=refs,
        renders=list(renders.values()),
        beats=beats,
    )


# ----- pending-work calculators (shared by driver and phases) -------------------


def _render_done(render: PlanRender, cache_dir: Path) -> bool:
    return (cache_dir / f"{render.cache_key}.wav").exists()


def _live_renders(plan: StagedPlan, cache_dir: Path) -> list[PlanRender]:
    """Renders whose content-cache WAV doesn't exist yet."""
    return [r for r in plan.renders if not _render_done(r, cache_dir)]


def pending_refs(plan: StagedPlan, root: Path, cache_dir: Path) -> list[str]:
    needed = {r.ref_hash for r in _live_renders(plan, cache_dir) if r.ref_hash}
    return sorted(h for h in needed if not _ref_file(root, h).exists())


def _neg_needed(plan: StagedPlan, root: Path, cache_dir: Path) -> bool:
    return (
        plan.params.cfg_scale > 1.0
        and bool(_live_renders(plan, cache_dir))
        and not _neg_file(root).exists()
    )


def pending_ctx(plan: StagedPlan, root: Path, cache_dir: Path) -> list[PlanChunk]:
    return [
        c
        for r in _live_renders(plan, cache_dir)
        for c in r.chunks
        if not _ctx_file(root, c.item_id).exists()
    ]


def pending_latents(
    plan: StagedPlan, root: Path, cache_dir: Path
) -> list[tuple[PlanRender, PlanChunk]]:
    return [
        (r, c)
        for r in _live_renders(plan, cache_dir)
        for c in r.chunks
        if not _latent_file(root, c.item_id).exists()
    ]


def pending_renders(plan: StagedPlan, root: Path, cache_dir: Path) -> list[PlanRender]:
    return _live_renders(plan, cache_dir)


def count_pending(phase: str, plan: StagedPlan, root: Path, cache_dir: Path) -> int:
    if phase == "refs":
        return len(pending_refs(plan, root, cache_dir))
    if phase == "ctx":
        return len(pending_ctx(plan, root, cache_dir)) + (
            1 if _neg_needed(plan, root, cache_dir) else 0
        )
    if phase == "denoise":
        return len(pending_latents(plan, root, cache_dir))
    if phase == "decode":
        return len(pending_renders(plan, root, cache_dir))
    raise ValueError(f"unknown phase {phase!r}")


def first_pending(phase: str, plan: StagedPlan, root: Path, cache_dir: Path) -> Optional[str]:
    if phase == "refs":
        p = pending_refs(plan, root, cache_dir)
        return p[0] if p else None
    if phase == "ctx":
        p = pending_ctx(plan, root, cache_dir)
        return p[0].item_id if p else ("neg" if _neg_needed(plan, root, cache_dir) else None)
    if phase == "denoise":
        p = pending_latents(plan, root, cache_dir)
        return p[0][1].item_id if p else None
    if phase == "decode":
        p = pending_renders(plan, root, cache_dir)
        return p[0].cache_key if p else None
    return None


# ----- per-device knobs ---------------------------------------------------------


def _knobs(device: str):
    """(torch_dtype, bnb_4bit, compile_model) — mirrors the MPS defaults
    DramaboxClient applies (DESIGN.md §11.3); CUDA keeps TTSServer defaults."""
    import torch

    is_mps = device.startswith("mps")
    dtype = torch.float16 if is_mps else torch.bfloat16
    return dtype, (not is_mps), (not is_mps)


# ----- phase: refs ---------------------------------------------------------------


def _prep_ref_waveform(voice, ref_duration: float):
    """Channel massage + tile-to-ref_duration + peak norm, verbatim from
    TTSServer.generate()'s voice-ref block."""
    from ltx_core.types import Audio

    w = voice.waveform
    if w.dim() == 2:
        if w.shape[0] == 1:
            w = w.repeat(2, 1)
        w = w.unsqueeze(0)
    elif w.dim() == 3 and w.shape[1] == 1:
        w = w.repeat(1, 2, 1)
    target_samples = int(ref_duration * voice.sampling_rate)
    if w.shape[-1] < target_samples:
        w = w.repeat(1, 1, (target_samples // w.shape[-1]) + 1)
    w = w[..., :target_samples]
    peak = w.abs().max()
    if peak > 0:
        w = w * (10 ** (-4.0 / 20) / peak)
    return Audio(waveform=w, sampling_rate=voice.sampling_rate)


def _denoise_ref(voice, reuse, device):
    """RE-USE denoise of the reference, verbatim from
    TTSServer._denoise_voice_ref (minus its per-path cache — each clip
    passes through here exactly once). Returns (voice, reuse) where reuse
    is the lazily-built upsampler, or False if unavailable."""
    import logging

    from ltx_core.types import Audio

    if reuse is None:
        from super_resolution import REUSEUpsampler

        try:
            reuse = REUSEUpsampler(
                target_sr=int(voice.sampling_rate), device=device, chunk_size_s=1.0
            )
        except Exception as e:
            logging.warning(f"Voice-ref denoise disabled (RE-USE unavailable: {e})")
            reuse = False
    if reuse is False:
        return voice, reuse

    w = voice.waveform
    if w.dim() == 3:
        mono = w[0].mean(dim=0)
    elif w.dim() == 2:
        mono = w.mean(dim=0)
    else:
        mono = w
    cleaned, _ = reuse(mono.contiguous(), in_sr=int(voice.sampling_rate))
    if cleaned.dim() == 2 and cleaned.shape[0] == 1:
        cleaned = cleaned[0]
    cleaned = cleaned.unsqueeze(0).unsqueeze(0).to(device, dtype=w.dtype)
    return Audio(waveform=cleaned, sampling_rate=voice.sampling_rate), reuse


def _phase_refs(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None:
    todo = pending_refs(plan, root, cache_dir)
    if not todo:
        return
    _ensure_ltx_path()
    import torch
    from ltx_core.model.audio_vae import encode_audio as vae_encode_audio
    from ltx_pipelines.utils.blocks import AudioConditioner
    from ltx_pipelines.utils.media_io import decode_audio_from_file
    from model_downloader import get_all_paths

    paths = get_all_paths()
    dev = torch.device(device)
    dtype, _, _ = _knobs(device)
    conditioner = AudioConditioner(
        checkpoint_path=paths["audio_components"], dtype=dtype, device=dev, warm=True
    )
    clip_by_hash = {r.ref_hash: r.clip_path for r in plan.refs}
    reuse = None

    with torch.inference_mode():
        for i, ref_hash in enumerate(todo):
            mark_inflight(root, "refs", ref_hash)
            clip_path = clip_by_hash[ref_hash]
            voice = decode_audio_from_file(clip_path, dev, 0.0, plan.params.ref_duration)
            if voice is None:
                raise RuntimeError(f"Could not load voice clip {clip_path}")
            if plan.params.denoise_ref:
                voice, reuse = _denoise_ref(voice, reuse, dev)
            voice = _prep_ref_waveform(voice, plan.params.ref_duration)
            ref_latent = conditioner(lambda enc: vae_encode_audio(voice, enc, None))
            _save_tensors(_ref_file(root, ref_hash), {"ref_latent": ref_latent})
            print(f"[refs] {i + 1}/{len(todo)} {ref_hash} ← {Path(clip_path).name}", flush=True)


# ----- phase: ctx ----------------------------------------------------------------


def _phase_ctx(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None:
    todo = pending_ctx(plan, root, cache_dir)
    need_neg = _neg_needed(plan, root, cache_dir)
    if not todo and not need_neg:
        return
    _ensure_ltx_path()
    import torch
    from ltx_pipelines.utils.blocks import PromptEncoder
    from model_downloader import get_all_paths

    from inference_server import DEFAULT_NEG  # single source of truth

    paths = get_all_paths()
    dev = torch.device(device)
    dtype, bnb_4bit, _ = _knobs(device)
    encoder = PromptEncoder(
        checkpoint_path=paths["audio_components"],
        gemma_root=paths["gemma_root"],
        dtype=dtype,
        device=dev,
        warm=True,
        use_bnb_4bit=bnb_4bit,
        audio_only=True,
    )

    with torch.inference_mode():
        if need_neg:
            mark_inflight(root, "ctx", "neg")
            neg = encoder([DEFAULT_NEG], streaming_prefetch_count=None)[0].audio_encoding
            _save_tensors(_neg_file(root), {"a_ctx": neg})
            print("[ctx] negative prompt encoded (once per book)", flush=True)
        for i, chunk in enumerate(todo):
            mark_inflight(root, "ctx", chunk.item_id)
            ctx = encoder([chunk.text], streaming_prefetch_count=None)[0].audio_encoding
            _save_tensors(_ctx_file(root, chunk.item_id), {"a_ctx": ctx})
            if (i + 1) % 10 == 0 or i + 1 == len(todo):
                print(f"[ctx] {i + 1}/{len(todo)}", flush=True)


# ----- phase: denoise -------------------------------------------------------------


def _build_dit(paths: dict, device, dtype, compile_model: bool):
    """Build the audio-only DiT. Mirrors TTSServer._load_all step 3 — the
    configurator is defined inside that method, so it can't be imported."""
    import json as _json

    import torch
    from ltx_core.loader import DummyRegistry
    from ltx_core.loader.sd_ops import SDOps
    from ltx_core.loader.single_gpu_model_builder import SingleGPUModelBuilder as Builder
    from ltx_core.model.model_protocol import ModelConfigurator
    from ltx_core.model.transformer.attention import AttentionFunction
    from ltx_core.model.transformer.model import LTXModel, LTXModelType
    from ltx_core.model.transformer.rope import LTXRopeType
    from ltx_core.model.transformer.text_projection import create_caption_projection
    from safetensors import safe_open

    checkpoint = paths["transformer"]
    with safe_open(checkpoint, framework="pt") as f:
        _json.loads(f.metadata()["config"])  # validated the same way TTSServer does

    class AudioOnlyConfigurator(ModelConfigurator[LTXModel]):
        @classmethod
        def from_config(cls, cfg):
            t = cfg.get("transformer", {})
            cp = None
            if not t.get("caption_proj_before_connector", False):
                with torch.device("meta"):
                    cp = create_caption_projection(t, audio=True)
            return LTXModel(
                model_type=LTXModelType.AudioOnly,
                audio_num_attention_heads=t.get("audio_num_attention_heads", 32),
                audio_attention_head_dim=t.get("audio_attention_head_dim", 64),
                audio_in_channels=t.get("audio_in_channels", 128),
                audio_out_channels=t.get("audio_out_channels", 128),
                num_layers=t.get("num_layers", 48),
                audio_cross_attention_dim=t.get("audio_cross_attention_dim", 2048),
                norm_eps=t.get("norm_eps", 1e-6),
                attention_type=AttentionFunction(t.get("attention_type", "default")),
                positional_embedding_theta=10000.0,
                audio_positional_embedding_max_pos=[20.0],
                timestep_scale_multiplier=t.get("timestep_scale_multiplier", 1000),
                use_middle_indices_grid=t.get("use_middle_indices_grid", True),
                rope_type=LTXRopeType(t.get("rope_type", "interleaved")),
                double_precision_rope=t.get("frequencies_precision", False) == "float64",
                apply_gated_attention=t.get("apply_gated_attention", False),
                audio_caption_projection=cp,
                cross_attention_adaln=t.get("cross_attention_adaln", False),
            )

    audio_sd_ops = SDOps("AO").with_matching(prefix="model.diffusion_model.").with_replacement(
        "model.diffusion_model.", ""
    )
    builder = Builder(
        model_path=checkpoint,
        model_class_configurator=AudioOnlyConfigurator,
        model_sd_ops=audio_sd_ops,
        registry=DummyRegistry(),
    )
    model = builder.build(device=device, dtype=dtype).to(device).eval()
    if compile_model:
        model = torch.compile(model, mode="default", dynamic=True)
    return model


def _phase_denoise(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None:
    todo = pending_latents(plan, root, cache_dir)
    if not todo:
        return
    # Frame-sorted so torch.compile(dynamic=True) sees few distinct shapes.
    todo.sort(key=lambda rc: rc[1].n_frames)

    _ensure_ltx_path()
    import torch
    from audio_conditioning import AudioConditionByReferenceLatent
    from ltx_core.components.diffusion_steps import EulerDiffusionStep
    from ltx_core.components.guiders import MultiModalGuider, MultiModalGuiderParams
    from ltx_core.components.noisers import GaussianNoiser
    from ltx_core.components.patchifiers import AudioPatchifier
    from ltx_core.components.schedulers import LTX2Scheduler
    from ltx_core.model.transformer.model import X0Model
    from ltx_core.tools import AudioLatentTools
    from ltx_core.types import AudioLatentShape, VideoPixelShape
    from ltx_pipelines.utils.denoisers import GuidedDenoiser
    from ltx_pipelines.utils.samplers import euler_denoising_loop
    from model_downloader import get_all_paths

    from inference_server import auto_rescale_for_cfg

    p = plan.params
    paths = get_all_paths()
    dev = torch.device(device)
    dtype, _, compile_model = _knobs(device)
    velocity_model = _build_dit(paths, dev, dtype, compile_model)
    patchifier = AudioPatchifier(patch_size=1)

    a_ctx_neg = None
    if p.cfg_scale > 1.0:
        a_ctx_neg = _load_tensor(_neg_file(root), "a_ctx").to(dev, dtype)
    rescale = auto_rescale_for_cfg(p.cfg_scale)
    ref_cache: dict[str, "torch.Tensor"] = {}

    with torch.inference_mode():
        for i, (render, chunk) in enumerate(todo):
            mark_inflight(root, "denoise", chunk.item_id)
            t0 = time.time()

            pixel_shape = VideoPixelShape(
                batch=1, frames=chunk.n_frames, height=64, width=64, fps=p.fps
            )
            target_shape = AudioLatentShape.from_video_pixel_shape(pixel_shape)
            audio_tools = AudioLatentTools(patchifier=patchifier, target_shape=target_shape)
            state = audio_tools.create_initial_state(device=dev, dtype=dtype)

            if render.ref_hash:
                ref_latent = ref_cache.get(render.ref_hash)
                if ref_latent is None:
                    ref_latent = _load_tensor(_ref_file(root, render.ref_hash), "ref_latent")
                    ref_cache[render.ref_hash] = ref_latent
                cond = AudioConditionByReferenceLatent(
                    latent=ref_latent.to(dev, dtype), strength=1.0
                )
                state = cond.apply_to(state, audio_tools)

            gen = torch.Generator(device=dev).manual_seed(p.seed)
            state = GaussianNoiser(generator=gen)(state, noise_scale=1.0)

            a_ctx = _load_tensor(_ctx_file(root, chunk.item_id), "a_ctx").to(dev, dtype)
            guider = MultiModalGuider(
                params=MultiModalGuiderParams(
                    cfg_scale=p.cfg_scale,
                    stg_scale=p.stg_scale,
                    stg_blocks=[29],
                    rescale_scale=rescale,
                    modality_scale=1.0,
                ),
                negative_context=a_ctx_neg,
            )
            denoiser = GuidedDenoiser(
                v_context=None, a_context=a_ctx, video_guider=None, audio_guider=guider
            )
            sigmas = LTX2Scheduler().execute(steps=p.steps, latent=state.latent).to(dev)

            _, audio_state = euler_denoising_loop(
                sigmas=sigmas,
                video_state=None,
                audio_state=state,
                stepper=EulerDiffusionStep(),
                transformer=X0Model(velocity_model),
                denoiser=denoiser,
            )
            audio_state = audio_tools.clear_conditioning(audio_state)
            audio_state = audio_tools.unpatchify(audio_state)

            # Frame-513 end-of-clip silence-prior fix (TTSServer.generate).
            latent = audio_state.latent
            if latent.shape[2] > 513:
                f0, f1 = 511, 514
                n = f1 - f0
                patched = latent.clone()
                for f in (512, 513):
                    t = (f - f0) / n
                    patched[:, :, f, :] = (1.0 - t) * latent[:, :, f0, :] + t * latent[:, :, f1, :]
                latent = patched

            _save_tensors(_latent_file(root, chunk.item_id), {"latent": latent})
            print(
                f"[denoise] {i + 1}/{len(todo)} {chunk.item_id} "
                f"({chunk.gen_duration:.1f}s target, {time.time() - t0:.1f}s wall)",
                flush=True,
            )


# ----- phase: decode ---------------------------------------------------------------


def _write_wav(path: Path, waveform, sample_rate: int) -> None:
    """soundfile writer (same layout convention as the torchaudio shim in
    dramabox_client), atomic via tmp + replace."""
    import numpy as np
    import soundfile as sf

    arr = waveform.detach().cpu().float().numpy()
    if arr.ndim == 2:
        arr = arr.T  # (C, T) → (T, C)
    tmp = path.with_name(path.stem + ".tmp.wav")
    sf.write(str(tmp), np.ascontiguousarray(arr), int(sample_rate))
    os.replace(tmp, path)


def _phase_decode(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None:
    todo = pending_renders(plan, root, cache_dir)
    if todo:
        _ensure_ltx_path()
        import torch
        from ltx_pipelines.utils.blocks import AudioDecoder
        from model_downloader import get_all_paths

        from inference_server import _equal_power_crossfade

        p = plan.params
        paths = get_all_paths()
        dev = torch.device(device)
        dtype, _, _ = _knobs(device)
        decoder = AudioDecoder(
            checkpoint_path=paths["audio_components"], dtype=dtype, device=dev, warm=True
        )
        cache_dir.mkdir(parents=True, exist_ok=True)

        with torch.inference_mode():
            for i, render in enumerate(todo):
                mark_inflight(root, "decode", render.cache_key)
                out, out_sr = None, None
                for chunk in render.chunks:
                    latent = _load_tensor(_latent_file(root, chunk.item_id), "latent").to(
                        dev, dtype
                    )
                    decoded = decoder(latent)
                    wav = decoded.waveform.cpu().float()
                    if wav.dim() == 1:
                        wav = wav.unsqueeze(0)
                    if out is None:
                        out, out_sr = wav, decoded.sampling_rate
                    else:
                        if decoded.sampling_rate != out_sr:
                            raise RuntimeError(
                                f"Sample-rate mismatch between chunks: {out_sr} vs "
                                f"{decoded.sampling_rate}"
                            )
                        if wav.shape[0] != out.shape[0]:
                            if wav.shape[0] == 1:
                                wav = wav.repeat(out.shape[0], 1)
                            elif out.shape[0] == 1:
                                out = out.repeat(wav.shape[0], 1)
                        out = _equal_power_crossfade(out, wav, out_sr, fade_ms=p.crossfade_ms)
                if out is None:
                    raise RuntimeError(
                        f"Render {render.cache_key} has no chunks and no cache WAV — "
                        "re-run `lnvox s4 --staged` to re-plan."
                    )

                if p.watermark:
                    try:
                        import numpy as np
                        import perth

                        wm = perth.PerthImplicitWatermarker()
                        mono = out.mean(dim=0).numpy() if out.shape[0] > 1 else out[0].numpy()
                        mono_wm = wm.apply_watermark(mono, sample_rate=out_sr)
                        mono_wm_t = torch.from_numpy(
                            np.asarray(mono_wm, dtype=np.float32)
                        ).unsqueeze(0)
                        out = mono_wm_t if out.shape[0] == 1 else mono_wm_t.repeat(out.shape[0], 1)
                    except Exception as e:
                        import logging

                        logging.warning(f"Perth watermark skipped ({e})")

                _write_wav(cache_dir / f"{render.cache_key}.wav", out, out_sr)
                for chunk in render.chunks:
                    _ctx_file(root, chunk.item_id).unlink(missing_ok=True)
                    _latent_file(root, chunk.item_id).unlink(missing_ok=True)
                print(
                    f"[decode] {i + 1}/{len(todo)} {render.cache_key} "
                    f"({len(render.chunks)} chunk(s), {out.shape[-1] / out_sr:.1f}s)",
                    flush=True,
                )

    _finalize(plan, root, cache_dir)


def _finalize(plan: StagedPlan, root: Path, cache_dir: Path) -> None:
    """Place every beat's WAV from the content cache and write per-chapter
    manifests — same layout and schema as s4_tts.render_chapter."""
    from lnvox.tts.schema import ChapterAudio, RenderedBeat

    audio_dir = root.parent  # …/05_audio
    render_by_key = {r.cache_key: r for r in plan.renders}
    by_chapter: dict[str, list[PlanBeat]] = {}
    for b in plan.beats:
        by_chapter.setdefault(b.chapter_id, []).append(b)

    for chapter_id, chapter_beats in by_chapter.items():
        chapter_dir = audio_dir / chapter_id
        chapter_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[RenderedBeat] = []
        total = 0.0
        for b in chapter_beats:
            wav_path = chapter_dir / f"{b.beat_id}.wav"
            cache_path = cache_dir / f"{b.cache_key}.wav"
            if not wav_path.exists():
                if not cache_path.exists():
                    raise RuntimeError(
                        f"Content cache missing for beat {b.beat_id} ({b.cache_key}) — "
                        "the decode phase should have produced it."
                    )
                shutil.copy(cache_path, wav_path)
            # Edge-silence trim (DESIGN.md §2.6) — idempotent; a real trim
            # upgrades the cache entry in place so re-runs stay trimmed.
            from lnvox.tts.trim import trim_file

            if trim_file(wav_path) > 0 and cache_path.exists():
                shutil.copy(wav_path, cache_path)
            dur = _wav_duration(wav_path)
            total += dur
            rendered.append(
                RenderedBeat(
                    beat_id=b.beat_id,
                    scene_id=b.scene_id,
                    type=b.type,
                    speaker=b.speaker,
                    wav_path=str(wav_path.relative_to(audio_dir.parent)),
                    duration_seconds=round(dur, 3),
                    cache_key=b.cache_key,
                    cached=render_by_key[b.cache_key].cached,
                )
            )
        manifest = ChapterAudio(
            chapter_id=chapter_id,
            beats=rendered,
            total_duration_seconds=round(total, 3),
        )
        _atomic_write_text(chapter_dir / "manifest.json", manifest.model_dump_json(indent=2))
        print(
            f"[decode] ✓ {chapter_id}: {len(rendered)} beats, {total:.1f}s → manifest.json",
            flush=True,
        )


# ----- phase dispatch ---------------------------------------------------------------


def run_phase(phase: str, book_dir: Path, cache_dir: Path, device: str) -> None:
    """Entry point for the hidden `lnvox s4-phase` command (one subprocess
    per invocation — a fresh CUDA context every time)."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
    root = staged_root(book_dir)
    plan = load_plan(root)
    dispatch = {
        "refs": _phase_refs,
        "ctx": _phase_ctx,
        "denoise": _phase_denoise,
        "decode": _phase_decode,
    }
    dispatch[phase](plan, root, cache_dir, device)

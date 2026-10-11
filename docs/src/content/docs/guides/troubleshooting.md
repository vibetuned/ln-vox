---
title: Troubleshooting
description: Symptoms, causes and fixes for the failures people actually hit — environment, LLM server, Stage 4 renders, casting and sync.
sidebar:
  order: 10
---

Start with the stage that failed: every stage page has its own **Failure modes** section. This page collects the cross-cutting problems.

## Environment

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Dramabox not found at external/DramaBox` | Setup skipped | `./scripts/setup_dramabox.sh` |
| `No module named 'av'` / `requires accelerate` / a Dramabox import error | `uv sync` (any LLM-phase sync) pruned Dramabox's overlay deps | `./scripts/run_pipeline.sh --prepare-env tts`, or re-run `./scripts/setup_dramabox.sh` |
| `No module named 'mamba_ssm'`, or s4 5–10× slower than usual on CUDA | Mamba kernel wheels missing or built against the wrong torch | `--prepare-env tts` reinstalls the prebuilt wheels ([`install_mamba_kernel_wheels`](/launcher/functions/#install_mamba_kernel_wheels)) |
| `Torch not compiled with CUDA enabled` on DGX Spark / Jetson | aarch64 has no CUDA wheel for Dramabox's pinned torch 2.8 | `./scripts/setup_dramabox.sh` detects aarch64 and pulls torch 2.10 from cu130 |
| Gemma `has no attribute 'model'` in Dramabox | A VibeVoice install pinned `transformers==4.51.3` | `uv pip install -U 'transformers>=4.52'` (`prepare_tts_env` does this) |
| `Python.h not found` during a JIT compile | System Python without headers | Use the uv-managed Python 3.13 |
| `llama-server` not on PATH | llama.cpp not installed | `brew install llama.cpp`, `winget install ggml.llamacpp` on Windows (then open a new shell), or build it with CUDA on Linux |

## LLM server

| Symptom | Cause | Fix |
| --- | --- | --- |
| "did not become ready within 600s" | First run downloading the model, or a crash | Read the log file the launcher printed (`lnvox_llm.*.log` in the temp dir); re-run once the download finishes |
| `BadRequestError: maximum context length` | The server's context is smaller than the client assumes (65 536 by default) | Pass `--max-model-len N` to the launcher (it reaches both the serve script and the client), or export `LNVOX_LLM_MAX_LEN=N` when running `lnvox` by hand ([environment](/reference/environment/)) |
| `openai.APITimeoutError` mid-s2 on a slow model | Per-call timeout assumes > 20 tok/s (a timed-out generation is not re-sent) | Raise `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN` (0.35–0.5 for ~6 tok/s hosts) — timeout = `timeout_base_seconds + max_tokens × timeout_seconds_per_token` |
| Structured calls exhaust their budget on llama-server | Gemma's thinking channel enabled | Make sure `LNVOX_LLM_BACKEND=llama` reaches the stages (the launcher exports it; set it yourself when running `lnvox` by hand) |
| mlx: identical invalid JSON on every retry | `mlx_lm.server` ignores per-request sampling | `serve_mlx.sh` serves with `--temp` (`LNVOX_LLM_TEMP`, default 0.2); the client feeds the validation error back on retry |
| A server on another port or host | — | `LNVOX_LLM_PORT=<port>` moves the launcher's server, probe and client together; for another host pass `--vllm-url http://host:port/v1` |

## Stage 4 renders

| Symptom | Cause | Fix |
| --- | --- | --- |
| Monolithic s4 dies after 10–15 min | CUDA fragmentation on long continuous denoise | Use the staged path (the launcher default) or `s4_retry.sh`; the cache makes every restart a resume |
| Staged s4 aborts with "stuck on item" | The same item failed `LNVOX_S4_STALL_LIMIT` (10) consecutive restarts | Read the stall diagnostics; see [staged driver](/library/tts/staged-driver/) |
| `LNVOX_S4_CHUNK_CAP / LNVOX_S4_DECODE_DTYPE need the staged path` (exit 2) | A staged-only knob set for the monolithic path | Unset it (an empty value counts as unset), or render with `--staged`. Exit 2 is never retried |
| `Monolithic Dramabox can't render on MPS` (exit 2) | No single dtype works on MPS (the fp16 Gemma encoder returns NaN → silent audio) | Render with `--staged`, the launcher default — it applies the per-component dtype map. To check a finished file for silence: `ffmpeg -af volumedetect` (silence ≈ −91 dB) |
| Renders sound rushed or slurred | Beats too long | Lower `MAX_MERGED_BEAT_CHARS` in `s3_director.py`, re-run s3 |
| Everything re-renders after a config change | The render recipe is part of the cache key | Expected; pick one recipe per book ([render quality](/guides/render-quality/)) |

## Casting

| Symptom | Fix |
| --- | --- |
| Two characters who talk to each other sound alike | `lnvox voice audit <book> [--fix]`, or recast by hand ([casting guide](/guides/casting/)) |
| A character has no clip | Stage 1 left their gender `unknown`; assign one by hand |
| Narrator voice doesn't match its descriptor | A stale `voice_descriptor` on the casting (s3 uses a non-empty one verbatim): empty it, then re-run s3 — profiles and chapters follow the casting |
| A hand edit was undone | It wasn't locked: an unlocked entry loses to a prior volume's assignment. Set `"locked": true` (the Studio and Set Voice do) — see [casting](/guides/casting/) |
| `Voicebank empty` | Seed it (`lnvox voice seed-cv`) or point `LNVOX_VOICEBANK` at the right bank |

## Sync

| Symptom | Fix |
| --- | --- |
| Highlighting drifts through the book | s6 followed a different mix (pass `--title` for the m4b you ship), explicit s6 pad flags overrode the sidecar, or beats were re-rendered after the mix (s6 warns `off the mixed chapter`) — re-run s5, then s6 |
| Stage 6 skipped | No EPUB at `epubs/<book_id>.epub` (pass `--epub`), no `05_audio/`, or VibeVoice audio |
| Low match rate | Stage 2 paraphrased the text; check `07_sync/unmatched.json` and the `match_confidence` histogram ([Stage 6](/stages/s6-sync/)) |

## Sharing the book

| Symptom | Cause | Fix |
| --- | --- | --- |
| An `.m4b` sent from Windows shows up as `bin`, an unknown file or a video on the phone / other computer, while Linux and Mac mixes open as audio | Windows registers no MIME type for `.m4b`, so the app that sends it labels it `application/octet-stream`; the file itself is fine | `reg add "HKCU\Software\Classes\.m4b" /v "Content Type" /d "audio/mp4" /f` (prefix `MSYS_NO_PATHCONV=1` in Git Bash), restart the sending app, re-send — [details](/guides/windows/#sharing-the-m4b) |

## Disk

Plan for ~96 GB for the Common Voice tarball, ~600 MB of WAVs per hour of audio in `05_audio/`, a few full-length WAVs in `06_final/_work/` while a mix runs (removed after a successful mix; `--keep-work` keeps them), and ~50 MB per hour in the final `.m4b`. `cache/tts/` grows indefinitely; every entry is a re-render you don't pay again, so clear it only when you must.

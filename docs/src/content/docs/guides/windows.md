---
title: Windows
description: Running ln-vox on Windows 11 + NVIDIA — Git Bash for the scripts, llama.cpp from winget, Dramabox's CUDA torch, registering .m4b so shared books arrive as audio, and what behaves differently from Linux.
sidebar:
  order: 7
---

Linux + CUDA is the primary target. Windows 11 with an NVIDIA GPU is supported: the launcher, the LLM stages on llama.cpp and Dramabox s4 all run natively, with the bash scripts under **Git Bash**. Validated on an RTX 5090 (32 GB) — see [Validation run](#validation-run).

## Setup

From any shell, then open a **new** Git Bash so `PATH` picks up the tools:

```bash
# 1. Tooling
winget install Git.Git           # also provides Git Bash
winget install astral-sh.uv
winget install Gyan.FFmpeg       # s5 mix
winget install ggml.llamacpp     # llama-server, the default LLM backend

# 2. Python deps
uv sync --extra voice --extra tts

# 3. Dramabox — the Windows branch takes its torch pin from the cu128 index
./scripts/setup_dramabox.sh

# 4. Voicebank: copy voicebank/ from your main machine, or seed it as on Linux

# 5. Register .m4b as audio, so the books you share arrive as audio
#    (see "Sharing the .m4b"). Git Bash needs the MSYS_NO_PATHCONV=1 prefix;
#    in PowerShell / cmd drop it.
MSYS_NO_PATHCONV=1 reg add "HKCU\Software\Classes\.m4b" /v "Content Type" /d "audio/mp4" /f
```

Then run the launcher exactly as on Linux, from Git Bash:

```bash
./scripts/run_pipeline.sh novel-name/volume-01 --narrator-clip cv_… --book-title "…"
```

From PowerShell or `cmd`, call Git's bash explicitly — a bare `bash` on Windows may be WSL's, which runs the script in a Linux VM against a different venv:

```powershell
& "C:\Program Files\Git\bin\bash.exe" scripts/run_pipeline.sh novel-name/volume-01 …
```

The `uv run lnvox …` stage commands work from any shell.

## What differs from Linux

**LLM backend: llama.cpp only.** vLLM ships Linux wheels only and mlx is Apple-only; `prepare_llm_env` refuses both on Windows with exit 2 (an external `--vllm-url` server still works). winget installs llama.cpp's **Vulkan** build. On a machine that also has an integrated GPU, llama.cpp uses only the discrete one — verified with Windows' per-adapter GPU counters. The CUDA zips on the llama.cpp release page work too, unpacked onto `PATH`.

**Dramabox's torch comes from the cu128 index.** PyPI's Windows torch wheels are CPU-only. `setup_dramabox.sh` and the launcher's `install_dramabox_reqs` install Dramabox's own `torch` / `torchaudio` pins from `https://download.pytorch.org/whl/cu128` first (CUDA 12.8 covers Blackwell), then the rest of its requirements.

**No RE-USE voice-reference denoising.** RE-USE needs the mamba-ssm / causal-conv1d kernels, which have no Windows build. s4 logs `Voice-ref denoise disabled (RE-USE unavailable: …)` once and clones from the reference clip as-is. A quality-ranked voicebank (`seed-cv --rank-by-quality`) matters a little more here.

**No `torch.compile` without Triton.** Torch ships without Triton on Windows, and an uncompilable DiT would crash on its first denoise call, so both s4 paths skip compilation unless `triton` is importable (`s4_compile_supported`). Installing the community `triton-windows` package turns it back on; the TTS env prep doesn't do that for you. Compilation only affects speed, so it isn't part of the cache key.

**Sleep prevention.** `lnvox s4` asks Windows to stay awake (`SetThreadExecutionState`) until it exits — the counterpart of the launcher's macOS `caffeinate` re-exec, and it covers direct `lnvox s4` runs and ComfyUI too.

**UTF-8 output.** A piped stdout on Windows defaults to the ANSI code page, which can't encode the `→` / `✓` in the progress output. The CLI reconfigures its streams to UTF-8, and the launcher and the ComfyUI pack export `PYTHONUTF8=1` for everything they start.

**Output file names.** `:` and the other characters NTFS forbids (`<>"|?*`), plus trailing dots and spaces, become `_` in the `.m4b` and `.timings.json` names — a `Title: Subtitle.m4b` would otherwise land in an NTFS alternate data stream. The embedded title keeps them. This is Windows-only, so books mixed elsewhere keep their names.

**Line endings.** Git for Windows defaults to `core.autocrlf=true`, and a CRLF shell script fails on line one with `$'\r': command not found`. `.gitattributes` pins `*.sh` to LF. A checkout made before that file existed needs a refresh: `rm scripts/*.sh && git checkout -- scripts`.

**Hugging Face cache without symlinks.** Without Developer Mode, `huggingface_hub` warns that it can't symlink and stores plain copies instead. It works; enabling Developer Mode saves the duplicate disk space. It also avoids a first-download hiccup: `huggingface_hub` (0.36) marks a folder symlink-capable *before* probing it, so parallel download threads can hit `OSError: [WinError 1314]` on the very first fetch of a model. The staged s4 driver relaunches the phase and the download resumes (files already fetched skip the race) — expect one or two `phase refs`/`phase ctx` restarts on the first render, not a stall.

## Sharing the .m4b

**Symptom.** A book mixed on Windows and sent to a phone or another computer shows up as `bin`, an unknown file, or even a *video* — on Android, iOS/macOS and Linux alike — while books mixed on a Linux or Mac box open as audio everywhere.

**Cause.** Not the file: a Windows mix has byte-for-byte the same layout as a Linux or Mac one (same `isom` brand, audio + chapter tracks, pictures in the `covr` cover-art atom — even with the same ffmpeg version). It's the **label it travels with**. Windows registers no MIME type for `.m4b` — it does for `.m4a` (`audio/mp4`), `.mp3` and `.epub` — and browsers, sync clients and transfer apps ask Windows for a file's type when they send it. For `.m4b` they get nothing and fall back to `application/octet-stream`. The receiving side then lists a generic binary, or sniffs the content, sees a plain MP4 header and calls it video. Python on Windows has the same gap (`mimetypes.guess_type("book.m4b")` → `None`).

**Fix.** Register the type for your Windows user (no admin needed), then restart the app you send files with:

```bash
# Git Bash — without the prefix, Git Bash rewrites /v into a path and reg fails
MSYS_NO_PATHCONV=1 reg add "HKCU\Software\Classes\.m4b" /v "Content Type" /d "audio/mp4" /f
```
```powershell
# PowerShell / cmd
reg add "HKCU\Software\Classes\.m4b" /v "Content Type" /d "audio/mp4" /f
```

Check it with `reg query "HKCU\Software\Classes\.m4b" /v "Content Type"` (same prefix rule), or `uv run python -c "import mimetypes; print(mimetypes.guess_type('book.m4b')[0])"` → `audio/mp4`. Undo with `reg delete "HKCU\Software\Classes\.m4b" /v "Content Type" /f`.

Linux and macOS give `.m4b` an audiobook type of their own (Linux: `audio/x-m4b`, a subtype of `audio/mp4`). `audio/mp4` — what Windows itself uses for `.m4a` — was read as audio by every app and OS we tested on: Android, iOS/macOS and Linux.

A file already sent with the wrong label keeps it: re-send it after the fix. Changing the m4b's own brand (e.g. Apple's `M4B `) doesn't help — the label comes from the sender, not the file — and it makes some apps list the book as an unknown file, so Stage 5 keeps the generic brand on every platform.

## Validation run

Windows 11 Pro, RTX 5090 (32 GB, driver 617.42), llama.cpp b11552 (Vulkan, winget), torch 2.8.0+cu128, no Triton. Input: two essay chapters (~23k characters) converted from a scanned PDF, run as `./scripts/run_pipeline.sh … --mode lecture --book-title "…: Excerpts"` from Git Bash with an auto-cast narrator.

| Step | Result |
| --- | --- |
| LLM phase (ingest → voice cast → speech-normalize) | ~1.5 min for 91 beats; Gemma 4 12B QAT at 64k context took ~10 GB, all on the discrete GPU |
| llama-server shutdown | Process gone and VRAM back to idle before Dramabox loaded |
| s4 denoise (141 chunks) | 10 min 13 s for 34.1 min of audio: ~3.3× faster than real time, without `torch.compile` |
| s4 restarts | `refs` relaunched twice on the first run (the WinError 1314 download race above), then completed; no other restarts |
| s5 mix | 2-chapter `.m4b`, −18.0 LUFS integrated, −1.8 dBFS true peak; file named `…_ Excerpts.m4b`, embedded title `…: Excerpts` |
| Wall clock | ~25 min end to end, including ~45 GB of first-run model downloads |

## Tips

- **Use `127.0.0.1`, not `localhost`.** Windows resolves `localhost` to `::1` first, and the serve scripts bind IPv4 only, so every new connection pays about 2 s before falling back (llama-server drops idle keep-alive connections after 5 s). Exporting `LNVOX_LLM_ENDPOINT=http://127.0.0.1:8000/v1` avoids it.
- **ComfyUI:** install the pack with a directory junction (`mklink /J`) rather than a symlink — see the [ComfyUI guide](/guides/comfyui/#install).
- **VibeVoice** (`--tts-backend vibevoice`) hasn't been validated on Windows; `prepare_vibevoice_env` would need the same cu128 torch handling as Dramabox.

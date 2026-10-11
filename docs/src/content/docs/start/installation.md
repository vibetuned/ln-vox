---
title: Installation
description: Install ln-vox on Linux + CUDA, Apple Silicon or Windows — uv, ffmpeg, llama.cpp, Dramabox (and optionally VibeVoice), the render extra, and a voicebank.
sidebar:
  order: 2
---

ln-vox is a uv project (Python 3.13, pinned in `.python-version`) plus three things uv can't install for you: **ffmpeg**, the **llama-server** binary, and the **Dramabox** checkout. Run everything from the repository root.

## 1. System tools

| Tool | Why | Install |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | Every command runs as `uv run …`; uv also fetches Python 3.13 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| ffmpeg (+ ffprobe) | Stage 5 mixing and encoding; voicebank seeding | `apt install ffmpeg` / `brew install ffmpeg` |
| llama.cpp's `llama-server` | The default LLM backend | `brew install llama.cpp` on macOS; `winget install ggml.llamacpp` on Windows; on Linux build it with CUDA (`cmake -B build -DGGML_CUDA=on && cmake --build build --target llama-server`) and put it on `PATH` |
| git, curl | Setup scripts clone repos; the launcher probes the LLM server | usually present |

## 2. Python dependencies

```bash
uv sync --extra voice --extra tts
```

The extras, from `pyproject.toml`:

| Extra | Contents | Needed for |
| --- | --- | --- |
| `voice` | soundfile, librosa, tqdm | voicebank seeding, casting audit, trimming |
| `tts` | soundfile, huggingface-hub, soxr | Stage 4 (Dramabox / VibeVoice clients) |
| `serve` | vLLM (nightly cu130 wheels), transformers, torch — Linux only | `--llm-backend vllm` |
| `mlx` | mlx-lm (git-pinned) — macOS only | `--llm-backend mlx` |
| `render` | Pygments, Playwright | lecture mode: rendering code/tables to PNG (`uv run playwright install chromium` once) |

You don't have to juggle these by hand for pipeline runs: the launcher's [`prepare_llm_env` / `prepare_tts_env`](/launcher/functions/) sync the right combination before each phase. vLLM and Dramabox need incompatible torch builds, so never try to keep `--extra serve` and Dramabox in one venv.

## 3. Dramabox (the default TTS engine)

```bash
./scripts/setup_dramabox.sh
```

Clones Resemble AI's Dramabox into `external/DramaBox/` and installs its requirements with per-platform torch handling — x86_64 keeps its `torch==2.8.0+cu128` pin, aarch64 (DGX Spark, Jetson) pulls `torch>=2.10` from the cu130 index, Darwin installs MPS torch without bitsandbytes. Weights (~15 GB) download from Hugging Face on the first render, plus a ~23 GB unquantized Gemma prompt encoder used by the default [high-quality recipe](/guides/render-quality/). Details: [helper scripts](/launcher/helper-scripts/).

Optional second engine:

```bash
./scripts/setup_vibevoice.sh               # clone + editable install of the community fork
./scripts/setup_vibevoice.sh --weights-hf  # also fetch VibeVoice-Large (~18 GB) into models/
```

## 4. A voicebank

Casting needs reference clips. Seed the English bank once from Mozilla Common Voice (download the tarball from the Mozilla Data Collective and extract it under `data/`):

```bash
uv run lnvox voice seed-cv data/<cv-corpus-NN.N-YYYY-MM-DD>/en/ --max-speakers 400 --rank-by-quality
uv run lnvox voice list
```

Seeding is CPU/ffmpeg work (~30 min for 400 speakers). Already have a bank on another machine? Copy `voicebank/` (it's just `manifest.json` + `clips/`). Other languages get their own bank, selected with `LNVOX_VOICEBANK=voicebank-fr`. See [Building the voicebank](/stages/voicebank/).

## 5. Check it

```bash
uv run lnvox --help                 # the CLI imports
llama-server --version              # the default LLM backend is on PATH
ffmpeg -version | head -1
uv run python -c "import torch; print(torch.cuda.is_available(), torch.backends.mps.is_available())"
```

## Apple Silicon

Same steps, with Homebrew for the tools and the `mlx` extra if you want the Apple-native LLM backend:

```bash
brew install ffmpeg llama.cpp
uv sync --extra mlx --extra voice --extra tts
./scripts/setup_dramabox.sh
```

Everything MPS-specific (dtype map, chunk caps, memory sizing, caffeinate) is on the [Apple Silicon guide](/guides/apple-silicon/).

## Windows

Same steps on Windows 11 + NVIDIA, with winget for the tools. The scripts are bash, so run them from **Git Bash** (part of Git for Windows); open a new one after installing so `PATH` picks up the tools:

```bash
winget install Git.Git
winget install astral-sh.uv
winget install Gyan.FFmpeg
winget install ggml.llamacpp
uv sync --extra voice --extra tts
./scripts/setup_dramabox.sh       # Dramabox's torch pin from the cu128 index
# Register .m4b as audio, or books shared from Windows arrive as "bin" files
MSYS_NO_PATHCONV=1 reg add "HKCU\Software\Classes\.m4b" /v "Content Type" /d "audio/mp4" /f
```

What differs (llama.cpp-only LLM backend, no RE-USE denoising, `torch.compile` only with Triton, sleep prevention, file names, [the `.m4b` MIME type](/guides/windows/#sharing-the-m4b)) is on the [Windows guide](/guides/windows/).

## Optional tools

- **Voicebank Studio** — `uv pip install PySide6`, then `uv run python scripts/voicebank_studio.py`. [Guide](/guides/voicebank-studio/)
- **ComfyUI nodes** — symlink `comfyui/` into `ComfyUI/custom_nodes/`. [Guide](/guides/comfyui/)
- **This documentation site** — `cd docs && npm install && npm run dev`.

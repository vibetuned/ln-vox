---
title: Quickstart
description: From an EPUB to a finished, synced .m4b in five commands — import, pick a narrator, smoke-test, run, listen.
sidebar:
  order: 3
---

This walks one novel through the whole pipeline. It assumes [installation](/start/installation/) is done and a voicebank is seeded.

## 1. Put the book in place

From an EPUB (the usual case — it also enables the text-sync layer):

```bash
mkdir -p epubs/my-novel
cp ~/Downloads/volume-01.epub epubs/my-novel/volume-01.epub
uv run lnvox ingest-epub epubs/my-novel/volume-01.epub novels/my-novel/volume-01
```

This writes one `NN-<slug>.txt` per narrative chapter, the illustrations under `images/`, and `.epub_meta.json`. Skim the `.txt` files: if a whole book landed in two or three files, re-run with `--split-chapters` ([Stage 0a](/stages/ingest-epub/)).

No EPUB? Put one `.txt` per chapter in `novels/my-novel/volume-01/` — files sort by name, and the first non-empty line of each is its title.

## 2. Pick the narrator by ear

The narrator voices most of a third-person novel, so choose it yourself:

```bash
uv run lnvox voice list                         # bank composition by gender × age
uv pip install PySide6
uv run python scripts/voicebank_studio.py       # listen, note a clip id like cv_051e865815e5
```

## 3. Smoke-test the casting (optional, recommended)

```bash
./scripts/run_pipeline.sh my-novel/volume-01 \
    --narrator-clip cv_051e865815e5 --stop-after-audit
```

This runs the whole LLM phase except the director, prints the casting audit and stops before anything is rendered — the one moment a recast costs nothing. Fix collisions by ear in the Studio's Casting tab ([hand casting](/guides/casting/)).

## 4. Run it

```bash
./scripts/run_pipeline.sh my-novel/volume-01 \
    --narrator-clip cv_051e865815e5 \
    --book-title "My Novel — Volume 1"
```

The finished stages are no-ops, so this continues from where step 3 stopped: s3 → LLM server stopped → s4 (hours) → s5 → s6. If anything dies, run the same command again; every stage resumes from its cache.

## 5. Listen

```text
artifacts/my-novel/volume-01/06_final/My Novel — Volume 1.m4b
artifacts/my-novel/volume-01/07_sync/sync_manifest.json     # + the synced EPUB
```

The next volume needs no narrator flag — it inherits every recurring voice:

```bash
uv run lnvox ingest-epub epubs/my-novel/volume-02.epub novels/my-novel/volume-02
./scripts/run_pipeline.sh my-novel/volume-02 --book-title "My Novel — Volume 2"
```

:::tip[Try a slice first]
A full novel is an overnight render. To hear the voices after the LLM phase without committing to it, render a few beats and mix them:

```bash
./scripts/run_pipeline.sh my-novel/volume-01 --narrator-clip cv_… --skip-tts --skip-mix --skip-sync
./scripts/run_pipeline.sh --prepare-env tts     # swap the venv to the Dramabox runtime
uv run lnvox s4 my-novel/volume-01 --staged --limit 40
uv run lnvox s5 my-novel/volume-01 --title "preview"
```
:::

Prefer a graph? The same steps are [ComfyUI nodes](/guides/comfyui/), with an example workflow per mode.

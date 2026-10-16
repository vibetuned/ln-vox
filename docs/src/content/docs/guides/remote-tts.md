---
title: Rendering on another machine
description: Run the LLM stages here and Stage 4 on a GPU box over SSH — a DGX Spark, a cloud instance — with lnvox remote-tts, which syncs the book and the voicebank, renders detached, queues books, reports progress and fetches the audio.
sidebar:
  order: 8
---

Stage 4 is the long part of a book — hours of GPU time. `lnvox remote-tts` hands it to another machine reached over SSH, so this one can sleep or shut down while it renders: a **DGX Spark** on the desk, or a **cloud GPU instance**. The LLM stages, the mix and the sync stay here.

```bash
# 1. The LLM phase here, then Stage 4 on the box — the launcher stops there.
./scripts/run_pipeline.sh my-book/volume-01 --mode lecture \
    --narrator-clip manual_narrator --book-title "My Book" \
    --remote-tts gpu-box

# 2. Later, as often as you like: progress — and once it's done, the fetch, mix and sync.
./scripts/run_pipeline.sh my-book/volume-01 --mode lecture \
    --narrator-clip manual_narrator --book-title "My Book" \
    --remote-tts gpu-box --skip-llm
```

Or drive it directly — the launcher's `--remote-tts` runs exactly this:

```bash
uv run lnvox remote-tts my-book/volume-01 --host gpu-box   # an ~/.ssh/config alias, or user@host
uv run lnvox remote-tts my-book/volume-01                  # the host is remembered per book
```

## What one run does

Each run looks at the book's state on the box and moves it one step forward:

| State on the box | What the run does | Exit |
| --- | --- | --- |
| nothing launched | clones or updates the box's checkout, syncs the book's inputs and the voicebank, starts the render | 3 |
| another book rendering | syncs, and **queues** this one: it starts on its own when the GPU frees | 3 |
| queued | shows its place in the queue | 3 |
| rendering | shows its progress; `--pull` fetches the chapters already placed | 3 |
| interrupted (the box rebooted, the process was killed) | syncs and starts again; the render resumes from its checkpoints and the cache | 3 |
| done | fetches `05_audio`, and prints the launcher line that mixes and syncs it here | 0 |
| done, but the beats or voices changed here since | syncs and renders again — unchanged beats come straight from the box's cache | 3 |
| failed or stopped | shows the log; `--restart` launches it again | 1 |

`--status` only reports; `--stop` stops the render (or takes a queued book out of the queue); `--wait` stays connected, reporting every `--interval` seconds (default 300), and fetches the render once it's done.

```text
gpu-box — gpu-box, NVIDIA GB10, 96 % — /home/me/ln-vox @ 2fa8b39 (main)
remote-test/volume-01: rendering — started 13 Oct 19:32 (4 min ago)
  3 renders (0 cached before this run), 4 chunks
  ctx          4/4      100%
  denoise      4/4      100%
  decode       0/3        0%  ◀
  chapters placed: 0/1 (0/3 beats)
```

The staged renderer sweeps the whole book one phase at a time — prompt encoding (`ctx`), the diffusion loop (`denoise`, most of the time), then `decode`, which places each chapter's WAVs — so progress is shown per phase, with the rate of the last half hour and the time left in the current phase.

## The queue

One GPU renders one book at a time. Launching a book while another renders syncs it and puts it in the box's queue (`artifacts/_remote/queue.json` there); a render that ends — done or failed, not stopped — starts the next queued book itself, so a series of volumes renders back to back with nothing connected. Running `remote-tts` again for a queued book keeps its place; `--stop` takes it out.

## What moves

- **To the box**, before a launch: the book's Stage 0–4 artifacts (`00_*` … `04_*`: the text, cast, scenes, directed beats and voice assignments), the whole **voicebank** (`$LNVOX_VOICEBANK`, default `voicebank/` — it isn't in git) and the agent the box uses to run its queue. Directed chapters deleted here are deleted there.
- **From the box**, once it's done (or with `--pull`, finished chapters while it renders): `05_audio/` — each chapter's WAVs and manifest, the `_staged/` intermediates excluded.

Only files whose size or modification time differ are copied — rsync's quick check, without needing rsync: the files travel as tar streams over `ssh`, which Git for Windows, macOS and Linux all have. A voicebank goes over once; later launches send only what changed.

## The box

What it needs: key-based SSH from here (`ssh <host>` must log in without a password — an `~/.ssh/config` alias works), `git`, `tar`, `python3` and [uv](https://docs.astral.sh/uv/) (in `~/.local/bin` is fine), an NVIDIA GPU, and — for the model downloads — a Hugging Face login (`huggingface-cli login`) if the models need one.

- **The checkout**: `--remote-dir`, default `~/ln-vox`, cloned from this checkout's `origin` when missing; otherwise fetched and fast-forwarded to this checkout's branch (`--branch` to pick another). A checkout with local changes that can't fast-forward is reported, never overwritten. The box runs what is **pushed**: the run warns when this checkout is ahead, or has uncommitted changes. `--no-git` leaves the checkout alone.
- **The first render** on a box installs the TTS environment — `uv sync`, then `scripts/setup_dramabox.sh` (Dramabox, its CUDA torch, the RE-USE kernels; aarch64 builds on a DGX Spark) — and downloads the model weights on its first render (about 15 GB, unless the Hugging Face cache already has them). Then every render runs `scripts/run_pipeline.sh <book> --skip-llm --skip-mix --skip-sync`: the launcher's own env preparation and the staged Stage 4 with its crash recovery.
- **The job** is `artifacts/<book>/_remote/job.sh` on the box, started in its own session so it outlives the SSH connection; its state is in `_remote/state.json` and its log in `_remote/job.log` (the previous run's in `job.prev.log`). Its render cache, `cache/tts/` on the box, is separate from this machine's — a render recipe can differ per device anyway.

Validated on a DGX Spark (GB10, Ubuntu 24.04) driven from Windows 11: a fresh checkout cloned and switched to the working branch, the 287 MB voicebank synced in seconds, the TTS environment installed and a three-beat book rendered in under five minutes, the WAVs fetched, and a second book queued behind a third and started by it.

## After the fetch

The audio is here, rendered; mix and sync it with the line the run prints, which skips the LLM phase and Stage 4:

```bash
./scripts/run_pipeline.sh my-book/volume-01 --skip-llm --skip-tts --mode lecture \
    --book-title 'My Book'
```

This machine's own TTS cache doesn't have these renders: a later **local** Stage 4 on the book would render it again. Keep Stage 4 on the box (`--remote-tts`), or `--skip-tts`.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Permission denied (publickey…)` | SSH needs a key login: `ssh-copy-id <host>`, or an `~/.ssh/config` entry with `IdentityFile`. Password prompts can't work here (`BatchMode`). |
| `git on the box: … Not possible to fast-forward` | The box's checkout has its own commits or changes. Sort them out there, or run with `--no-git` to render with what it has. |
| `failed, exit 1` with a CUDA or download error in the log | Fix it on the box (driver, disk space, `huggingface-cli login`), then `--restart`. |
| `interrupted` | The box restarted or the job was killed. The next run relaunches it; nothing rendered is lost. |
| A queued book never starts | Its predecessor was stopped, or the box restarted: run `remote-tts` for that book — with the GPU free it starts at once. |

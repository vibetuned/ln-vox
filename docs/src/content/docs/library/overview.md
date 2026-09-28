---
title: Library overview
description: Map of the lnvox Python package — what each subpackage and module does, how they depend on each other, and where to start reading.
sidebar:
  order: 1
  label: Overview
---

`lnvox` (in `src/lnvox/`) is a plain Python package with a Typer CLI on top. Every stage is a module with a `run(...)` function that takes already-loaded inputs (pydantic models, a client, output paths) and writes its artifacts; the CLI in `cli.py` does the file loading, prints progress, and calls it. That split makes every stage usable from your own Python:

```python
from pathlib import Path
from lnvox.config import Settings
from lnvox.ingest.text import read_jsonl
from lnvox.llm.client import LLMClient
from lnvox.stages import s1_characters

book = Settings().artifacts_dir / "my-novel/volume-01"
chapters = read_jsonl(book / "00_text.jsonl")
cast = s1_characters.run(chapters, LLMClient(), book)      # writes 01_characters.json
print([c.name for c in cast.characters])
```

## Package map

| Module | Role |
| --- | --- |
| [`lnvox.config`](/library/config/) | `Settings` / `LLMConfig`: artifacts dir and LLM endpoint, model, budgets, timeouts from `LNVOX_*` env vars |
| [`lnvox.series`](/library/series/) | Book ids and prior-volume detection |
| `lnvox.cli` | The `lnvox` commands — see the [CLI reference](/reference/cli/) |
| **`lnvox.llm`** | |
| [`client`](/library/llm/client/) | `LLMClient` — OpenAI-compatible calls with schema-validated, retried structured output |
| [`chunker`](/library/llm/chunker/) | Splits chapters into paragraph-aligned chunks that fit the context |
| [`schemas`](/library/llm/schemas/) | Pydantic models for every stage's JSON (characters, scenes, directed beats, scripts, profiles) |
| [`prompts`](/library/llm/prompts/) | The Jinja prompt templates, one per LLM task |
| **`lnvox.ingest`** | |
| [`text`](/library/ingest/text/) | Folder of `.txt` → `00_text.jsonl`; JSONL helpers |
| [`epub`](/library/ingest/epub/) | EPUB → `novels/` layout, images, `.epub_meta.json`; chapter splitting |
| [`blocks`](/library/ingest/blocks/) | Lecture-mode block classification and drop rules |
| [`render`](/library/ingest/render/) | Code / table / figure → PNG (Pygments + Playwright) |
| [`scenario`](/library/ingest/scenario/) | Theater script → structured, verbatim-validated `00_script.json` |
| **`lnvox.voices`** | |
| [`schema`](/library/voices/schema/) | Voicebank and casting models (`VoiceClip`, `BookCasting`, …) |
| [`manifest`](/library/voices/manifest/) | Load / save / summarize `voicebank/manifest.json` |
| [`common_voice`](/library/voices/common-voice/) | Seed a bank from Common Voice, with optional quality ranking |
| [`matcher`](/library/voices/matcher/) | Stage V: cross-volume reuse + LLM target inference and ranked matching |
| [`acoustics`](/library/voices/acoustics/) | Per-clip pitch measurements (cached) for the audit |
| [`audit`](/library/voices/audit/) | Stage A: scene co-occurrence, voice collisions, repair |
| **`lnvox.stages`** | |
| [`s1_characters`](/library/stages/s1-characters/) | Per-chapter extraction, clustering, LLM merge groups |
| [`s2_scenes`](/library/stages/s2-scenes/) | Scene boundaries, then beats with speakers and `source_span` |
| [`s3_director`](/library/stages/s3-director/) | Voice profiles, per-scene directions, prompt format, beat merging/splitting |
| [`lecture`](/library/stages/lecture/) | Lecture-mode beats and speech normalization |
| [`scenario`](/library/stages/scenario/) | Scenario-mode emotion + cue direction |
| [`s4_tts`](/library/stages/s4-tts/) | Monolithic per-beat render loop with the content-hash cache |
| [`s4_vibevoice`](/library/stages/s4-vibevoice/) | VibeVoice session planning and rendering |
| [`s5_mix`](/library/stages/s5-mix/) | ffmpeg mix → chaptered, loudness-normalized `.m4b` |
| [`s6_sync`](/library/stages/s6-sync/) | Re-align beats onto the EPUB; sync manifest |
| [`scenario_sync`](/library/stages/scenario-sync/) | Scenario timeline: `play.json`, per-scene files, SRT |
| [`sample`](/library/stages/sample/) | Cut a demo sample from a finished book |
| **`lnvox.tts`** | |
| [`schema`](/library/tts/schema/) | `RenderedBeat`, `ChapterAudio` — the `05_audio/<ch>/manifest.json` format |
| [`dramabox_client`](/library/tts/dramabox-client/) | `DramaboxClient` and the render-recipe knobs that shape the cache key |
| [`staged`](/library/tts/staged/) | The staged s4 plan and its four phases (refs, ctx, denoise, decode) |
| [`staged_driver`](/library/tts/staged-driver/) | Runs each phase in a subprocess with resume and a stall guard |
| [`trim`](/library/tts/trim/) | Edge-silence trim applied to every rendered WAV |
| [`vibevoice_client`](/library/tts/vibevoice-client/) | `VibeVoiceClient` — multi-speaker session generation |
| [`lab_engine`](/library/tts/lab-engine/) | Warm in-process engine for the Studio's TTS Lab on MPS |

## Dependencies

```text
cli ─► stages.* ─► llm.client ─► config
          │   └──► llm.schemas, llm.chunker, llm.prompts
          ├──────► voices.* (matcher, audit, manifest, schema)
          └──────► tts.*    (s4 only: dramabox_client, staged, trim, vibevoice_client)
ingest.* ─► llm.client (lecture fallback classifier, scenario structuring)
```

Heavy dependencies are imported lazily: torch, Dramabox and VibeVoice only inside the TTS modules' functions, Playwright only when rendering. So the CLI starts, and the LLM stages run, in a venv without the TTS runtime.

## Where to start reading

- **How a book flows through**: [stages overview](/stages/overview/), then `cli.py` top to bottom — each command is short and names the library call it makes.
- **The data**: [`lnvox.llm.schemas`](/library/llm/schemas/) — every artifact between s1 and s3 is one of these models.
- **Talking to the LLM**: [`LLMClient`](/library/llm/client/), then any stage's use of `client.structured(...)`.
- **Rendering**: [`s4_tts`](/library/stages/s4-tts/) for the simple loop, then [`staged`](/library/tts/staged/) for what the launcher actually runs.

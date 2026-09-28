---
title: lnvox.llm.chunker
description: Paragraph splitting and LLM-chosen chunk boundaries for long chapters — split_paragraphs, chunk_text, Chunk, and the break-selection fallback.
sidebar:
  order: 2
  label: chunker
---

`lnvox.llm.chunker` (`src/lnvox/llm/chunker.py`) cuts a long chapter into paragraph-aligned chunks small enough for one scene-boundary call in [Stage 2](/stages/s2-scenes/). Naive cuts at a paragraph boundary can land mid-conversation and break speaker attribution, so near each cut point the module shows the LLM a few candidate paragraphs and lets it pick the cleanest narrative break. It also owns [`split_paragraphs`](#split_paragraphs), the single definition of "paragraph" that s2, lecture mode, and the EPUB ingest's visual-element anchors all index into.

## At a glance

| Name | Kind | Used by |
| --- | --- | --- |
| [`split_paragraphs(text)`](#split_paragraphs) | function | `s2_scenes.segment_chapter`, `lecture.split_chapter`, `chunk_text` |
| [`chunk_text(client, text, *, target_chars, candidate_count)`](#chunk_text) | function | `s2_scenes.segment_chapter` |
| [`Chunk`](#chunk) | pydantic model | s2 pass 2a (`_detect_boundaries`) |
| `DEFAULT_TARGET_CHARS = 30000` | constant | default `target_chars` |
| `DEFAULT_CANDIDATE_COUNT = 6` | constant | default `candidate_count` |
| [`_ask_llm_for_break`](#_ask_llm_for_break), `_Choice`, `_preview`, `_SYSTEM` | private | the LLM break-point call |

## API

### `split_paragraphs`

```python
def split_paragraphs(text: str) -> list[str]
```

Splits on `"\n\n"` and drops pieces that are empty or whitespace-only. Paragraph text isn't otherwise stripped. This is the **single source of truth for paragraph numbering**:

- s2a's scene ranges (`start_paragraph` / `end_paragraph`, 1-indexed) and s2b's per-scene slices index into this list.
- Lecture mode records the 0-indexed position as `DirectedBeat.source_paragraph`.
- In lecture mode, `ingest-epub` writes chapter bodies so that each block's `after_paragraph` lines up with this split. Stage 6 relies on that to place code, table and figure elements.

If you change the paragraph definition, existing `02_scenes/` and `03_directed/` artifacts and visual-element anchors stop matching. Re-run from ingest.

### `chunk_text`

```python
def chunk_text(
    client: LLMClient,
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
) -> list[Chunk]
```

Returns consecutive, non-overlapping [`Chunk`](#chunk)s that together cover every paragraph of `text` in order.

**Algorithm**

1. `paragraphs = split_paragraphs(text)`. If there's at most one paragraph, or `len(text) <= target_chars`, return a single chunk (`paragraphs or [text]`) without calling the LLM.
2. Precompute each paragraph's cumulative end offset, counting `len(p) + 2` per paragraph for the separator.
3. From the current start paragraph:
   - If the remaining characters are at most `int(target_chars * 1.3)`, take everything left as the final chunk. This avoids a tiny trailing chunk.
   - Otherwise find the **ideal** paragraph, the first whose end offset reaches `start + target_chars`.
   - Build a window of `candidate_count` paragraph indices centred on it: `lo = max(start + 1, ideal - candidate_count // 2)`, `hi = min(n, lo + candidate_count)`, then `lo = max(start + 1, hi - candidate_count)`.
   - With two or more candidates, ask the LLM ([`_ask_llm_for_break`](#_ask_llm_for_break)) which one to split **after**. With one, use it. With none, use the ideal index.
   - Emit `paragraphs[start : chosen + 1]`, then continue from `chosen + 1`.

**Properties**

- Chunks hold whole paragraphs. A single paragraph longer than `target_chars` is never split.
- Every non-final chunk has at least two paragraphs, because candidates start at `start + 1`. The final "take the rest" chunk can be a single paragraph.
- Chunk sizes vary around `target_chars`: the window reaches about `candidate_count // 2` paragraphs on either side of the ideal cut, so a chunk can end up noticeably shorter or longer depending on paragraph lengths.
- The result isn't cached. s2 caches the whole chapter's `ChapterScenes` in `02_scenes/<chapter_id>.json`, so re-running s2 on a finished chapter never re-chunks it.

**Example**

```python
from lnvox.llm.client import LLMClient
from lnvox.llm.chunker import chunk_text

chunks = chunk_text(LLMClient(), chapter.text)          # 30k-char targets
for c in chunks:
    first = c.base_paragraph + 1                         # 1-indexed, chapter-global
    last = c.base_paragraph + len(c.paragraphs)
    print(f"paragraphs {first}..{last}")
```

### `Chunk`

```python
class Chunk(BaseModel):
    base_paragraph: int
    paragraphs: list[str]
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `base_paragraph` | `int` | required | 0-indexed position of `paragraphs[0]` in the chapter's full `split_paragraphs` list. |
| `paragraphs` | `list[str]` | required | The chunk's paragraphs, verbatim. |

s2a numbers a chunk's paragraphs locally (`[1]..[N]`) in the prompt and adds `base_paragraph` to the returned ranges, so the stored scene ranges are chapter-global (`base_paragraph + local_index + 1`). Nothing downstream needs per-chunk offsets.

### `_ask_llm_for_break`

```python
def _ask_llm_for_break(client: LLMClient, paragraphs: list[str]) -> int
```

Returns the 0-indexed position within `paragraphs` (the candidate window) after which to split.

- With at most one paragraph, returns `0` without calling the LLM.
- Otherwise it numbers the candidates `[1]..[N]` and shortens each one with `_preview(p, max_len=600)` (first 300 chars, a `[…paragraph continues, total N chars…]` marker, last 300 chars). The prompt, built inline rather than from a Jinja template, lists what makes a good split (scene end, closed conversation, resolved thought) and a bad one (a setup whose answer comes next, mid back-and-forth).
- It calls `client.structured(system=_SYSTEM, user=…, schema=_Choice, max_tokens=512)`, where `_Choice` has `split_after_paragraph: int` (1-indexed) and `reason: str`.
- **Any failure falls back to the middle candidate** (`len(paragraphs) // 2`): an exception from `structured()` (including a dead server) or an out-of-range index. The fallback is silent.

:::note
Because failures here are swallowed, a server that dies during chunking isn't noticed until s2's next call (`_detect_boundaries`), which does propagate errors. The run then fails and the launcher retries. Chunks computed before the failure are discarded with the chapter, since nothing is cached below the chapter level.
:::

## Notes

- `target_chars=30000` is about 8.5k input tokens at the client's 3.5 chars/token estimate, which leaves room in the 65536-token default context for the numbered-paragraph prompt and the boundary JSON. s2 always uses the defaults. There's no CLI flag or env var for them.
- Each break call costs at most `3 × (90 + 512 × 0.25) = 654` s of timeout with default settings (the OpenAI SDK no longer retries on its own; only connection errors are re-sent). See [LLMClient retry semantics](/library/llm/client/#retry-semantics).

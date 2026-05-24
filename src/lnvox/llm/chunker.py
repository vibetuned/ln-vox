"""LLM-guided text chunking.

Splits long chapters into chunks small enough for the segmentation LLM to
process in one call. Naive paragraph-boundary chunking can cut a chunk
mid-conversation, breaking speaker attribution. This module asks the LLM to
choose the cleanest narrative break among a small set of candidates near the
target chunk size.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from lnvox.llm.client import LLMClient


_SYSTEM = (
    "You select narrative break points for splitting a novel chapter into chunks. "
    "Output only the requested JSON. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation around the JSON."
)


DEFAULT_TARGET_CHARS = 30000
DEFAULT_CANDIDATE_COUNT = 6


class _Choice(BaseModel):
    split_after_paragraph: int = Field(
        description="1-indexed paragraph number AFTER which the chunk should end."
    )
    reason: str = Field(
        description="One short sentence on why this is a clean narrative break."
    )


def _preview(text: str, max_len: int = 600) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return f"{text[:half]}\n[…paragraph continues, total {len(text)} chars…]\n{text[-half:]}"


def _ask_llm_for_break(client: LLMClient, paragraphs: list[str]) -> int:
    """Return 0-indexed paragraph index after which to split. Falls back to
    the middle paragraph on any failure."""
    if len(paragraphs) <= 1:
        return 0

    numbered = "\n\n".join(
        f"[{i + 1}]\n{_preview(p)}" for i, p in enumerate(paragraphs)
    )
    user = (
        "Below are several consecutive paragraphs from a novel chapter. "
        "Pick the paragraph AFTER WHICH the chapter should be split into two chunks.\n\n"
        "A GOOD split is right after a paragraph that:\n"
        "  - Ends a scene (next paragraph shifts location, time, or POV).\n"
        "  - Closes out a conversation (next paragraph starts something new).\n"
        "  - Resolves a thought rather than leaving one mid-action.\n\n"
        "A BAD split is right after a paragraph that:\n"
        "  - Sets up dialogue whose answer comes in the next paragraph.\n"
        "  - Asks a question answered immediately afterward.\n"
        "  - Sits in the middle of an ongoing back-and-forth between characters.\n\n"
        f"Paragraphs (1 through {len(paragraphs)}):\n\n{numbered}\n\n"
        'Return JSON: {"split_after_paragraph": N, "reason": "..."}'
    )

    try:
        choice = client.structured(
            system=_SYSTEM, user=user, schema=_Choice, max_tokens=512
        )
        idx = choice.split_after_paragraph - 1
        if 0 <= idx < len(paragraphs):
            return idx
    except Exception:
        pass
    return len(paragraphs) // 2


def chunk_text(
    client: LLMClient,
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
) -> list[str]:
    """Split `text` into chunks at LLM-chosen paragraph boundaries.

    Each chunk targets `target_chars` characters. For each cut point the LLM
    sees `candidate_count` paragraphs centred on the ideal position and picks
    the cleanest one.
    """
    if len(text) <= target_chars:
        return [text]

    paragraphs = text.split("\n\n")
    if len(paragraphs) <= 1:
        return [text]

    # Cumulative end-char offset per paragraph (inclusive of the \n\n separator).
    ends: list[int] = []
    pos = 0
    for p in paragraphs:
        pos += len(p) + 2
        ends.append(pos)

    chunks: list[str] = []
    start_para = 0
    start_char = 0

    while start_para < len(paragraphs):
        remaining_chars = ends[-1] - start_char
        # If what's left is comfortably within one chunk, take all of it.
        if remaining_chars <= int(target_chars * 1.3):
            chunks.append(text[start_char:].strip())
            break

        ideal_end_char = start_char + target_chars
        ideal_idx = start_para
        for i in range(start_para, len(paragraphs)):
            if ends[i] >= ideal_end_char:
                ideal_idx = i
                break

        half = candidate_count // 2
        lo = max(start_para + 1, ideal_idx - half)
        hi = min(len(paragraphs), lo + candidate_count)
        lo = max(start_para + 1, hi - candidate_count)
        candidate_indices = list(range(lo, hi))

        if not candidate_indices:
            chosen_para_idx = ideal_idx
        elif len(candidate_indices) == 1:
            chosen_para_idx = candidate_indices[0]
        else:
            chosen_local = _ask_llm_for_break(
                client, [paragraphs[i] for i in candidate_indices]
            )
            chosen_para_idx = candidate_indices[chosen_local]

        chunk_end = ends[chosen_para_idx]
        chunks.append(text[start_char:chunk_end].strip())
        start_para = chosen_para_idx + 1
        start_char = chunk_end

    return chunks

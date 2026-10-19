"""Stage 2 regression tests: scene-range tiling (Pass 2a) and output budgets.

The LLM is stubbed; no model is called. All prose is invented.
Runnable directly (`python tests/test_s2_scenes.py`) or under pytest.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.ingest.text import Chapter
from lnvox.llm.client import LLMClient, _jinja
from lnvox.llm.schemas import (
    Beat,
    ChapterBoundaries,
    CharacterList,
    SceneBeats,
    SceneBoundary,
)
from lnvox.stages import s2_scenes
from lnvox.stages.s2_scenes import _tile_boundaries


def _b(start: int, end: int, sid: str = "x") -> SceneBoundary:
    return SceneBoundary(scene_id=sid, start_paragraph=start, end_paragraph=end)


def _ranges(bs) -> list[tuple[int, int]]:
    return [(b.start_paragraph, b.end_paragraph) for b in bs]


# ---- _tile_boundaries ---------------------------------------------------------


def test_clean_tiling_is_untouched():
    tiled, notes = _tile_boundaries([_b(1, 3), _b(4, 7)], 7)
    assert _ranges(tiled) == [(1, 3), (4, 7)] and notes == []


def test_gap_is_folded_into_the_earlier_scene():
    tiled, notes = _tile_boundaries([_b(1, 2), _b(5, 7)], 7)
    assert _ranges(tiled) == [(1, 4), (5, 7)]
    assert any("gap 3–4" in n for n in notes)


def test_overlap_is_trimmed_off_the_later_scene():
    tiled, notes = _tile_boundaries([_b(1, 4), _b(3, 7)], 7)
    assert _ranges(tiled) == [(1, 4), (5, 7)]
    assert any("overlap" in n for n in notes)


def test_head_tail_unsorted_contained_and_out_of_range():
    tiled, notes = _tile_boundaries(
        [_b(6, 40), _b(3, 4), _b(3, 3), _b(2, 5)], 9
    )
    # Sorted by start, 2–5 comes first and takes the head (1–5); 3–3 and 3–4
    # are then fully covered → dropped; 6–40 is clamped to 6–9.
    assert _ranges(tiled) == [(1, 5), (6, 9)], _ranges(tiled)
    assert len(notes) == 4, notes
    covered = [p for b in tiled for p in range(b.start_paragraph, b.end_paragraph + 1)]
    assert covered == list(range(1, 10))


def test_tail_gap_extends_the_last_scene():
    tiled, _ = _tile_boundaries([_b(1, 3)], 6)
    assert _ranges(tiled) == [(1, 6)]


# ---- segment_chapter with a stub LLM -------------------------------------------


class _StubClient:
    """Just enough LLMClient surface for s2: render, budget_for, structured."""

    _CHARS_PER_TOKEN = LLMClient._CHARS_PER_TOKEN
    budget_for = LLMClient.budget_for

    def __init__(self, scenes: list[tuple[int, int]], max_model_len: int = 65536):
        self.settings = SimpleNamespace(llm=SimpleNamespace(max_model_len=max_model_len))
        self.scenes = scenes
        self.calls: list[tuple[str, int, int]] = []  # (schema, prompt chars, max_tokens)

    def render(self, template: str, **kw) -> str:
        return _jinja.get_template(template).render(**kw)

    def structured(self, *, system, user, schema, max_tokens=None, **_):
        self.calls.append((schema.__name__, len(system) + len(user), max_tokens))
        if schema is ChapterBoundaries:
            return ChapterBoundaries(
                chapter_id="01",
                scenes=[_b(s, e, f"01_s{i}") for i, (s, e) in enumerate(self.scenes, 1)],
            )
        if schema is SceneBeats:
            # One narration beat per paragraph of the scene text.
            text = user.split("Scene text:", 1)[-1] if "Scene text:" in user else user
            paras = re.findall(r"Paragraph number (\d+)\.", text)
            return SceneBeats(beats=[
                Beat(type="narration", text=f"P{p}", source_span=f"Paragraph number {p}.")
                for p in paras
            ])
        raise AssertionError(schema)


def _chapter(n: int, pad: int = 0) -> Chapter:
    filler = " lorem" * pad
    text = "\n\n".join(f"Paragraph number {i}.{filler}" for i in range(1, n + 1))
    return Chapter(chapter_id="01", title="T", text=text)


def _voiced_paragraphs(result) -> list[int]:
    return [int(b.text[1:]) for s in result.scenes for b in s.beats]


def test_gap_paragraphs_still_get_beats():
    client = _StubClient(scenes=[(1, 2), (5, 6)])  # misses 3–4 and 7
    result = s2_scenes.segment_chapter(client, _chapter(7), CharacterList(characters=[]))
    assert _voiced_paragraphs(result) == [1, 2, 3, 4, 5, 6, 7]
    assert [(s.start_paragraph, s.end_paragraph) for s in result.scenes] == [(1, 4), (5, 7)]


def test_overlap_paragraphs_are_voiced_once():
    client = _StubClient(scenes=[(1, 5), (3, 7)])
    result = s2_scenes.segment_chapter(client, _chapter(7), CharacterList(characters=[]))
    assert _voiced_paragraphs(result) == [1, 2, 3, 4, 5, 6, 7]


class _LoopingClient(_StubClient):
    """Fails schema validation on any scene text longer than two paragraphs,
    the way the quantized model loops on one long scene."""

    def structured(self, *, system, user, schema, max_tokens=None, **_):
        if schema is SceneBeats and len(re.findall(r"Paragraph number \d+\.", user)) > 2:
            self.calls.append((schema.__name__, len(system) + len(user), max_tokens))
            raise ValueError("LLM returned content that failed schema validation")
        return super().structured(system=system, user=user, schema=schema, max_tokens=max_tokens)


def test_a_scene_the_model_loops_on_is_tagged_in_halves():
    client = _LoopingClient(scenes=[(1, 7)])
    result = s2_scenes.segment_chapter(client, _chapter(7), CharacterList(characters=[]))
    assert _voiced_paragraphs(result) == [1, 2, 3, 4, 5, 6, 7]
    assert [(s.start_paragraph, s.end_paragraph) for s in result.scenes] == [(1, 7)]


def test_a_paragraph_the_model_never_tags_still_fails():
    class Hopeless(_StubClient):
        def structured(self, *, system, user, schema, max_tokens=None, **_):
            if schema is SceneBeats:
                raise ValueError("LLM returned content that failed schema validation")
            return super().structured(system=system, user=user, schema=schema, max_tokens=max_tokens)

    try:
        s2_scenes.segment_chapter(Hopeless(scenes=[(1, 3)]), _chapter(3), CharacterList(characters=[]))
    except ValueError:
        pass
    else:
        raise AssertionError("expected the validation error to propagate")


def test_budgets_fit_a_small_served_context():
    # ~20K chars in one scene: the old fixed beats budget (32K tokens) plus the
    # prompt overflowed a 12K context; budget_for clamps it.
    ctx = 12000
    client = _StubClient(scenes=[(1, 60)], max_model_len=ctx)
    s2_scenes.segment_chapter(client, _chapter(60, pad=55), CharacterList(characters=[]))
    assert {c[0] for c in client.calls} == {"ChapterBoundaries", "SceneBeats"}
    for name, prompt_chars, max_tokens in client.calls:
        est_input = int(prompt_chars / LLMClient._CHARS_PER_TOKEN)
        assert est_input + max_tokens + 1024 <= ctx, (name, est_input, max_tokens)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

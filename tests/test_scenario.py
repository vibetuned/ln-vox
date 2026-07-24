"""Tests for scenario mode (DESIGN.md §17) — the LLM-free pieces.

Covers the markdown chunker, label canonicalization, the verbatim-validation
invariant, staging-run grouping, and the sync timing math. The fixture script
is INVENTED (per the §17 IP rule: no real scenario text in tests).
Runnable directly (`python tests/test_scenario.py`) or under pytest.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.ingest.scenario import (
    cached_structured,
    canonicalize_speakers,
    chunk_script,
    clean_md_line,
    is_group_label,
    is_scene_header,
    split_chunk,
    validate_items,
)
from lnvox.llm.schemas import (
    ChapterDirected,
    DirectedBeat,
    DirectedScene,
    ScriptItem,
    ScriptScene,
)
from lnvox.stages.scenario import group_dialogue_runs
from lnvox.stages.scenario_sync import build_play_sync, build_scene_sync, to_srt
from lnvox.tts.schema import ChapterAudio, RenderedBeat

FIXTURE_MD = """Titre de la pièce

### Personnages

- **Gardien \\+ Le facteur → Paul**
- **Voyageuse → Jeanne**

Séquence 1 – La porte

**Gardien** \\- Qui va là ?

*Un volet claque au premier étage.*

**Voyageuse** : C'est moi, ouvrez \\!

### 2. LE MATIN

**Gardien**  \\- Vous voilà enfin.
"""


# ---- markdown helpers ---------------------------------------------------------


def test_clean_md_line():
    assert clean_md_line("**Gardien** \\- Qui va là ?") == "Gardien - Qui va là ?"
    assert clean_md_line("*Un volet claque.*") == "Un volet claque."
    assert clean_md_line("ouvrez \\!") == "ouvrez !"


def test_scene_header_detection():
    assert is_scene_header("Séquence 1 – La porte")
    assert is_scene_header("### 2. LE MATIN")
    assert is_scene_header("**3\\.**") or is_scene_header("**3**")
    assert not is_scene_header("**Gardien** \\- Qui va là ?")
    assert not is_scene_header("### Personnages")


def test_chunk_script_fixture():
    roster_chunks, scenes = chunk_script(FIXTURE_MD)
    assert len(roster_chunks) == 1
    assert "Paul" in roster_chunks[0]
    assert len(scenes) == 2
    assert scenes[0][0] == "Séquence 1 – La porte"
    assert "Qui va là" in scenes[0][1]
    assert scenes[1][0] == "2. LE MATIN"
    assert "enfin" in scenes[1][1]


def test_chunk_script_headerless_becomes_one_scene():
    roster, scenes = chunk_script("**A** \\- Bonjour.\n\n**B** \\- Salut.")
    assert roster == []
    assert len(scenes) == 1 and scenes[0][0] == ""


def test_split_chunk_respects_blank_lines():
    chunk = "\n\n".join(f"line {i} " + "x" * 50 for i in range(10))
    parts = split_chunk(chunk, max_chars=120)
    assert len(parts) > 1
    assert "\n\n".join(parts).replace("\n\n", " ").count("line") == 10


# ---- speakers -----------------------------------------------------------------


def test_canonicalize_speaker_variants():
    labels = [
        "Rôle 1 (Prénom)",
        "Rôle 1(Prénom)",
        "Rôle 1 (Prénom)",
        "Rôle 1",
        "Autre",
    ]
    canon = canonicalize_speakers(labels)
    targets = {canon[l] for l in labels[:4]}
    assert len(targets) == 1  # all four variants collapse to one character
    assert "Prénom" in targets.pop()  # fullest form wins
    assert canon["Autre"] == "Autre"


def test_group_labels():
    assert is_group_label("Tous")
    assert is_group_label("**Tous ensemble**")
    assert not is_group_label("Gardien")


# ---- verbatim invariant ---------------------------------------------------------


def test_validate_items_demotes_rewritten_dialogue():
    chunk = "**Gardien** \\- Qui va là ?\n\n*Un volet claque.*"
    items = [
        ScriptItem(type="dialogue", speaker="Gardien", text="Qui va là ?"),
        ScriptItem(type="staging", speaker="", text="Un volet claque."),
        ScriptItem(type="dialogue", speaker="Gardien", text="Une phrase inventée."),
    ]
    validated, demoted = validate_items(items, chunk)
    assert demoted == 1
    assert [it.type for it in validated] == ["dialogue", "staging", "staging"]
    assert validated[2].text == "Une phrase inventée."  # kept, never dropped


# ---- LLM cache -----------------------------------------------------------------


class StubLLM:
    """Duck-typed LLMClient stand-in for cached_structured."""

    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.settings = SimpleNamespace(llm=SimpleNamespace(model="stub-model"))

    def budget_for(self, **_kw):
        return 1000

    def structured(self, **_kw):
        self.calls += 1
        return self.result


def test_cached_structured_hits_and_rekeys():
    from lnvox.llm.schemas import SceneStructure

    result = SceneStructure(
        items=[ScriptItem(type="dialogue", speaker="A", text="bonjour")]
    )
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        stub = StubLLM(result)
        kw = dict(system="s", schema=SceneStructure, desired=100, floor=10)

        got, hit = cached_structured(stub, cache_dir=cache, user="chunk A", **kw)
        assert not hit and stub.calls == 1
        assert got.items[0].text == "bonjour"

        got, hit = cached_structured(stub, cache_dir=cache, user="chunk A", **kw)
        assert hit and stub.calls == 1  # served from disk, LLM untouched
        assert got.items[0].text == "bonjour"

        _, hit = cached_structured(stub, cache_dir=cache, user="chunk B", **kw)
        assert not hit and stub.calls == 2  # changed content re-keys

        # Different model re-keys too.
        stub.settings.llm.model = "other-model"
        _, hit = cached_structured(stub, cache_dir=cache, user="chunk A", **kw)
        assert not hit and stub.calls == 3

        # Corrupt cache entry regenerates instead of crashing.
        for f in cache.glob("*.json"):
            f.write_text("{ not json", encoding="utf-8")
        stub.settings.llm.model = "stub-model"
        _, hit = cached_structured(stub, cache_dir=cache, user="chunk A", **kw)
        assert not hit and stub.calls == 4

        # cache_dir=None bypasses entirely.
        _, hit = cached_structured(stub, cache_dir=None, user="chunk A", **kw)
        assert not hit and stub.calls == 5


# ---- staging-run grouping -------------------------------------------------------


def _scene(items) -> ScriptScene:
    return ScriptScene(scene_id="01", title="Test", items=items)


def test_group_dialogue_runs():
    d = lambda t: ScriptItem(type="dialogue", speaker="A", text=t)
    s = ScriptItem(type="staging", speaker="", text="action")
    c = ScriptItem(type="cue", speaker="", text="SON 1")
    scene = _scene([s, d("un"), d("deux"), c, d("trois"), s, s, d("quatre")])
    runs = group_dialogue_runs(scene)
    # Leading staging opens nothing; the cue does not split; the double
    # staging run splits once.
    assert runs == [[1, 2, 4], [7]]


# ---- sync timing ---------------------------------------------------------------


def _beat(line: int, text: str = "x") -> DirectedBeat:
    return DirectedBeat(
        type="dialogue",
        text=text,
        speaker="A",
        direction="cue-fr",
        emotion="joy",
        prompt=f'(d) "{text}"',
        source_paragraph=line,
    )


def _manifest(chapter: ChapterDirected, dur: float = 2.0) -> ChapterAudio:
    beats = []
    for ds in chapter.scenes:
        for i, b in enumerate(ds.beats):
            beats.append(
                RenderedBeat(
                    beat_id=f"{ds.scene_id}_b{i:04d}",
                    scene_id=ds.scene_id,
                    type=b.type,
                    speaker=b.speaker,
                    wav_path="x.wav",
                    duration_seconds=dur,
                    cache_key="k",
                )
            )
    return ChapterAudio(chapter_id=chapter.chapter_id, beats=beats)


def test_scene_sync_timing():
    d = lambda t: ScriptItem(type="dialogue", speaker="A", text=t)
    scene = _scene(
        [
            d("un"),
            d("deux"),
            ScriptItem(type="staging", speaker="", text="action"),
            ScriptItem(type="cue", speaker="", text="SON 1"),
            d("trois"),
        ]
    )
    chapter = ChapterDirected(
        chapter_id="01",
        scenes=[
            DirectedScene(scene_id="01_g00", beats=[_beat(1), _beat(2)]),
            DirectedScene(scene_id="01_g01", beats=[_beat(3)]),
        ],
    )
    sync = build_scene_sync(
        scene, chapter, _manifest(chapter), intra=0.25, staging_pause=1.0
    )
    e = sync.entries
    assert [x.type for x in e] == ["dialogue", "dialogue", "staging", "cue", "dialogue"]
    assert (e[0].start, e[0].end) == (0.0, 2.0)
    assert (e[1].start, e[1].end) == (2.25, 4.25)  # intra pad
    assert (e[2].start, e[2].end) == (4.25, 5.25)  # staging pause window
    assert (e[3].start, e[3].end) == (4.25, 4.25)  # cue: zero-duration marker
    assert (e[4].start, e[4].end) == (5.25, 7.25)  # after the staging pause
    assert sync.end == 7.25
    assert e[0].direction == "cue-fr" and e[0].emotion == "joy"


def test_split_line_regroups_and_leading_staging_is_zero_width():
    scene = _scene(
        [
            ScriptItem(type="staging", speaker="", text="décor"),
            ScriptItem(type="dialogue", speaker="A", text="longue tirade"),
        ]
    )
    chapter = ChapterDirected(
        chapter_id="01",
        scenes=[DirectedScene(scene_id="01_g00", beats=[_beat(1, "a"), _beat(1, "b")])],
    )
    sync = build_scene_sync(
        scene, chapter, _manifest(chapter), intra=0.25, staging_pause=1.0
    )
    stg, dlg = sync.entries
    assert stg.start == stg.end == 0.0  # s5 inserts no leading pad
    assert (dlg.start, dlg.end) == (0.0, 4.25)  # 2.0 + 0.25 + 2.0, one entry


def test_play_sync_offsets_and_mismatch_guard():
    d = ScriptItem(type="dialogue", speaker="A", text="un")
    s1, s2 = _scene([d]), ScriptScene(scene_id="02", title="", items=[d])
    ch1 = ChapterDirected(
        chapter_id="01", scenes=[DirectedScene(scene_id="01_g00", beats=[_beat(1)])]
    )
    ch2 = ChapterDirected(
        chapter_id="02", scenes=[DirectedScene(scene_id="02_g00", beats=[_beat(1)])]
    )
    play = build_play_sync(
        ScenarioScriptStub(scenes=[s1, s2]),
        {"01": ch1, "02": ch2},
        {"01": _manifest(ch1), "02": _manifest(ch2)},
        intra=0.25,
        staging_pause=1.0,
        inter_scene=2.0,
    )
    assert play.scenes[0].end == 2.0
    assert play.scenes[1].start == 4.0  # 2.0 + inter_scene
    assert play.total_duration_seconds == 6.0

    # A session-mode (VibeVoice) manifest has fewer entries than beats.
    bad = _manifest(ch1)
    bad.beats = bad.beats[:0]
    try:
        build_scene_sync(s1, ch1, bad)
        raise AssertionError("expected ValueError on beat/manifest mismatch")
    except ValueError as exc:
        assert "per-beat" in str(exc)


class ScenarioScriptStub:
    """Duck-typed stand-in for ScenarioScript in build_play_sync."""

    def __init__(self, scenes):
        self.scenario_id = "test"
        self.title = "Test"
        self.scenes = scenes


def test_srt_export():
    d = ScriptItem(type="dialogue", speaker="A", text="un")
    scene = _scene([d, ScriptItem(type="cue", speaker="", text="SON 1")])
    ch = ChapterDirected(
        chapter_id="01", scenes=[DirectedScene(scene_id="01_g00", beats=[_beat(1)])]
    )
    play = build_play_sync(
        ScenarioScriptStub(scenes=[scene]), {"01": ch}, {"01": _manifest(ch)}
    )
    srt = to_srt(play)
    assert "1\n00:00:00,000 --> 00:00:02,000\nA: un" in srt
    assert "((SON 1))" in srt
    assert "00:00:02,000 --> 00:00:03,500" in srt  # zero-width cue gets display time


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

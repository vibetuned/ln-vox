"""Casting-aware caches in s3 (narration) and scenario direction.

Guards: a recast after direction must reach the cached chapters (their
prompts are re-voiced, cues kept, no new LLM call); an unchanged casting must
reuse everything (no LLM call, files untouched); `--regen-profiles` forces
every profile; files written before the records existed count as current; and
a dead LLM server fails the step instead of caching cue-less chapters.

The LLM is stubbed; all names and lines are invented.
Runnable directly (`python tests/test_s3_casting_cache.py`) or under pytest.
"""

import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import openai

from lnvox.llm.client import LLMClient, _jinja
from lnvox.llm.schemas import (
    Beat,
    ChapterDirected,
    ChapterScenes,
    Character,
    CharacterList,
    ScenarioDirections,
    ScenarioScript,
    Scene,
    SceneDirections,
    ScriptItem,
    ScriptScene,
    VoiceProfileList,
)
from lnvox.stages import s3_director
from lnvox.stages import scenario as scenario_stage
from lnvox.voices.schema import BookCasting, CharacterCasting, Voicebank, VoiceClip, VoiceTarget

NARRATOR_DESC = "adult, female, warm clear voice"


class _StubClient:
    """render / budget_for / structured; profile descriptors are versioned so
    a regeneration is visible, and every structured call is logged."""

    _CHARS_PER_TOKEN = LLMClient._CHARS_PER_TOKEN
    budget_for = LLMClient.budget_for

    def __init__(self):
        self.settings = SimpleNamespace(llm=SimpleNamespace(max_model_len=65536))
        self.calls: list[tuple[str, list[str]]] = []
        self.version = 0
        self.fail_directions: Exception | None = None

    def render(self, template: str, **kw) -> str:
        return _jinja.get_template(template).render(**kw)

    def structured(self, *, system, user, schema, max_tokens=None, **_):
        if schema is VoiceProfileList:
            self.version += 1
            data = json.loads(user.split("(JSON):\n", 1)[1].split("\n---", 1)[0])
            names = [e["name"] for e in data]
            self.calls.append(("profiles", names))
            return VoiceProfileList(profiles=[
                {"name": n, "voice_descriptor": f"{n.lower()} voice v{self.version}"}
                for n in names
            ])
        if schema in (SceneDirections, ScenarioDirections):
            self.calls.append(("directions", []))
            if self.fail_directions is not None:
                raise self.fail_directions
            n = len(re.findall(r"^\d+\. ", user, re.M))
            if schema is SceneDirections:
                return SceneDirections(directions=[
                    {"line": i, "cue": f"cue {i}"} for i in range(1, n + 1)
                ])
            return ScenarioDirections(directions=[
                {"line": i, "emotion": "joy", "cue": f"indice {i}"} for i in range(1, n + 1)
            ])
        raise AssertionError(schema)

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def _clip(cid: str, gender: str, age: str, accent: str) -> VoiceClip:
    return VoiceClip(id=cid, source="manual", clip_path=f"clips/{cid}.wav",
                     duration_seconds=12.0, gender=gender, age_band=age, accent=accent)


VOICEBANK = Voicebank(clips=[
    _clip("f1", "female", "adult", "us"),
    _clip("m1", "male", "adult", "england"),
    _clip("m2", "male", "elder", "us"),
])

CAST = CharacterList(characters=[
    Character(name="Ilse", gender="female", approx_age="adult", description="A pilot."),
    Character(name="Oren", gender="male", approx_age="adult", description="A clerk."),
])


def _casting(oren_clip: str = "m1") -> BookCasting:
    def cc(name, clip, desc=""):
        return CharacterCasting(
            character_name=name, target=VoiceTarget(gender="male", age_band="adult"),
            assigned_clip_id=clip, voice_descriptor=desc,
        )
    return BookCasting(book_id="b", castings=[
        cc("Ilse", "f1"), cc("Oren", oren_clip), cc("Narrator", "f1", NARRATOR_DESC),
    ])


def _scenes() -> list[ChapterScenes]:
    def beat(sp, text):
        return Beat(type="dialogue" if sp else "narration", text=text,
                    speaker=sp, source_span=text)
    return [
        ChapterScenes(chapter_id="01", scenes=[Scene(scene_id="01_s1", beats=[
            beat(None, "The hangar was cold."),
            beat("Ilse", "Fuel her up."),
            beat("Oren", "Sign here first."),
        ])]),
        ChapterScenes(chapter_id="02", scenes=[Scene(scene_id="02_s1", beats=[
            beat(None, "Night fell."),
            beat("Ilse", "We fly at dawn."),
        ])]),
    ]


def _s3(client, out: Path, casting: BookCasting, **kw):
    return s3_director.run(_scenes(), CAST, client, out, casting=casting,
                           voicebank=VOICEBANK, **kw)


def _read(out: Path, cid: str) -> ChapterDirected:
    return ChapterDirected.model_validate_json(
        (out / "03_directed" / f"{cid}.json").read_text(encoding="utf-8"))


def _beats(cd: ChapterDirected, speaker: str):
    return [b for s in cd.scenes for b in s.beats if b.speaker == speaker]


# ---- s3 -----------------------------------------------------------------------


def test_first_run_records_what_it_was_written_for():
    with tempfile.TemporaryDirectory() as d:
        out, client = Path(d), _StubClient()
        _s3(client, out, _casting())
        # Narrator's descriptor is locked in the casting → not sent to the LLM.
        assert client.calls[0] == ("profiles", ["Ilse", "Oren"])
        assert client.kinds().count("directions") == 2
        profiles = VoiceProfileList.model_validate_json(
            (out / "03_voice_profiles.json").read_text(encoding="utf-8"))
        rec = {p.name: (p.written_for_clip, p.written_for_descriptor) for p in profiles.profiles}
        assert rec == {"Ilse": ("f1", ""), "Oren": ("m1", ""), "Narrator": ("f1", NARRATOR_DESC)}
        assert _read(out, "01").voice_descriptors == {
            "Ilse": "ilse voice v1", "Narrator": NARRATOR_DESC, "Oren": "oren voice v1",
        }


def test_unchanged_casting_reuses_everything():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _s3(_StubClient(), out, _casting())
        before = {p.name: p.read_bytes() for p in out.rglob("*.json")}
        client = _StubClient()
        _s3(client, out, _casting())
        assert client.calls == []
        assert {p.name: p.read_bytes() for p in out.rglob("*.json")} == before


def test_recast_regenerates_only_that_profile_and_revoices_its_chapters():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _s3(_StubClient(), out, _casting("m1"))
        ch02_before = (out / "03_directed" / "02.json").read_bytes()
        client = _StubClient()
        client.version = 1
        _s3(client, out, _casting("m2"))
        # Only Oren is re-profiled, and no direction call is re-paid.
        assert client.calls == [("profiles", ["Oren"])]
        ch01 = _read(out, "01")
        oren = _beats(ch01, "Oren")[0]
        assert oren.direction == "oren voice v2, cue 2"
        assert oren.prompt == s3_director.format_prompt("oren voice v2, cue 2", oren.text)
        assert _beats(ch01, "Ilse")[0].direction == "ilse voice v1, cue 1"
        assert ch01.voice_descriptors["Oren"] == "oren voice v2"
        # Oren isn't in chapter 02 → untouched.
        assert (out / "03_directed" / "02.json").read_bytes() == ch02_before


def test_regen_profiles_forces_every_profile():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _s3(_StubClient(), out, _casting())
        client = _StubClient()
        client.version = 1
        _s3(client, out, _casting(), regen_profiles=True)
        assert client.calls == [("profiles", ["Ilse", "Oren"])]
        assert _beats(_read(out, "02"), "Ilse")[0].direction == "ilse voice v2, cue 1"


def _write_legacy(out: Path, oren_profile: dict) -> None:
    """Artifacts as the pre-record code wrote them (no written_for / no
    voice_descriptors)."""
    profiles = {"profiles": [
        {"name": "Ilse", "voice_descriptor": "ilse old", "accent": "us",
         "gender": "female", "age_band": "adult"},
        oren_profile,
        {"name": "Narrator", "voice_descriptor": NARRATOR_DESC, "accent": "us",
         "gender": "female", "age_band": "adult"},
    ]}
    (out / "03_voice_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    (out / "03_directed").mkdir()
    for cs in _scenes():
        cd = s3_director.direct_chapter(
            _StubClient(), cs, VoiceProfileList.model_validate(profiles))
        data = json.loads(cd.model_dump_json())
        del data["voice_descriptors"], data["normalized"]
        (out / "03_directed" / f"{cs.chapter_id}.json").write_text(
            json.dumps(data), encoding="utf-8")


OREN_M1 = {"name": "Oren", "voice_descriptor": "oren old", "accent": "england",
           "gender": "male", "age_band": "adult"}


def test_legacy_artifacts_are_treated_as_current_and_stamped():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _write_legacy(out, OREN_M1)
        client = _StubClient()
        _s3(client, out, _casting("m1"))
        assert client.calls == []  # nothing re-paid
        assert _read(out, "01").voice_descriptors == {
            "Ilse": "ilse old", "Narrator": NARRATOR_DESC, "Oren": "oren old",
        }
        prof = VoiceProfileList.model_validate_json(
            (out / "03_voice_profiles.json").read_text(encoding="utf-8"))
        assert all(p.written_for_clip is not None for p in prof.profiles)


def test_legacy_profile_for_another_clip_is_regenerated_and_revoiced():
    # Oren's legacy profile was snapped to m1 (adult/england); he's cast to m2
    # (elder/us) now, so that profile provably describes the old voice.
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _write_legacy(out, OREN_M1)
        client = _StubClient()
        _s3(client, out, _casting("m2"))
        assert client.calls == [("profiles", ["Oren"])]
        oren = _beats(_read(out, "01"), "Oren")[0]
        assert oren.direction == "oren voice v1, cue 2"
        assert _beats(_read(out, "01"), "Ilse")[0].direction == "ilse old, cue 1"


def test_dead_server_fails_the_step_but_a_bad_answer_does_not():
    import httpx

    req = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    with tempfile.TemporaryDirectory() as d:
        out, client = Path(d), _StubClient()
        client.fail_directions = openai.APIConnectionError(request=req)
        try:
            _s3(client, out, _casting())
        except openai.APIConnectionError:
            pass
        else:
            raise AssertionError("transport error was swallowed")
        assert not (out / "03_directed" / "01.json").exists()
    with tempfile.TemporaryDirectory() as d:
        out, client = Path(d), _StubClient()
        client.fail_directions = ValueError("schema validation failed")
        _s3(client, out, _casting())
        oren = _beats(_read(out, "01"), "Oren")[0]
        assert oren.direction == "oren voice v1"  # cue-less, descriptor only


# ---- scenario -------------------------------------------------------------------


def _script() -> ScenarioScript:
    return ScenarioScript(scenario_id="p", scenes=[
        ScriptScene(scene_id="01", items=[
            ScriptItem(type="dialogue", speaker="Ilse", text="Qui va là ?"),
            ScriptItem(type="staging", text="Un volet claque."),
            ScriptItem(type="dialogue", speaker="Oren", text="C'est moi."),
        ]),
        ScriptScene(scene_id="02", items=[
            ScriptItem(type="dialogue", speaker="Ilse", text="Enfin."),
        ]),
    ])


def _scenario(client, out, casting, **kw):
    return scenario_stage.run(_script(), CAST, client, out, casting=casting,
                              voicebank=VOICEBANK, **kw)


def test_scenario_recast_revoices_without_llm():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _scenario(_StubClient(), out, _casting("m1"))
        ch02_before = (out / "03_directed" / "02.json").read_bytes()
        again = _StubClient()
        _scenario(again, out, _casting("m1"))
        assert again.calls == []
        client = _StubClient()
        client.version = 1
        _scenario(client, out, _casting("m2"))
        assert client.calls == [("profiles", ["Oren"])]
        oren = _beats(_read(out, "01"), "Oren")[0]
        assert oren.prompt == s3_director.format_prompt("oren voice v2, joy", oren.text)
        assert oren.direction == "indice 2" and oren.emotion == "joy"
        assert (out / "03_directed" / "02.json").read_bytes() == ch02_before


def test_scenario_regen_profiles_and_transport_error():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        _scenario(_StubClient(), out, _casting())
        client = _StubClient()
        _scenario(client, out, _casting(), regen_profiles=True)
        assert client.calls == [("profiles", ["Ilse", "Oren"])]
    with tempfile.TemporaryDirectory() as d:
        client = _StubClient()
        client.fail_directions = openai.APITimeoutError(
            request=__import__("httpx").Request("POST", "http://x"))
        try:
            _scenario(client, Path(d), _casting())
        except openai.APITimeoutError:
            pass
        else:
            raise AssertionError("transport error was swallowed")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

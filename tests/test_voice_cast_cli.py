"""`lnvox voice cast` / `voice seed-cv` at the CLI level (LLM stubbed).

Covers what only the command can get wrong: which layers of the volume's own
04_voice_assignments.json reach the matcher (locks, --fresh), and the
up-front --narrator-clip check. All names below are invented.
"""

import inspect
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import lnvox.cli as cli
from lnvox.llm.schemas import Character, CharacterList
from lnvox.voices.common_voice import seed_from_common_voice
from lnvox.voices.schema import (
    BookCasting,
    CharacterCasting,
    MatchResult,
    VoiceClip,
    Voicebank,
    VoiceTarget,
    _RankedChoice,
)


class _StubLLM:
    """Stands in for LLMClient: male/teen target, ranks `pick` first."""

    pick = "m3"
    constructed = 0

    def __init__(self):
        type(self).constructed += 1
        self.settings = SimpleNamespace(llm=SimpleNamespace(endpoint="stub", model="stub"))

    def render(self, template, **kwargs):
        return template

    def structured(self, *, system, user, schema, max_tokens):
        if schema is VoiceTarget:
            return VoiceTarget(gender="male", age_band="teen")
        return MatchResult(
            ranked_choices=[_RankedChoice(clip_id=self.pick, score=0.9, reason="stub")]
        )


class _NoLLM:
    def __init__(self):
        raise AssertionError("LLMClient must not be constructed")


class _SilentLLM(_StubLLM):
    """Constructible (the CLI always builds a client) but must never be asked."""

    def structured(self, **kwargs):
        raise AssertionError("no LLM call expected: everything is reused")


def _clip(cid, gender="male", age="teen"):
    return VoiceClip(id=cid, source="manual", clip_path=f"clips/{cid}.wav",
                     duration_seconds=12.0, gender=gender, age_band=age, accent="us")


def _entry(name, cid, gender="male", age="teen", **extra):
    return CharacterCasting(
        character_name=name,
        target=VoiceTarget(gender=gender, age_band=age),
        candidates_considered=1,
        ranked=[_RankedChoice(clip_id=cid, score=1.0, reason="test")],
        assigned_clip_id=cid,
        **extra,
    )


def _char(name):
    return Character(name=name, aliases=[], gender="male", approx_age="teen",
                     description="test character", evidence=[])


def _setup(root: Path, own: list[CharacterCasting] | None, names=("Aldric", "Brann")):
    vb = root / "vb"
    vb.mkdir()
    (vb / "manifest.json").write_text(Voicebank(clips=[
        _clip("m1"), _clip("m2"), _clip("m3"),
        _clip("f_nar", "female", "adult"), _clip("f_alt", "female", "adult"),
    ]).model_dump_json())
    art = root / "artifacts"
    v1 = art / "s" / "volume-01"
    v2 = art / "s" / "volume-02"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    (v1 / "01_characters.json").write_text(
        CharacterList(characters=[_char("Aldric")]).model_dump_json())
    (v1 / "04_voice_assignments.json").write_text(BookCasting(
        book_id="s/volume-01",
        castings=[_entry("Aldric", "m1"),
                  _entry("Narrator", "f_nar", gender="female", age="adult")],
    ).model_dump_json())
    (v2 / "01_characters.json").write_text(
        CharacterList(characters=[_char(n) for n in names]).model_dump_json())
    if own is not None:
        (v2 / "04_voice_assignments.json").write_text(
            BookCasting(book_id="s/volume-02", castings=own).model_dump_json())
    env = {"LNVOX_VOICEBANK": str(vb), "LNVOX_ARTIFACTS_DIR": str(art)}
    return v2, env


def _invoke(args, env, llm):
    saved = cli.LLMClient
    cli.LLMClient = llm
    try:
        return CliRunner().invoke(cli.app, args, env=env)
    finally:
        cli.LLMClient = saved


def _result(v2: Path) -> dict[str, dict]:
    data = json.loads((v2 / "04_voice_assignments.json").read_text())
    return {c["character_name"]: c for c in data["castings"]}


def test_unknown_narrator_clip_exits_2_before_casting_anyone():
    with tempfile.TemporaryDirectory() as tmp:
        v2, env = _setup(Path(tmp), own=None)
        res = _invoke(["voice", "cast", "s/volume-02", "--narrator-clip", "f_nra"], env, _NoLLM)
        assert res.exit_code == 2, res.output
        assert "f_nra" in res.output and "f_nar" in res.output   # did-you-mean
        assert not (v2 / "04_voice_assignments.json").exists()


def test_recast_keeps_locked_entries_over_prior_volume_and_narrator_clip():
    """The --stop-after-audit flow: hand-cast + lock, then resume the same
    launcher command (which re-runs `voice cast … --narrator-clip`)."""
    with tempfile.TemporaryDirectory() as tmp:
        v2, env = _setup(Path(tmp), own=[
            _entry("Aldric", "m2", locked=True, audit_note="deeper voice"),
            _entry("Brann", "m3"),
            _entry("Narrator", "f_alt", gender="female", age="adult", locked=True),
        ])
        res = _invoke(["voice", "cast", "s/volume-02", "--narrator-clip", "f_nar"],
                      env, _SilentLLM)
        assert res.exit_code == 0, res.output
        by = _result(v2)
        assert by["Aldric"]["assigned_clip_id"] == "m2"     # not volume-01's m1
        assert by["Aldric"]["locked"] and by["Aldric"]["audit_note"] == "deeper voice"
        assert by["Brann"]["assigned_clip_id"] == "m3"      # unlocked, first appearance
        assert by["Narrator"]["assigned_clip_id"] == "f_alt" and by["Narrator"]["locked"]
        assert "is ignored" in res.output                   # the flag lost to the lock


def test_unlocked_own_entry_of_recurring_character_follows_prior_volume():
    with tempfile.TemporaryDirectory() as tmp:
        v2, env = _setup(Path(tmp), own=[_entry("Aldric", "m2"), _entry("Brann", "m3")])
        res = _invoke(["voice", "cast", "s/volume-02"], env, _SilentLLM)
        assert res.exit_code == 0, res.output
        by = _result(v2)
        assert by["Aldric"]["assigned_clip_id"] == "m1"     # continuity wins
        assert by["Brann"]["assigned_clip_id"] == "m3"


def test_fresh_recasts_unlocked_entries_but_keeps_locked_ones():
    with tempfile.TemporaryDirectory() as tmp:
        v2, env = _setup(Path(tmp), own=[
            _entry("Brann", "m2", locked=True),
            _entry("Cyra", "m3"),
        ], names=("Brann", "Cyra"))
        _StubLLM.pick = "m1"
        res = _invoke(["voice", "cast", "s/volume-02", "--fresh"], env, _StubLLM)
        assert res.exit_code == 0, res.output
        by = _result(v2)
        assert by["Brann"]["assigned_clip_id"] == "m2" and by["Brann"]["locked"]
        assert by["Cyra"]["assigned_clip_id"] == "m1"       # re-rolled by the LLM


def test_seed_cv_library_default_matches_cli():
    lib = inspect.signature(seed_from_common_voice).parameters["max_speakers"].default
    opt = inspect.signature(cli.voice_seed_cv).parameters["max_speakers"].default
    assert lib == opt.default == 150


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

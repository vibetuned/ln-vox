"""The Stage-5 layout contract: `06_final/<title>.timings.json` (MixLayout).

Stage 6 and scenario-sync rebuild beat times from the manifests and never
probe audio, so they have to replay s5's layout exactly. They used to take it
from their own flags, kept in step with the s5 run by hand, and three s5
options had no s6 counterpart at all:

  - `--lead-in`: s6 had no lead-in term, so every beat drifted by one lead-in
    per beat;
  - `--chapters`: s6 laid out every chapter in 05_audio/, so a partial mix
    got timings for a book it didn't contain;
  - `--cover` / `--images-dir`: s6 re-resolved pictures without them, so the
    manifest `images` slots no longer matched the embedded pictures (ln-reader
    pairs them by position).

s5 now records all of it and s6 / scenario-sync / `lnvox sample` read it back.
The s5 tests mix tiny generated WAVs with the real ffmpeg pipeline, so the
replay is checked against what ffmpeg actually produced.

All prose below is invented — no real book content.

Runnable directly: `.venv/bin/python tests/test_mix_layout.py`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.llm.schemas import (  # noqa: E402
    ChapterDirected,
    DirectedBeat,
    DirectedScene,
    ScenarioScript,
    ScriptItem,
    ScriptScene,
)
from lnvox.stages import s5_mix, s6_sync, scenario_sync  # noqa: E402
from lnvox.stages.sample import pick_source_audio  # noqa: E402
from lnvox.tts.schema import ChapterAudio, RenderedBeat  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@contextmanager
def _tmp():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---- fixture: a three-chapter book, rendered and ingested ---------------------

# chapter → [(scene suffix, text, seconds)] in playback order. Two scenes per
# chapter so both the intra and the inter-scene pad are exercised.
BOOK = {
    "01": [("s1", "A lantern swung over the gate.", 1.0),
           ("s1", "Nobody answered the knock.", 0.5),
           ("s2", "The hinge gave a thin, cold whine.", 0.75)],
    "02": [("s1", "Dust rose behind the cart.", 0.5),
           ("s1", "The road bent north at the mill.", 0.75),
           ("s2", "Crows lifted from the stubble field.", 0.5)],
    "03": [("s1", "Rain drummed on the chapel roof.", 0.5),
           ("s1", "Somewhere a bell began to ring.", 1.25),
           ("s2", "The keeper counted the strokes aloud.", 0.5)],
}

OPF_ITEMS = "".join(
    f'<item id="p{c}" href="c{c}.xhtml" media-type="application/xhtml+xml"/>' for c in BOOK
)
OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
    'version="3.0" unique-identifier="i"><metadata '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Invented</dc:title>'
    f"</metadata><manifest>{OPF_ITEMS}</manifest><spine>"
    + "".join(f'<itemref idref="p{c}"/>' for c in BOOK)
    + "</spine></package>"
)
CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
# Where each chapter page shows a picture: before its second paragraph.
PAGE_IMAGE = {"01": "a.jpg", "03": "b.jpg"}


def _page(cid: str) -> str:
    paras = [f"<p>{text}</p>" for _, text, _ in BOOK[cid]]
    if cid in PAGE_IMAGE:
        paras.insert(1, f'<img src="../images/{PAGE_IMAGE[cid]}"/>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        f"<body>{''.join(paras)}</body></html>"
    )


def _wav(path: Path, seconds: float) -> None:
    # 48 kHz stereo s16, the format every s4 backend writes: the concat
    # demuxer needs beats and silence pads to share one stream format.
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"sine=frequency=330:sample_rate=48000:duration={seconds}",
        "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ], check=True)


def _jpg(path: Path, color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={color}:s=16x16", "-frames:v", "1", str(path),
    ], check=True)
    return path


def _build(root: Path, *, wavs: bool = True):
    """(book_dir, novel_dir, epub) for BOOK, with every chapter rendered."""
    book = root / "artifacts" / "demo"
    novel = root / "novels" / "demo"
    (book / "03_directed").mkdir(parents=True)
    for cid, beats in BOOK.items():
        by_scene: dict[str, list] = {}
        rendered = []
        d = book / "05_audio" / cid
        d.mkdir(parents=True)
        for suffix, text, secs in beats:
            scene_id = f"{cid}_{suffix}"
            i = len(by_scene.setdefault(scene_id, []))
            by_scene[scene_id].append(DirectedBeat(
                type="narration", text=text, source_span=text,
                speaker="Narrator", direction="d", prompt="p",
            ))
            beat_id = f"{scene_id}_b{i:04d}"
            if wavs:
                _wav(d / f"{beat_id}.wav", secs)
            rendered.append(RenderedBeat(
                beat_id=beat_id, scene_id=scene_id, type="narration",
                speaker="Narrator", wav_path=f"05_audio/{cid}/{beat_id}.wav",
                duration_seconds=secs, cache_key=f"k{beat_id}",
            ))
        (book / "03_directed" / f"{cid}.json").write_text(ChapterDirected(
            chapter_id=cid,
            scenes=[DirectedScene(scene_id=s, beats=b) for s, b in by_scene.items()],
        ).model_dump_json())
        (d / "manifest.json").write_text(
            ChapterAudio(chapter_id=cid, beats=rendered).model_dump_json()
        )

    novel.mkdir(parents=True)
    if wavs:
        _jpg(novel / "images" / "a.jpg", "red")
        _jpg(novel / "images" / "b.jpg", "blue")
    (novel / ".epub_meta.json").write_text(json.dumps({
        "title": "Invented", "authors": [], "visual_elements": [],
        "images": ["images/a.jpg", "images/b.jpg"],
        "chapters": [
            {"file": f"{cid}-x.txt", "title": f"Ch {cid}", "source_parts": [f"c{cid}"]}
            for cid in BOOK
        ],
    }))

    epub = root / "demo.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("content.opf", OPF)
        for cid in BOOK:
            z.writestr(f"c{cid}.xhtml", _page(cid))
    return book, novel, epub


def _mix(book: Path, root: Path, chapter_ids, **kw) -> Path:
    chapters = [
        ChapterAudio.model_validate_json(
            (book / "05_audio" / c / "manifest.json").read_text()
        )
        for c in chapter_ids
    ]
    return s5_mix.mix(
        chapters_audio=chapters,
        chapter_titles={c: f"Ch {c}" for c in chapter_ids},
        book_title="demo/partial",
        beats_root=book,
        output_dir=book / "06_final",
        audio_dir="05_audio",
        progress=lambda _m: None,
        **kw,
    )


def _sync(book, novel, epub, **kw):
    lines: list[str] = []
    m = s6_sync.run("demo", book, epub, novel, book / "07_sync",
                    progress=lines.append, **kw)
    return m, lines


def _probe(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout)


def _write_sidecar(final_dir: Path, stem: str, **fields) -> Path:
    final_dir.mkdir(parents=True, exist_ok=True)
    p = final_dir / f"{stem}.timings.json"
    p.write_text(json.dumps({"book_title": stem, **fields}))
    return p


def _chapters(ids_and_bounds):
    return [
        {"chapter_id": c, "title": c, "start_seconds": s, "end_seconds": e,
         "duration_seconds": e - s}
        for c, s, e in ids_and_bounds
    ]


# ---- s5: what the sidecar records ---------------------------------------------


def test_sidecar_records_everything_s6_needs_and_work_dir_is_removed():
    if not HAS_FFMPEG:
        print("  skip: needs ffmpeg")
        return
    with _tmp() as root:
        book, novel, epub = _build(root)
        cover = _jpg(root / "override" / "cover.jpg", "green")
        b = novel / "images" / "b.jpg"
        m4b = _mix(book, root, ["01", "03"], lead_in_silence=0.35,
                   inter_chapter_silence=0.75, cover_image=cover, images=[b, cover])

        side = book / "06_final" / "demo_partial.timings.json"
        data = json.loads(side.read_text())
        assert data["m4b"] == m4b.name == "demo_partial.m4b"
        assert data["audio_dir"] == "05_audio"
        assert data["lead_in_silence"] == 0.35
        assert data["inter_chapter_silence"] == 0.75
        assert [c["chapter_id"] for c in data["chapters"]] == ["01", "03"]
        # Embed order: cover first, deduplicated — what the m4b carries.
        assert [Path(p).name for p in data["embedded_images"]] == ["cover.jpg", "b.jpg"]

        layout = s5_mix.read_mix_layout(side)
        assert layout.m4b == m4b and layout.lead_in_silence == 0.35
        assert layout.chapter_ids == ["01", "03"]
        assert [p.name for p in layout.embedded_images] == ["cover.jpg", "b.jpg"]

        # _work/ held several full-length WAVs and nothing reuses it.
        assert not (book / "06_final" / "_work").exists()


def test_work_dir_is_kept_on_request_and_on_failure():
    if not HAS_FFMPEG:
        print("  skip: needs ffmpeg")
        return
    with _tmp() as root:
        book, _novel, _epub = _build(root)
        _mix(book, root, ["01"], keep_work=True)
        assert (book / "06_final" / "_work" / "book_normalized.wav").exists()

        shutil.rmtree(book / "06_final")
        (book / "05_audio" / "02" / "02_s1_b0001.wav").unlink()
        try:
            _mix(book, root, ["02"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("a missing beat WAV must fail the mix")
        assert (book / "06_final" / "_work").is_dir(), "kept for debugging"
        assert not list((book / "06_final").glob("*.timings.json"))


# ---- s6: follows the sidecar --------------------------------------------------


def test_s6_replays_lead_in_subset_and_picture_overrides_from_the_sidecar():
    """The three s5 options s6 used to ignore, all at once, checked against
    what ffmpeg actually produced."""
    if not HAS_FFMPEG:
        print("  skip: needs ffmpeg")
        return
    with _tmp() as root:
        book, novel, epub = _build(root)
        cover = _jpg(root / "override" / "cover.jpg", "green")
        m4b = _mix(book, root, ["01", "03"], lead_in_silence=0.35,
                   inter_chapter_silence=0.75, cover_image=cover,
                   images=[novel / "images" / "b.jpg"])
        side = json.loads((book / "06_final" / "demo_partial.timings.json").read_text())

        manifest, _lines = _sync(book, novel, epub)   # no flags at all
        beats = {b["beat_id"]: b for b in manifest["beats"]}

        # Chapter 02 is not in the m4b, so it is not on the timeline.
        assert not any(b["chapter_id"] == "02" for b in manifest["beats"])
        assert manifest["total_beats"] == 6 and manifest["matched"] == 6

        # Lead-in before EVERY beat; a beat starts where its lead-in starts.
        assert beats["01_s1_b0000"]["start_seconds"] == 0.0
        assert beats["01_s1_b0000"]["end_seconds"] == 1.35          # 0.35 + 1.0
        assert beats["01_s1_b0001"]["start_seconds"] == 1.6          # + 0.25 intra
        assert beats["01_s2_b0000"]["start_seconds"] == 3.45         # 2.45 + 1.0 scene
        # Chapter 03 starts where the m4b's chapter marker says …
        ch03 = next(c for c in side["chapters"] if c["chapter_id"] == "03")
        assert beats["03_s1_b0000"]["start_seconds"] == ch03["start_seconds"]
        # … and the replay ends where the real audio ends.
        last = beats["03_s2_b0000"]["end_seconds"]
        assert abs(last - _probe(m4b)) < 0.05, (last, _probe(m4b))

        # Picture slots follow the s5 embed order (override cover, then b),
        # not a re-resolution of novels/demo/images (a, b).
        assert [Path(i["src"]).name for i in manifest["images"]] == ["cover.jpg", "b.jpg"]
        assert manifest["images"][0]["trigger_seconds"] is None
        assert manifest["images"][1]["trigger_seconds"] == beats["03_s1_b0001"]["start_seconds"]

        assert manifest["m4b"] == "demo_partial.m4b"
        assert manifest["silences"] == {
            "intra_seconds": 0.25, "inter_scene_seconds": 1.0,
            "inter_chapter_seconds": 0.75, "lead_in_seconds": 0.35,
        }


def test_explicit_flags_override_the_sidecar_with_a_warning():
    with _tmp() as root:
        book, novel, epub = _build(root, wavs=False)
        _write_sidecar(
            book / "06_final", "demo", m4b="demo.m4b", audio_dir="05_audio",
            intra_silence=0.25, inter_scene_silence=1.0, inter_chapter_silence=2.0,
            lead_in_silence=0.35, embedded_images=[],
            chapters=_chapters([("01", 0.0, 4.5), ("02", 6.5, 10.1), ("03", 12.1, 16.6)]),
        )
        manifest, lines = _sync(book, novel, epub, lead_in_silence=0.0)
        assert manifest["silences"]["lead_in_seconds"] == 0.0
        b = {x["beat_id"]: x for x in manifest["beats"]}
        assert b["01_s1_b0000"]["end_seconds"] == 1.0                # no lead-in
        assert any("[WARN] lead_in" in ln for ln in lines), lines


def test_an_old_sidecar_still_supplies_pads_and_chapters():
    """Sidecars from before `m4b` / `audio_dir` / `embedded_images` (and, older
    still, `lead_in_silence`) must keep working."""
    with _tmp() as root:
        book, novel, epub = _build(root, wavs=False)
        (novel / "images").mkdir()
        for name in ("a.jpg", "b.jpg"):     # resolve_book_images only checks existence
            (novel / "images" / name).write_bytes(b"jpg")
        _write_sidecar(
            book / "06_final", "Old Title",
            intra_silence=0.5, inter_scene_silence=1.0, inter_chapter_silence=3.0,
            chapters=_chapters([("01", 0.0, 3.75), ("03", 6.75, 10.5)]),
        )
        manifest, _ = _sync(book, novel, epub)
        b = {x["beat_id"]: x for x in manifest["beats"]}
        assert b["01_s1_b0001"]["start_seconds"] == 1.5              # 1.0 + 0.5 intra
        assert b["03_s1_b0000"]["start_seconds"] == 6.75             # anchored
        assert not any(x["chapter_id"] == "02" for x in manifest["beats"])
        assert manifest["m4b"] == "Old Title.m4b"
        assert manifest["silences"]["lead_in_seconds"] == 0.0
        # No recorded picture list → re-resolved from the book, as before.
        assert [Path(i["src"]).name for i in manifest["images"]] == ["a.jpg", "b.jpg"]


def test_without_a_sidecar_s6_behaves_as_before():
    with _tmp() as root:
        book, novel, epub = _build(root, wavs=False)
        manifest, lines = _sync(book, novel, epub)
        assert manifest["m4b"] is None
        assert manifest["silences"] == {
            "intra_seconds": 0.25, "inter_scene_seconds": 1.0,
            "inter_chapter_seconds": 2.0, "lead_in_seconds": 0.0,
        }
        b = {x["beat_id"]: x for x in manifest["beats"]}
        # Every chapter, s5 defaults: 01 = 1.0 + .25 + .5 + 1.0 + .75 = 3.5
        assert b["02_s1_b0000"]["start_seconds"] == 5.5              # 3.5 + 2.0
        assert any("No 06_final" in ln for ln in lines)


def test_s6_refuses_a_vibevoice_mix_and_an_unknown_title():
    with _tmp() as root:
        book, novel, epub = _build(root, wavs=False)
        _write_sidecar(book / "06_final", "vv", audio_dir="05_audio_v2",
                       chapters=_chapters([("01", 0.0, 9.0)]))
        for kw, exc in (({}, ValueError), ({"mix_title": "nope"}, FileNotFoundError)):
            try:
                _sync(book, novel, epub, **kw)
            except exc as e:
                assert ("05_audio_v2" in str(e)) or ("nope" in str(e) and "vv" in str(e)), e
            else:
                raise AssertionError(f"expected {exc.__name__} for {kw}")


def test_a_chapter_rerendered_after_the_mix_is_flagged_and_contained():
    """Manifest durations that disagree with the mixed chapter mean 05_audio
    changed after s5. Later chapters stay on the m4b's chapter starts."""
    with _tmp() as root:
        book, novel, epub = _build(root, wavs=False)
        _write_sidecar(
            book / "06_final", "demo", m4b="demo.m4b", audio_dir="05_audio",
            intra_silence=0.25, inter_scene_silence=1.0, inter_chapter_silence=2.0,
            lead_in_silence=0.0, embedded_images=[],
            # 02's manifests now span 3.0 s; the mixed chapter was 10 s.
            chapters=_chapters([("01", 0.0, 3.5), ("02", 5.5, 15.5), ("03", 17.5, 21.0)]),
        )
        manifest, lines = _sync(book, novel, epub)
        b = {x["beat_id"]: x for x in manifest["beats"]}
        assert b["03_s1_b0000"]["start_seconds"] == 17.5
        warns = [ln for ln in lines if "[WARN] chapter" in ln]
        assert len(warns) == 1 and "chapter 02" in warns[0], lines


def test_newest_sidecar_wins_over_a_stale_mix():
    with _tmp() as root:
        final = root / "06_final"
        old = _write_sidecar(final, "Old")
        new = _write_sidecar(final, "New")
        os.utime(old, (1_000_000, 1_000_000))
        assert s5_mix.find_mix_sidecar(final) == new
        assert s5_mix.find_mix_sidecar(final, "Old") == old
        assert s5_mix.find_mix_sidecar(final, "Missing") is None
        assert s5_mix.find_mix_sidecar(root / "nowhere") is None
        # A title with a slash maps to the same sanitised stem as s5 uses.
        slash = _write_sidecar(final, "demo_vol")
        assert s5_mix.find_mix_sidecar(final, "demo/vol") == slash


# ---- lnvox sample: which m4b gets sliced --------------------------------------


def test_sample_slices_the_m4b_the_manifest_was_timed_against():
    with _tmp() as root:
        final = root / "06_final"
        for stem, t in (("Old", 1_000_000), ("New", 2_000_000)):
            _write_sidecar(final, stem, m4b=f"{stem}.m4b")
            (final / f"{stem}.m4b").write_bytes(b"x")
            for p in (final / f"{stem}.m4b", final / f"{stem}.timings.json"):
                os.utime(p, (t, t))
        said: list[str] = []
        pick = lambda man, title=None: pick_source_audio(final, man, title, progress=said.append)

        assert pick({"m4b": "Old.m4b"}).name == "Old.m4b" and not said
        assert pick({}).name == "New.m4b"                      # newest mix
        assert pick({"m4b": "Old.m4b"}, "New").name == "New.m4b"
        assert said and "timed against Old.m4b" in said[-1]
        assert pick({}, "Not a mix title").name == "New.m4b"   # --title is the sample's
        (final / "New.timings.json").unlink()
        (final / "Old.timings.json").unlink()
        assert pick({}).name == "New.m4b"                      # newest m4b


# ---- scenario-sync: same default ----------------------------------------------


def _scenario(root: Path) -> Path:
    book = root / "play"
    (book / "03_directed").mkdir(parents=True)
    scenes = []
    for sid in ("01", "02"):
        scenes.append(ScriptScene(scene_id=sid, title=sid, items=[
            ScriptItem(type="dialogue", speaker="A", text="un"),
            ScriptItem(type="dialogue", speaker="B", text="deux"),
        ]))
        beats = [DirectedBeat(type="dialogue", text=t, speaker="A", direction="",
                              prompt="p", source_paragraph=i + 1)
                 for i, t in enumerate(("un", "deux"))]
        ch = ChapterDirected(chapter_id=sid, scenes=[DirectedScene(scene_id=f"{sid}_g00", beats=beats)])
        (book / "03_directed" / f"{sid}.json").write_text(ch.model_dump_json())
        d = book / "05_audio" / sid
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(ChapterAudio(chapter_id=sid, beats=[
            RenderedBeat(beat_id=f"{sid}_g00_b{i:04d}", scene_id=f"{sid}_g00",
                         type="dialogue", speaker="A", wav_path="x.wav",
                         duration_seconds=2.0, cache_key="k")
            for i in range(2)
        ]).model_dump_json())
    (book / "00_script.json").write_text(
        ScenarioScript(scenario_id="play", title="Play", scenes=scenes).model_dump_json()
    )
    return book


def test_scenario_sync_follows_the_mix_pads_and_scene_list():
    with _tmp() as root:
        book = _scenario(root)
        quiet = lambda _m: None

        play = scenario_sync.run(book, progress=quiet)      # no mix: play constants
        assert [s.scene_id for s in play.scenes] == ["01", "02"]
        assert (play.lead_in_silence, play.inter_scene_silence) == (0.35, 0.75)

        _write_sidecar(
            book / "06_final", "Play", m4b="Play.m4b", audio_dir="05_audio",
            intra_silence=0.5, inter_scene_silence=1.0, inter_chapter_silence=0.75,
            lead_in_silence=0.2, chapters=_chapters([("02", 0.0, 4.9)]),
        )
        play = scenario_sync.run(book, progress=quiet)
        assert [s.scene_id for s in play.scenes] == ["02"]   # an s5 --chapters 02 mix
        assert play.scenes[0].start == 0.0
        assert (play.intra_silence, play.lead_in_silence) == (0.5, 0.2)
        e = play.scenes[0].entries
        assert (e[1].start, e[1].end) == (2.7, 4.9)          # 2.2 + 0.5, + 0.2 + 2.0
        assert play.total_duration_seconds == 4.9            # = the mixed chapter

        play = scenario_sync.run(book, lead_in=0.0, progress=quiet)
        assert play.lead_in_silence == 0.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

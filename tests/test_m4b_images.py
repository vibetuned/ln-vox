"""Embedded pictures ↔ sync-manifest images (DESIGN.md §23).

ln-reader pairs `manifest.images[i]` with the i-th picture in the m4b's `covr`
atom, by position — MP4 keeps no per-picture name. Every test here guards a
way that pairing broke on the demo run.

All content below is invented — no real book text or artwork.
"""

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from lnvox.stages.s5_mix import embedded_image_order
from lnvox.stages.s6_sync import _image_owners, align_images_to_embeds
from lnvox.stages.sample import _rebase, _slice_audio, select_pictures

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _png(path: Path, rgb: tuple[int, int, int], size=(16, 12)) -> Path:
    from PIL import Image
    Image.new("RGB", size, rgb).save(path, "JPEG")
    return path


# ---- s5: the embed order -----------------------------------------------------


def test_embed_order_is_cover_first_deduplicated_and_skips_missing(tmp_path):
    cover = _png(tmp_path / "cover.jpg", (255, 0, 0))
    a = _png(tmp_path / "a.jpg", (0, 255, 0))
    order = embedded_image_order(cover, [a, cover, tmp_path / "missing.jpg"])
    assert [p.name for p in order] == ["cover.jpg", "a.jpg"]


# ---- s6: one manifest image per embedded picture -----------------------------


def test_alignment_is_one_entry_per_picture_in_embed_order():
    """s6 used to write one entry per <img> OCCURRENCE; s5 embeds one per FILE.
    Oz: 1,453 entries for 148 pictures, 0 of them at the right position."""
    marks = [
        {"src": "../images/b.jpg", "trigger_seconds": 40.0},
        {"src": "../images/b.jpg", "trigger_seconds": 90.0},   # repeat
        {"src": "../images/a.jpg", "trigger_seconds": 10.0},
    ]
    embedded = [Path("cover.jpg"), Path("a.jpg"), Path("b.jpg")]
    out = align_images_to_embeds(marks, embedded)
    assert [Path(str(x["src"])).name for x in out] == ["cover.jpg", "a.jpg", "b.jpg"]
    assert [x["trigger_seconds"] for x in out] == [None, 10.0, 40.0]


def test_a_picture_nothing_reaches_still_holds_its_slot():
    """Dropping it would shift every later picture by one — the original bug."""
    out = align_images_to_embeds(
        [{"src": "b.jpg", "trigger_seconds": 5.0}],
        [Path("cover.jpg"), Path("orphan.jpg"), Path("b.jpg")],
    )
    assert len(out) == 3
    assert out[1]["trigger_seconds"] is None
    assert out[2]["trigger_seconds"] == 5.0


def test_first_TIMED_occurrence_wins_over_an_earlier_untimed_one():
    out = align_images_to_embeds(
        [{"src": "a.jpg", "trigger_seconds": None}, {"src": "a.jpg", "trigger_seconds": 7.0}],
        [Path("a.jpg")],
    )
    assert out[0]["trigger_seconds"] == 7.0


# ---- s6: a shared page's pictures belong to one chapter ----------------------


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


PAGE = (
    "<html><body>"
    '<img src="pre.jpg"/>'
    '<h2 id="c1">One</h2><p>text</p><img src="one.jpg"/>'
    '<h2 id="c2">Two</h2><img src="head2.jpg"/>'              # heading-only section
    '<h2 id="c3">Three</h2><p>text</p><img src="three.jpg"/>'
    "</body></html>"
)


def test_split_page_pictures_go_to_the_section_holding_them():
    """Each chapter used to claim EVERY picture on its shared page."""
    soup = _soup(PAGE)
    owners = _image_owners(
        soup, "pg", {"pg#c1": "01", "pg#c3": "03"}   # c2 produced no chapter
    )
    got = {img["src"]: owners[id(img)] for img in soup.find_all("img")}
    assert got["pre.jpg"] == "01"      # preamble opens the first section
    assert got["one.jpg"] == "01"
    assert got["head2.jpg"] == "03"    # a heading-only section hands off forward
    assert got["three.jpg"] == "03"


def test_single_chapter_page_needs_no_attribution():
    soup = _soup('<html><body><h2 id="c1">One</h2><img src="a.jpg"/></body></html>')
    assert _image_owners(soup, "pg", {"pg#c1": "01"}) == {}


# ---- sample: pictures chosen and aligned -------------------------------------


IMAGES = [
    {"src": "cover.jpg", "trigger_seconds": None},
    {"src": "early.jpg", "trigger_seconds": 50.0},
    {"src": "in1.jpg", "trigger_seconds": 150.0, "before_beat_id": "b_in"},
    {"src": "in2.jpg", "trigger_seconds": 180.0, "before_beat_id": "b_gone"},
    {"src": "late.jpg", "trigger_seconds": 900.0},
]


def test_sample_keeps_the_cover_and_in_window_pictures_in_embed_order():
    assert select_pictures(IMAGES, (100.0, 200.0)) == [0, 2, 3]


def test_sample_manifest_images_mirror_the_embedded_pictures():
    man = {"book_id": "x", "images": IMAGES, "visual_elements": [],
           "beats": [{"beat_id": "b_in", "data_beat_id": "b_in", "chapter_id": "01",
                      "start_seconds": 150.0, "end_seconds": 160.0}]}
    out = _rebase(man, (100.0, 200.0), {"b_in"}, [0, 2, 3])
    assert [x["src"] for x in out["images"]] == ["cover.jpg", "in1.jpg", "in2.jpg"]
    assert [x["trigger_seconds"] for x in out["images"]] == [None, 50.0, 80.0]
    assert out["images"][1]["before_beat_id"] == "b_in"
    assert out["images"][2]["before_beat_id"] is None     # anchor outside the window


# ---- sample: the pictures actually survive the cut ---------------------------


def _covr(path: Path) -> list[bytes]:
    buf = path.read_bytes()

    def walk(start, end):
        i = start
        while i + 8 <= end:
            size, kind = struct.unpack(">I4s", buf[i:i + 8])
            yield kind, i + 8, i + size
            i += size

    def find(start, end, chain):
        if not chain:
            return start, end
        for kind, s, e in walk(start, end):
            if kind == chain[0]:
                r = find(s + 4 if kind == b"meta" else s, e, chain[1:])
                if r:
                    return r
        return None

    r = find(0, len(buf), [b"moov", b"udta", b"meta", b"ilst", b"covr"])
    return [buf[s + 8:e] for k, s, e in walk(*r) if k == b"data"] if r else []


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_sliced_sample_still_carries_its_pictures(tmp_path):
    """An attached picture is one packet at pts 0, so a seek past 0 drops it.
    That is how the first samples shipped with no cover and no images."""
    pics = [_png(tmp_path / f"p{i}.jpg", c) for i, c in
            enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)])]
    src = tmp_path / "book.m4b"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=30"]
    for p in pics:
        cmd += ["-i", str(p)]
    cmd += ["-map", "0:a"] + sum((["-map", f"{i + 1}:v"] for i in range(3)), [])
    cmd += ["-c:a", "aac", "-c:v", "copy"]
    cmd += sum(([f"-disposition:v:{i}", "attached_pic"] for i in range(3)), [])
    subprocess.run(cmd + [str(src)], check=True)

    meta = tmp_path / "ffmeta.txt"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=10000\ntitle=A\n")
    out = tmp_path / "sample.m4b"
    _slice_audio(src, out, (10.0, 20.0), meta, [0, 2])      # cover + third picture

    got = _covr(out)
    want = [pics[0].read_bytes(), pics[2].read_bytes()]
    assert [hashlib.sha1(b).digest() for b in got] == [hashlib.sha1(b).digest() for b in want]
    dur = float(json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(out)],
        capture_output=True, text=True).stdout)["format"]["duration"])
    assert 9.5 < dur < 10.5
    assert not (tmp_path / "sample.audio.m4a").exists()     # temp file cleaned up

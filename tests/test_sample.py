"""Demo-sample construction (DESIGN.md §22).

All titles and ids below are invented — no real book content.
"""

from lnvox.stages.sample import (
    _MIN_IMAGES,
    ChapterSpan,
    _ffmetadata,
    _rebase,
    chapter_spans,
    pick_window,
    score_pairs,
)

SPANS = [
    ChapterSpan("01", "One", 0.0, 1800.0),
    ChapterSpan("02", "Two", 1800.0, 3600.0),
    ChapterSpan("03", "Three", 3600.0, 5400.0),
]


def _beat(bid, cid, lo, hi, speaker="Narrator"):
    return {"beat_id": bid, "data_beat_id": bid, "chapter_id": cid,
            "speaker": speaker, "start_seconds": lo, "end_seconds": hi}


# ---- window ------------------------------------------------------------------


def test_window_contains_the_boundary_with_margin():
    """A transition 20 seconds from the edge is present but undemonstrable."""
    lo, hi = pick_window(SPANS, "01", "02", 12.0)
    assert hi - lo == 720.0
    margin = 720.0 * 0.25
    assert lo + margin <= 1800.0 <= hi - margin


def test_window_slides_to_cover_images():
    """Images cluster, so a rigidly centred window misses them."""
    clustered = [1560.0, 1570.0, 1580.0, 1590.0]      # all before the boundary
    lo, hi = pick_window(SPANS, "01", "02", 12.0, clustered)
    assert sum(1 for t in clustered if lo <= t <= hi) == len(clustered)
    assert lo <= 1800.0 <= hi                          # boundary still inside


def test_window_never_sacrifices_the_boundary_for_images():
    far = [0.0, 10.0, 20.0]                            # only near chapter start
    lo, hi = pick_window(SPANS, "01", "02", 12.0, far)
    margin = 720.0 * 0.25
    assert lo + margin <= 1800.0 <= hi - margin


def test_short_chapters_still_produce_a_window():
    tiny = [ChapterSpan("01", "a", 0.0, 60.0), ChapterSpan("02", "b", 60.0, 120.0)]
    lo, hi = pick_window(tiny, "01", "02", 12.0)
    assert lo < 60.0 < hi


# ---- pair selection ----------------------------------------------------------


def _manifest(beats, images):
    return {"book_id": "demo/x", "beats": beats,
            "visual_elements": [{"src": f"i{i}.jpg", "chapter_id": "01",
                                 "trigger_seconds": t, "after_beat_id": None,
                                 "before_beat_id": None}
                                for i, t in enumerate(images)]}


def test_pairs_meeting_the_image_bar_are_ranked_by_speakers():
    beats = [_beat("b1", "01", 0, 10, "A"), _beat("b2", "01", 10, 20, "B"),
             _beat("b3", "02", 1800, 1810, "C"), _beat("b4", "03", 3600, 3610, "D")]
    images = [1700.0, 1750.0, 1790.0]                  # only around 01|02
    ranked = score_pairs(_manifest(beats, images), SPANS, 12.0)
    assert ranked[0][0] >= _MIN_IMAGES
    assert (ranked[0][2], ranked[0][3]) == ("01", "02")


def test_falls_back_to_pairs_with_any_image_when_none_clear_the_bar():
    beats = [_beat("b1", "01", 0, 10, "A"), _beat("b2", "02", 1800, 1810, "B"),
             _beat("b3", "03", 3600, 3610, "C")]
    ranked = score_pairs(_manifest(beats, [3550.0]), SPANS, 12.0)
    assert ranked[0][0] < _MIN_IMAGES                  # nothing was eligible
    assert ranked[0][0] == 1                           # but the best was chosen


# ---- manifest rebasing -------------------------------------------------------


def test_rebase_filters_and_shifts_to_zero():
    man = _manifest([_beat("in", "01", 100, 110), _beat("out", "01", 5000, 5010)], [])
    out = _rebase(man, (50.0, 200.0))
    assert [b["beat_id"] for b in out["beats"]] == ["in"]
    assert out["beats"][0]["start_seconds"] == 50.0
    assert out["sample"]["window_start_seconds"] == 50.0


def test_rebase_drops_beats_with_no_span_in_the_epub():
    """A timing pointing at a span that doesn't exist is dead weight — and in
    a 12-minute sample it is most of what a tester would click."""
    man = _manifest([_beat("kept", "01", 100, 110), _beat("nospan", "01", 120, 130)], [])
    out = _rebase(man, (0.0, 200.0), present_spans={"kept"})
    assert [b["beat_id"] for b in out["beats"]] == ["kept"]


def test_rebase_nulls_anchors_pointing_outside_the_window():
    man = {"book_id": "x", "beats": [_beat("kept", "01", 100, 110)],
           "visual_elements": [{"src": "i.jpg", "chapter_id": "01",
                                "trigger_seconds": 105.0,
                                "after_beat_id": "gone",
                                "before_beat_id": "kept"}]}
    out = _rebase(man, (0.0, 200.0), present_spans={"kept"})
    ve = out["visual_elements"][0]
    assert ve["after_beat_id"] is None          # dangling anchor removed
    assert ve["before_beat_id"] == "kept"
    assert ve["trigger_seconds"] == 105.0


def test_visual_elements_outside_the_window_are_dropped():
    man = _manifest([_beat("k", "01", 100, 110)], [105.0, 9000.0])
    out = _rebase(man, (0.0, 200.0), present_spans={"k"})
    assert len(out["visual_elements"]) == 1


# ---- chapter markers ---------------------------------------------------------


def test_ffmetadata_clips_and_rebases_chapters():
    meta = _ffmetadata(SPANS, (1500.0, 2220.0), "Title")
    assert meta.count("[CHAPTER]") == 2         # 03 is outside the window
    assert "START=0\n" in meta
    assert "title=One" in meta and "title=Two" in meta


def test_ffmetadata_skips_a_sliver_of_a_chapter():
    meta = _ffmetadata(SPANS, (1799.9, 2500.0), "Title")
    assert meta.count("[CHAPTER]") == 1         # 0.1s of chapter One is not a chapter
    assert "title=Two" in meta


def test_chapter_spans_derive_bounds_from_beats():
    man = _manifest([_beat("a", "01", 5, 10), _beat("b", "01", 20, 30),
                     _beat("c", "02", 40, 50)], [])
    spans = chapter_spans(man, {"01": "One", "02": "Two"})
    assert [(s.chapter_id, s.start, s.end) for s in spans] == [
        ("01", 5.0, 30.0), ("02", 40.0, 50.0)]


def test_below_the_bar_voices_beat_a_second_picture():
    """Flatland: no window reaches 3 images. One image with many voices is a
    better demo than two images and a near-monologue."""
    beats = [_beat("a1", "01", 0, 10, "A"),
             _beat("b1", "02", 1800, 1810, "B"), _beat("b2", "02", 1820, 1830, "C"),
             _beat("b3", "02", 1840, 1850, "D"), _beat("b4", "02", 1860, 1870, "E"),
             _beat("c1", "03", 3600, 3610, "B")]
    images = [1700.0, 1750.0,          # two pictures at 01|02 (voices A,B,C,D,E → 5)
              3500.0]                  # one picture at 02|03 (voices B,C,D,E → 4)
    ranked = score_pairs(_manifest(beats, images), SPANS, 12.0)
    assert (ranked[0][2], ranked[0][3]) == ("01", "02")   # 5 voices, 2 images

    images_one_each = [1700.0, 3500.0]
    beats_many_at_23 = beats + [_beat(f"x{i}", "03", 3620 + i, 3621 + i, f"V{i}") for i in range(4)]
    ranked = score_pairs(_manifest(beats_many_at_23, images_one_each), SPANS, 12.0)
    assert ranked[0][0] >= 1                               # the viewer is shown
    assert (ranked[0][2], ranked[0][3]) == ("02", "03")   # and voices decide

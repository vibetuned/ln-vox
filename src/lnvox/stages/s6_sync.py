"""Stage 6: Re-align Stage-3 beats onto the original EPUB's XHTML.

Outputs:
    artifacts/<book>/07_sync/<book>.epub       — original EPUB with
        ``<span class="lnvox-beat" data-beat-id="…">…</span>`` wrappers around
        every matched beat. Multi-paragraph beats emit multiple spans sharing
        the same data-beat-id.
    artifacts/<book>/07_sync/sync_manifest.json — per-beat audio timing aligned
        to the final m4b (silences from Stage 5 are accounted for) + the
        ``data-beat-id`` to look up in the EPUB.
    artifacts/<book>/07_sync/unmatched.json    — beats the anchor matcher
        couldn't place. Should be small / empty when normalization is sound.

Algorithm — see §2.8 of DESIGN.md. Briefly:
    1. Per chapter XHTML: BeautifulSoup walk → shadow string + index from
       normalized-shadow-pos → (text node, original offset).
    2. Sequential match, cursor-advancing: each beat's verbatim Stage-2
       `source_span` is exact-matched first (the common path); beats whose span
       is missing or drifted fall back to the head/tail (~30 char) + fuzzy
       ladder, with the "always start at previous match end" rule.
    3. DOM wrap: group matches by node, split the node into
       text/span/text/span/… parts in one pass so multiple beats per node work
       correctly.
    4. Repack: copy the original EPUB ZIP, overlaying only the XHTML we
       modified. mimetype + everything else stays byte-identical.
"""

from __future__ import annotations

import difflib
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString


# ---- constants --------------------------------------------------------------

_OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}
_CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}

SPAN_CLASS = "lnvox-beat"
SPAN_ATTR = "data-beat-id"

# Anchor lengths for the head/tail-search strategy. Long enough to be unique
# in most chapters, short enough that dropped attribution tags inside dialogue
# beats don't break the match.
HEAD_ANCHOR_LEN = 30
TAIL_ANCHOR_LEN = 30

_SKIP_NODE_PARENTS = {"script", "style", "head", "title"}


# ---- normalization ----------------------------------------------------------


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Return (normalized text, orig_pos_for_each_normalized_char).

    Transforms applied:
      - smart quotes → straight quotes (1 → 1 char)
      - em / en dash → ASCII hyphen      (1 → 1 char)
      - lowercase                         (1 → 1 char)
      - run of whitespace → single space  (N → 1 char)

    Ligatures (`ﬁ` / `ﬂ`) and the literal ellipsis (`…`) are deliberately
    NOT expanded — that would change the character-count mapping and make
    re-aligning the wrap back to the original DOM offsets fragile. We rely on
    s2/s3's prompts collapsing them the same way the source already does.
    """
    out: list[str] = []
    orig: list[int] = []
    in_ws = False
    for i, ch in enumerate(text):
        if ch in "‘’":
            ch = "'"
        elif ch in "“”":
            ch = '"'
        elif ch in "—–":
            ch = "-"
        if ch.isspace():
            if in_ws:
                continue
            in_ws = True
            out.append(" ")
            orig.append(i)
            continue
        in_ws = False
        out.append(ch.lower())
        orig.append(i)
    return "".join(out), orig


# ---- shadow building --------------------------------------------------------


@dataclass
class _NodeSegment:
    """One text node's slice of the chapter's shadow string."""

    shadow_start: int  # inclusive — position in the chapter shadow string
    shadow_end: int    # exclusive
    node: NavigableString
    # normalized-pos-within-this-segment → original-pos-within-node-text
    norm_to_orig: list[int]


@dataclass
class _ImageMark:
    """An <img> (or SVG image) encountered while walking the chapter, recorded
    at the shadow position where it appears between text nodes."""

    shadow_pos: int
    src: str


def _build_shadow(
    soup: BeautifulSoup,
) -> tuple[str, list[_NodeSegment], list[_ImageMark]]:
    """Concatenate every visible text node into a normalized shadow string.

    Walks the ``<body>`` subtree in document order so interleaved ``<img>``
    elements can be recorded at the shadow position where they fall — that's
    what lets Stage 6 emit "this image appears before beat X". A single space
    is inserted between adjacent text nodes so two nodes can't accidentally
    match across the boundary (e.g. ``</p><p>`` with no actual whitespace).

    The walk is scoped to ``<body>`` (not the whole soup) because BS4's
    ``html.parser`` exposes the ``<?xml … ?>`` declaration's content as a
    document-level NavigableString. A whole-soup walk used to pick that text
    up, the matcher could then wrap it in a ``<span>``, and the resulting
    serialized XHTML had stray spans BEFORE ``<html>`` — well-formed in
    permissive HTML viewers but rejected by strict XML parsers with
    "junk after document element".
    """
    body = soup.find("body") or soup
    parts: list[str] = []
    segments: list[_NodeSegment] = []
    images: list[_ImageMark] = []
    cursor = 0
    for node in body.descendants:
        if isinstance(node, NavigableString):
            if any(p.name in _SKIP_NODE_PARENTS for p in node.parents if p.name):
                continue
            raw = str(node)
            if not raw.strip():
                continue
            normalized, orig_map = _normalize_with_map(raw)
            if not normalized:
                continue
            segments.append(
                _NodeSegment(
                    shadow_start=cursor,
                    shadow_end=cursor + len(normalized),
                    node=node,
                    norm_to_orig=orig_map,
                )
            )
            parts.append(normalized)
            cursor += len(normalized)
            if not normalized.endswith(" "):
                parts.append(" ")
                cursor += 1
        elif getattr(node, "name", None) in ("img", "image"):
            # `image` covers SVG <image xlink:href="…"> used by some EPUBs for
            # full-page art. Record the first href-like attribute we find.
            src = (
                node.get("src")
                or node.get("xlink:href")
                or node.get("href")
                or ""
            )
            images.append(_ImageMark(shadow_pos=cursor, src=src))
    return "".join(parts), segments, images


# ---- matching ---------------------------------------------------------------


@dataclass
class _Match:
    beat_id: str
    shadow_start: int
    shadow_end: int


# Fuzzy-match budget. SequenceMatcher on the whole chapter shadow (~30 kB)
# costs ~100 ms; we cap the search window to keep total Stage-6 runtime
# bounded when many beats fall through to the fuzzy fallback.
_FUZZY_WINDOW_BASE = 3000
_FUZZY_MIN_RATIO = 0.20  # match must cover at least 20% of the normalized beat
_BACKTRACK_WINDOW = 1500  # chars before cursor we'll look for a head anchor


# Max chars a single Pass-1 match may sit ahead of the cursor. Beats are
# roughly sequential, so a match landing tens of thousands of chars forward is
# almost always a false positive on a generic head/tail anchor that recurs
# later in the chapter. Capping the forward search keeps one bad match from
# stranding every beat after it. A run of unmatched beats still fits: ~30 beats
# at ~250 chars each ≈ 7.5K, comfortably under the cap.
_MAX_FORWARD_JUMP = 8000


def _find_beat(
    shadow: str,
    beat_text: str,
    cursor: int,
    *,
    strict: bool = False,
    max_forward: int | None = None,
) -> tuple[int, int, str] | None:
    """Find `beat_text` in `shadow` at or after `cursor`.

    Returns ``(start, end, confidence)`` where confidence is one of:
      - ``"exact"``         full beat text verbatim (short beats)
      - ``"anchored"``      head + tail both matched (long beats, happy path)
      - ``"head-only"``     head matched, tail paraphrased — span is just the
                            verified head anchor (no over-claim)
      - ``"tail-only"``     tail matched, head paraphrased — span is just the
                            verified tail anchor
      - ``"backtrack"``     head found BEFORE cursor (previous beat
                            over-consumed); same conservative claim
      - ``"fuzzy"``         `difflib.SequenceMatcher` longest-common substring
                            fallback. Most lenient.

    When ``strict=True``, ONLY ``"exact"`` and ``"anchored"`` matches are
    returned; the other fallbacks become None. Used in Pass 1 so a lenient
    match doesn't false-positive on similar text and poison the cursor
    position the Pass 2 gap-filler will need.

    Returns ``None`` when no match meets the chosen strictness.
    """
    norm, _ = _normalize_with_map(beat_text)
    if not norm:
        return None

    # Upper bound for where a forward match may START. `None` = unbounded
    # (used by Pass 2 inside an already-constrained gap).
    find_end = (cursor + max_forward) if max_forward is not None else None

    def _find(sub: str, start: int) -> int:
        return shadow.find(sub, start) if find_end is None else shadow.find(sub, start, find_end)

    # 1. Short beat → exact match. (Strict and lenient both accept this.)
    if len(norm) <= HEAD_ANCHOR_LEN + TAIL_ANCHOR_LEN:
        idx = _find(norm, cursor)
        if idx != -1:
            return idx, idx + len(norm), "exact"
        if strict:
            return None
        # Lenient mode for short beats: try backtrack + fuzzy.
        back_start = max(0, cursor - _BACKTRACK_WINDOW)
        idx_back = shadow.rfind(norm, back_start, cursor)
        if idx_back != -1:
            return idx_back, idx_back + len(norm), "backtrack"
        return _fuzzy_match(shadow, norm, cursor)

    head = norm[:HEAD_ANCHOR_LEN]
    tail = norm[-TAIL_ANCHOR_LEN:]

    # 2. Head + tail anchored match (the happy path; strict accepts).
    h = _find(head, cursor)
    if h != -1:
        min_tail_start = h + HEAD_ANCHOR_LEN
        max_tail_end = h + max(len(norm) * 2 + 300, 1000)
        t = shadow.find(tail, min_tail_start, max_tail_end)
        if t != -1:
            return h, t + TAIL_ANCHOR_LEN, "anchored"

    if strict:
        return None

    # ---- lenient-only fallbacks below --------------------------------------

    # 3. Head-only: head matched but tail didn't. Claim ONLY the head anchor.
    if h != -1:
        return h, h + HEAD_ANCHOR_LEN, "head-only"

    # 4. Backtrack: head not found forward — try the window BEFORE cursor.
    back_start = max(0, cursor - _BACKTRACK_WINDOW)
    h_back = shadow.rfind(head, back_start, cursor)
    if h_back != -1:
        min_tail_start = h_back + HEAD_ANCHOR_LEN
        max_tail_end = h_back + max(len(norm) * 2 + 300, 1000)
        t = shadow.find(tail, min_tail_start, max_tail_end)
        if t != -1:
            return h_back, t + TAIL_ANCHOR_LEN, "backtrack"
        return h_back, h_back + HEAD_ANCHOR_LEN, "backtrack"

    # 5. Tail-only forward. Claim ONLY the verified tail anchor.
    t = shadow.find(tail, cursor)
    if t != -1:
        return t, t + TAIL_ANCHOR_LEN, "tail-only"

    # 6. Last resort: fuzzy longest-common substring.
    return _fuzzy_match(shadow, norm, cursor)


def _find_source_span(
    shadow: str, span: str, cursor: int, max_forward: int
) -> tuple[int, int, str] | None:
    """Exact-match a Stage-2 `source_span` in `shadow` at/after `cursor`.

    `source_span` is the verbatim, lossless source slice the beat was grounded
    in (quote marks + attribution kept), so after the same normalization it is
    expected to be an exact substring of the source — no head/tail anchoring
    needed. This is the primary, unambiguous match path; the head/tail/fuzzy
    ladder in `_find_beat` is only the fallback for beats whose span drifted.

    The forward-jump cap guards against the rare case where a short span recurs
    later in the chapter. Returns ``(start, end, "span-exact")`` or None.
    """
    norm, _ = _normalize_with_map(span)
    if not norm:
        return None
    idx = shadow.find(norm, cursor, cursor + max_forward)
    if idx == -1:
        return None
    return idx, idx + len(norm), "span-exact"


def _fuzzy_match(
    shadow: str, norm_beat: str, cursor: int, max_window: int | None = None
) -> tuple[int, int, str] | None:
    """`SequenceMatcher` find_longest_match within a forward-looking window.

    Accepts a match only if the longest matching substring covers at least
    `_FUZZY_MIN_RATIO` of the beat length AND at least 20 chars. Returns only
    the verified longest-common substring (no padding) so we don't over-claim.

    `max_window` overrides the default forward window size — Pass 2 passes the
    full available range so a fuzzy match within a 30K-char chapter gap isn't
    silently capped at the default 3K.
    """
    if not norm_beat:
        return None
    if max_window is None:
        max_window = max(_FUZZY_WINDOW_BASE, len(norm_beat) * 4)
    window_end = min(len(shadow), cursor + max_window)
    if window_end <= cursor:
        return None
    region = shadow[cursor:window_end]
    matcher = difflib.SequenceMatcher(None, norm_beat, region, autojunk=False)
    m = matcher.find_longest_match(0, len(norm_beat), 0, len(region))
    min_size = max(20, int(len(norm_beat) * _FUZZY_MIN_RATIO))
    if m.size < min_size:
        return None
    # Claim ONLY the verified longest-common substring — no padding. Padding
    # would over-extend into text that subsequent beats need, poisoning the
    # forward cursor.
    return cursor + m.b, cursor + m.b + m.size, "fuzzy"


def _find_beat_in_range(
    shadow: str, beat_text: str, range_start: int, range_end: int
) -> tuple[int, int, str] | None:
    """Run the full anchor/fuzzy matcher constrained to [range_start, range_end).

    Pass 2 hands the full gap as a sub-shadow. We use lenient `_find_beat`
    (all fallbacks enabled) and fall through to fuzzy across the ENTIRE gap
    when the anchor fallbacks fail — otherwise fuzzy's 3K-char default window
    would silently miss content in long gaps.
    """
    if range_end <= range_start:
        return None
    sub = shadow[range_start:range_end]
    result = _find_beat(sub, beat_text, 0, strict=False)
    if result is None:
        norm, _ = _normalize_with_map(beat_text)
        result = _fuzzy_match(sub, norm, 0, max_window=len(sub))
    if result is None:
        return None
    s, e, conf = result
    return range_start + s, range_start + e, conf


# ---- DOM wrapping -----------------------------------------------------------


def _wrap_matches(
    soup: BeautifulSoup,
    segments: list[_NodeSegment],
    matches: list[_Match],
) -> None:
    """Mutate `soup` in place so each match's text is wrapped in a span.

    Beats spanning multiple text nodes get one span per node, all sharing the
    same `data-beat-id` (the player joins them client-side).
    """
    # node_id(NavigableString) → list[(orig_start, orig_end, beat_id)]
    # using id() lets us key by identity even after we extract() from the DOM
    per_node: dict[int, list[tuple[int, int, str, NavigableString]]] = defaultdict(list)

    for m in matches:
        for seg in segments:
            # No overlap?
            if seg.shadow_end <= m.shadow_start or seg.shadow_start >= m.shadow_end:
                continue
            # Translate match's shadow range into this segment's normalized
            # local range, then into original-text local offsets via the map.
            local_n_start = max(0, m.shadow_start - seg.shadow_start)
            local_n_end = min(
                seg.shadow_end - seg.shadow_start, m.shadow_end - seg.shadow_start
            )
            if local_n_start >= len(seg.norm_to_orig):
                continue
            orig_start = seg.norm_to_orig[local_n_start]
            if local_n_end > 0 and local_n_end - 1 < len(seg.norm_to_orig):
                orig_end = seg.norm_to_orig[local_n_end - 1] + 1
            else:
                orig_end = len(str(seg.node))
            if orig_end <= orig_start:
                continue
            per_node[id(seg.node)].append((orig_start, orig_end, m.beat_id, seg.node))

    for slices in per_node.values():
        node = slices[0][3]
        parent = node.parent
        if parent is None:
            continue
        # Sort and clamp overlapping slices (defensive — shouldn't happen with
        # sequential matching but free insurance).
        slices.sort(key=lambda s: s[0])
        text = str(node)
        new_parts: list = []
        cursor = 0
        for orig_start, orig_end, beat_id, _ in slices:
            if orig_start < cursor:
                orig_start = cursor
            if orig_start >= orig_end:
                continue
            if orig_start > cursor:
                new_parts.append(NavigableString(text[cursor:orig_start]))
            span = soup.new_tag("span")
            span["class"] = SPAN_CLASS
            span[SPAN_ATTR] = beat_id
            span.string = text[orig_start:orig_end]
            new_parts.append(span)
            cursor = orig_end
        if cursor < len(text):
            new_parts.append(NavigableString(text[cursor:]))

        idx = list(parent.contents).index(node)
        node.extract()
        for offset, part in enumerate(new_parts):
            parent.insert(idx + offset, part)


# ---- EPUB inspection + repack -----------------------------------------------


def _parse_opf(
    epub_path: Path,
) -> tuple[str, dict[str, str], list[str]]:
    """Parse the OPF.

    Returns ``(opf_dir_prefix, stem_lower → href, spine_stems_in_order)``.
    ``spine_stems_in_order`` is the document-flow order of XHTML stems, used
    to place image-only spine pages (light-novel inserts) relative to the
    text chapters around them.
    """
    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", _CONTAINER_NS)
        if rootfile is None:
            raise ValueError(f"{epub_path}: no rootfile in container.xml")
        opf_path = rootfile.get("full-path") or ""
        opf_dir = Path(opf_path).parent.as_posix()
        if opf_dir == ".":
            opf_dir = ""
        opf = ET.fromstring(zf.read(opf_path))

        stem_to_href: dict[str, str] = {}
        id_to_stem: dict[str, str] = {}
        for item in opf.findall(".//opf:item", _OPF_NS):
            href = item.get("href") or ""
            mt = item.get("media-type") or ""
            if "html" not in mt and "xml" not in mt:
                continue
            stem = Path(href).stem.lower()
            stem_to_href[stem] = href
            iid = item.get("id") or ""
            if iid:
                id_to_stem[iid] = stem

        spine_stems: list[str] = []
        spine = opf.find(".//opf:spine", _OPF_NS)
        if spine is not None:
            for itemref in spine.findall("opf:itemref", _OPF_NS):
                idref = itemref.get("idref") or ""
                stem = id_to_stem.get(idref)
                if stem:
                    spine_stems.append(stem)

        return opf_dir, stem_to_href, spine_stems


def _write_modified_epub(
    orig_path: Path,
    modifications: dict[str, bytes],
    output_path: Path,
) -> None:
    """Copy the EPUB, overlaying only the XHTML files we modified."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(orig_path, "r") as src, zipfile.ZipFile(
        output_path, "w"
    ) as dst:
        for item in src.infolist():
            if item.filename in modifications:
                # Preserve the original ZipInfo's compression to keep
                # mimetype STORED (uncompressed) per EPUB spec.
                new_item = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                new_item.compress_type = item.compress_type
                new_item.external_attr = item.external_attr
                dst.writestr(new_item, modifications[item.filename])
            else:
                dst.writestr(item, src.read(item.filename))


# ---- timing accumulation ----------------------------------------------------


def _compute_beat_timings(
    audio_dir: Path,
    *,
    intra_silence: float,
    inter_scene_silence: float,
    inter_chapter_silence: float,
) -> dict[str, tuple[float, float]]:
    """beat_id → (start_seconds, end_seconds) in the final m4b time space.

    Mirrors Stage 5's silence layout: intra-scene between consecutive beats of
    one scene, inter-scene at scene boundaries, inter-chapter between chapters.
    """
    timings: dict[str, tuple[float, float]] = {}
    cursor = 0.0
    chapter_dirs = sorted(p for p in audio_dir.iterdir() if p.is_dir())
    for ci, chap_dir in enumerate(chapter_dirs):
        mani_path = chap_dir / "manifest.json"
        if not mani_path.exists():
            continue
        data = json.loads(mani_path.read_text(encoding="utf-8"))
        prev_scene: str | None = None
        for i, beat in enumerate(data["beats"]):
            if i > 0:
                cursor += (
                    inter_scene_silence
                    if beat["scene_id"] != prev_scene
                    else intra_silence
                )
            start = cursor
            cursor += float(beat["duration_seconds"])
            timings[beat["beat_id"]] = (round(start, 3), round(cursor, 3))
            prev_scene = beat["scene_id"]
        if ci < len(chapter_dirs) - 1:
            cursor += inter_chapter_silence
    return timings


# ---- entry point ------------------------------------------------------------


def run(
    book_id: str,
    book_dir: Path,
    epub_path: Path,
    novel_dir: Path,
    output_dir: Path,
    *,
    intra_silence: float = 0.25,
    inter_scene_silence: float = 1.0,
    inter_chapter_silence: float = 2.0,
    progress: Callable[[str], None] = print,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    epub_meta_path = novel_dir / ".epub_meta.json"
    if not epub_meta_path.exists():
        raise FileNotFoundError(
            f"Missing {epub_meta_path}; was this book imported via lnvox ingest-epub?"
        )
    epub_meta = json.loads(epub_meta_path.read_text(encoding="utf-8"))

    # chapter_id (e.g. "01") → list of XHTML stems (e.g. ["chapter1", "chapter1_1"])
    chapter_stems: dict[str, list[str]] = {}
    for ch in epub_meta.get("chapters", []):
        chapter_id = (ch.get("file") or "").split("-", 1)[0]
        if chapter_id:
            chapter_stems[chapter_id] = ch.get("source_parts") or []

    # Reverse map: XHTML stem → chapter_id (for spine-order image placement).
    stem_to_chapter: dict[str, str] = {}
    for cid, parts in chapter_stems.items():
        for st in parts:
            stem_to_chapter[st.lower()] = cid

    opf_dir, stem_to_href, spine_stems = _parse_opf(epub_path)
    progress(
        f"EPUB OPF: {len(stem_to_href)} XHTML/HTML manifest entries, "
        f"{len(spine_stems)} spine items, opf_dir={opf_dir!r}"
    )

    # Per-beat timing in m4b time space.
    audio_dir = book_dir / "05_audio"
    timings = _compute_beat_timings(
        audio_dir,
        intra_silence=intra_silence,
        inter_scene_silence=inter_scene_silence,
        inter_chapter_silence=inter_chapter_silence,
    )
    progress(f"Computed timing for {len(timings)} beat(s) from {audio_dir}")

    # Directed beats with per-chapter ordering.
    directed_dir = book_dir / "03_directed"
    chapter_beats: dict[str, list[tuple[str, dict]]] = {}
    for path in sorted(directed_dir.glob("*.json")):
        directed = json.loads(path.read_text(encoding="utf-8"))
        cid = directed["chapter_id"]
        ordered: list[tuple[str, dict]] = []
        for scene in directed["scenes"]:
            for i, beat in enumerate(scene["beats"]):
                beat_id = f"{scene['scene_id']}_b{i:04d}"
                ordered.append((beat_id, beat))
        chapter_beats[cid] = ordered

    modifications: dict[str, bytes] = {}
    matched_beats: list[dict] = []
    unmatched: list[dict] = []
    image_marks: list[dict] = []
    # chapter_id → (first_matched_beat_id, last_matched_beat_id) for spine
    # image placement.
    chapter_first_last: dict[str, tuple[str, str]] = {}
    confidence_counts: dict[str, int] = {
        "span-exact": 0,
        "exact": 0,
        "anchored": 0,
        "head-only": 0,
        "tail-only": 0,
        "backtrack": 0,
        "fuzzy": 0,
    }

    with zipfile.ZipFile(epub_path, "r") as zf:
        for chapter_id, beats_in_order in chapter_beats.items():
            stems = chapter_stems.get(chapter_id, [])
            if not stems:
                progress(
                    f"  [skip] {chapter_id}: no source_parts in .epub_meta.json"
                )
                for beat_id, beat in beats_in_order:
                    unmatched.append({
                        "beat_id": beat_id,
                        "chapter_id": chapter_id,
                        "text_preview": beat["text"][:120],
                        "reason": "no source_parts for chapter",
                    })
                continue

            # Build a MASTER shadow for the whole chapter by concatenating
            # every source XHTML part. Stage-2/3 sometimes paraphrases
            # bridge text near part boundaries, so per-part matching loses
            # downstream beats; per-chapter matching survives those gaps.
            arc_for_segments: dict[int, str] = {}
            soup_for_arc: dict[str, BeautifulSoup] = {}
            chapter_segments: list[_NodeSegment] = []
            chapter_images: list[tuple[int, str, str]] = []  # (master_pos, src, arc)
            master_parts: list[str] = []
            master_cursor = 0
            for stem in stems:
                href = stem_to_href.get(stem.lower())
                if not href:
                    progress(
                        f"  [skip] {chapter_id}/{stem}: not in OPF manifest"
                    )
                    continue
                arcname = (opf_dir + "/" if opf_dir else "") + href
                xhtml_bytes = zf.read(arcname)
                soup = BeautifulSoup(xhtml_bytes, "html.parser")
                soup_for_arc[arcname] = soup
                local_shadow, local_segments, local_images = _build_shadow(soup)
                for seg in local_segments:
                    seg.shadow_start += master_cursor
                    seg.shadow_end += master_cursor
                    chapter_segments.append(seg)
                    arc_for_segments[id(seg.node)] = arcname
                for im in local_images:
                    chapter_images.append(
                        (im.shadow_pos + master_cursor, im.src, arcname)
                    )
                master_parts.append(local_shadow)
                master_cursor += len(local_shadow)

            master_shadow = "".join(master_parts)

            # ---- Pass 1: forward sequential, STRICT -------------------------
            # Only exact/anchored matches in Pass 1. A lenient match (tail-
            # only, fuzzy, …) can land at the wrong position when a beat
            # happens to share short substrings with text elsewhere in the
            # chapter — that false positive then advances the cursor past
            # genuine match positions for subsequent beats. Pass 2 handles
            # the lenient cases within tight gap windows where the false-
            # positive risk is contained.
            pass1: list[tuple[str, dict, _Match | None, str]] = []
            cursor = 0
            for beat_id, beat in beats_in_order:
                # Primary: exact match on the verbatim source_span from Stage 2.
                result = None
                span = beat.get("source_span") or ""
                if span:
                    result = _find_source_span(
                        master_shadow, span, cursor, _MAX_FORWARD_JUMP
                    )
                # Fallback: the lossy-text head/tail ladder for beats whose
                # span is missing (legacy data / split remainders) or drifted.
                if result is None:
                    result = _find_beat(
                        master_shadow,
                        beat["text"],
                        cursor,
                        strict=True,
                        max_forward=_MAX_FORWARD_JUMP,
                    )
                if result is None:
                    pass1.append((beat_id, beat, None, ""))
                    continue
                s, e, conf = result
                pass1.append((beat_id, beat, _Match(beat_id, s, e), conf))
                cursor = e

            # ---- Pass 2: retry unmatched beats in the gaps ------------------
            # For each None entry, scan back/forward in pass1 to find the
            # bracketing matches' shadow positions. Search the failing beat
            # in that range, BUT allow looking ~2000 chars before prev_end —
            # Stage 2 sometimes reorders dialogue attribution (e.g. source
            # has "Patrick admitted, '…'" but Stage 2 emits the dialogue
            # beat first and the "Patrick admitted" narration beat second,
            # even though it's earlier in the source). Without the
            # backward slack, those beats can never be found.
            #
            # Repeat the pass until no new matches land — each round shrinks
            # the unmatched-gap that subsequent rounds need to search.
            _PASS2_BACKWARD_SLACK = 2000
            for _round in range(3):
                round_matches = 0
                for i, (beat_id, beat, m, _) in enumerate(pass1):
                    if m is not None:
                        continue
                    prev_end = 0
                    for j in range(i - 1, -1, -1):
                        if pass1[j][2] is not None:
                            prev_end = pass1[j][2].shadow_end
                            break
                    next_start = len(master_shadow)
                    for j in range(i + 1, len(pass1)):
                        if pass1[j][2] is not None:
                            next_start = pass1[j][2].shadow_start
                            break
                    search_start = max(0, prev_end - _PASS2_BACKWARD_SLACK)
                    if next_start <= search_start:
                        continue
                    # Try the verbatim span first within the gap, then the
                    # lenient text ladder.
                    result = None
                    span = beat.get("source_span") or ""
                    if span:
                        result = _find_source_span(
                            master_shadow, span, search_start,
                            next_start - search_start,
                        )
                    if result is None:
                        result = _find_beat_in_range(
                            master_shadow, beat["text"], search_start, next_start
                        )
                    if result is None:
                        continue
                    s, e, conf = result
                    pass1[i] = (beat_id, beat, _Match(beat_id, s, e), conf + "+pass2")
                    round_matches += 1
                if round_matches == 0:
                    break

            # ---- Collect matches + record manifest entries ------------------
            chapter_matches: list[_Match] = []
            chapter_matched = 0
            chapter_tail_only = 0
            for beat_id, beat, m, conf in pass1:
                if m is None:
                    unmatched.append({
                        "beat_id": beat_id,
                        "chapter_id": chapter_id,
                        "text_preview": beat["text"][:120],
                        "reason": "no anchor in chapter source (pass1+pass2)",
                    })
                    continue
                base_conf = conf.replace("+pass2", "")
                confidence_counts[base_conf] = confidence_counts.get(base_conf, 0) + 1
                if base_conf == "tail-only":
                    chapter_tail_only += 1
                chapter_matches.append(m)
                chapter_matched += 1
                arc_for_match = next(
                    (
                        arc_for_segments[id(seg.node)]
                        for seg in chapter_segments
                        if seg.shadow_start <= m.shadow_start < seg.shadow_end
                    ),
                    "",
                )
                timing = timings.get(beat_id, (0.0, 0.0))
                matched_beats.append({
                    "beat_id": beat_id,
                    "data_beat_id": beat_id,
                    "chapter_id": chapter_id,
                    "xhtml": arc_for_match,
                    "type": beat.get("type", ""),
                    "speaker": beat.get("speaker", ""),
                    "start_seconds": timing[0],
                    "end_seconds": timing[1],
                    "match_confidence": conf,
                })

            # Record first/last matched beat (in source position order) for
            # spine-level image placement after the chapter loop.
            if chapter_matches:
                by_pos = sorted(chapter_matches, key=lambda m: m.shadow_start)
                chapter_first_last[chapter_id] = (
                    by_pos[0].beat_id,
                    by_pos[-1].beat_id,
                )

            # ---- Map INLINE images to neighbouring beats --------------------
            # Images embedded directly in the chapter text (rare for LN, but
            # possible). Spine-level insert pages are handled after the loop.
            # An image at shadow position P sits *after* the last beat whose
            # span ends at/before P, and *before* the first beat whose span
            # starts at/after P. The player triggers the image when it reaches
            # before_beat_id's start_seconds.
            if chapter_images:
                by_start = sorted(chapter_matches, key=lambda m: m.shadow_start)
                for pos, src, arcname in chapter_images:
                    after_beat = None
                    for m in by_start:
                        if m.shadow_end <= pos:
                            after_beat = m.beat_id
                        else:
                            break
                    before_beat = next(
                        (m.beat_id for m in by_start if m.shadow_start >= pos),
                        None,
                    )
                    before_timing = (
                        timings.get(before_beat, (None, None))[0]
                        if before_beat
                        else None
                    )
                    image_marks.append({
                        "src": src,
                        "xhtml": arcname,
                        "chapter_id": chapter_id,
                        "after_beat_id": after_beat,
                        "before_beat_id": before_beat,
                        "trigger_seconds": before_timing,
                    })

            # Wrap matches in their respective XHTMLs. _wrap_matches only acts
            # on segments that overlap the matches, so passing the full
            # `chapter_segments` and matches to each soup is safe but
            # wasteful. Filter per-arc for clarity.
            for arcname, soup in soup_for_arc.items():
                soup_segments = [
                    s for s in chapter_segments if arc_for_segments[id(s.node)] == arcname
                ]
                relevant = [
                    m
                    for m in chapter_matches
                    if any(
                        s.shadow_start <= m.shadow_start < s.shadow_end
                        or s.shadow_start < m.shadow_end <= s.shadow_end
                        or (m.shadow_start <= s.shadow_start and m.shadow_end >= s.shadow_end)
                        for s in soup_segments
                    )
                ]
                if not relevant:
                    continue
                _wrap_matches(soup, soup_segments, relevant)
                modifications[arcname] = str(soup).encode("utf-8")

            tail_note = (
                f" ({chapter_tail_only} tail-only)" if chapter_tail_only else ""
            )
            progress(
                f"  ✓ {chapter_id}: matched {chapter_matched}/"
                f"{len(beats_in_order)} beats{tail_note}"
            )

        # ---- Spine-level image pages (light-novel inserts) -----------------
        # Image-only spine items (insert*, color*, bonus*, cover, …) were
        # skipped from text extraction but they sit between text chapters in
        # reading order. Place each between the preceding XHTML part's last
        # beat and the following part's first beat — per-PART (not per-chapter)
        # so an insert that falls between chapter1.xhtml and chapter1_1.xhtml
        # gets the correct mid-chapter trigger.
        #
        # arc_first_last is keyed by the XHTML arcname each beat matched in;
        # matched_beats is in playback (beat) order so first/last are the
        # earliest/latest-playing beats of that part.
        arc_first_last: dict[str, tuple[str, str]] = {}
        for mb in matched_beats:
            arc = mb["xhtml"]
            if not arc:
                continue
            if arc not in arc_first_last:
                arc_first_last[arc] = (mb["beat_id"], mb["beat_id"])
            else:
                arc_first_last[arc] = (arc_first_last[arc][0], mb["beat_id"])

        def _arc_for_stem(st: str) -> str:
            href_ = stem_to_href.get(st)
            return ((opf_dir + "/" if opf_dir else "") + href_) if href_ else ""

        for idx, stem in enumerate(spine_stems):
            if stem in stem_to_chapter:
                continue  # this spine item is a text chapter, not an image page
            href = stem_to_href.get(stem)
            if not href:
                continue
            arcname = (opf_dir + "/" if opf_dir else "") + href
            try:
                page = BeautifulSoup(zf.read(arcname), "html.parser")
            except KeyError:
                continue
            srcs = [
                (img.get("src") or img.get("xlink:href") or img.get("href") or "")
                for img in page.find_all(["img", "image"])
            ]
            srcs = [s for s in srcs if s]
            if not srcs:
                continue
            # Walk spine backward → last beat of the nearest preceding text part.
            after_beat = None
            after_chapter = None
            for j in range(idx - 1, -1, -1):
                arc_j = _arc_for_stem(spine_stems[j])
                if arc_j in arc_first_last:
                    after_beat = arc_first_last[arc_j][1]
                    after_chapter = stem_to_chapter.get(spine_stems[j])
                    break
            # Walk spine forward → first beat of the nearest following text part.
            before_beat = None
            before_chapter = None
            for j in range(idx + 1, len(spine_stems)):
                arc_j = _arc_for_stem(spine_stems[j])
                if arc_j in arc_first_last:
                    before_beat = arc_first_last[arc_j][0]
                    before_chapter = stem_to_chapter.get(spine_stems[j])
                    break
            trigger = (
                timings.get(before_beat, (None, None))[0] if before_beat else None
            )
            for src in srcs:
                image_marks.append({
                    "src": src,
                    "xhtml": arcname,
                    "spine_page": stem,
                    "after_chapter": after_chapter,
                    "before_chapter": before_chapter,
                    "after_beat_id": after_beat,
                    "before_beat_id": before_beat,
                    "trigger_seconds": trigger,
                })

    # Write the modified EPUB.
    safe_book_id = book_id.replace("/", "_")
    output_epub = output_dir / f"{safe_book_id}.epub"
    _write_modified_epub(epub_path, modifications, output_epub)

    manifest = {
        "book_id": book_id,
        "epub": str(output_epub),
        "span_class": SPAN_CLASS,
        "data_attr": SPAN_ATTR,
        "total_beats": len(matched_beats) + len(unmatched),
        "matched": len(matched_beats),
        "unmatched": len(unmatched),
        "match_confidence": confidence_counts,
        "silences": {
            "intra_seconds": intra_silence,
            "inter_scene_seconds": inter_scene_silence,
            "inter_chapter_seconds": inter_chapter_silence,
        },
        "images": image_marks,
        "beats": matched_beats,
    }
    (output_dir / "sync_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "unmatched.json").write_text(
        json.dumps({"beats": unmatched}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    pct = (
        100.0 * len(matched_beats) / max(1, len(matched_beats) + len(unmatched))
    )
    progress(
        f"Done. {len(matched_beats)}/{len(matched_beats) + len(unmatched)} "
        f"beats matched ({pct:.1f}%), {len(image_marks)} image(s) located. "
        f"EPUB: {output_epub}"
    )
    return manifest

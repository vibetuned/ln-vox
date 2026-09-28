"""Stage 6: Re-align Stage-3 beats onto the original EPUB's XHTML.

Outputs:
    artifacts/<book>/07_sync/<book>.epub       — original EPUB with
        ``<span class="lnvox-beat" data-beat-id="…">…</span>`` wrappers around
        every matched beat. Multi-paragraph beats emit multiple spans sharing
        the same data-beat-id.
    artifacts/<book>/07_sync/sync_manifest.json — per-beat audio timing aligned
        to the final m4b (replaying the layout Stage 5 recorded in
        ``06_final/<title>.timings.json``) + the ``data-beat-id`` to look up
        in the EPUB.
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
from bs4.element import PreformattedString


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
        low = ch.lower()
        # A handful of codepoints lowercase to MORE than one char ('İ' →
        # 'i̇'). Emit one map entry per output char, all pointing at the
        # same original index, so len(normalized) == len(map) always holds —
        # otherwise every wrap offset after that char shifts.
        for c in low:
            out.append(c)
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
    # The <img> itself, so a picture on a page shared by several chapters can
    # be attributed to the section that actually holds it (§23.2).
    node: object = None


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
            # Comments / CDATA / processing instructions / doctypes are all
            # NavigableString subclasses (via PreformattedString). Their text
            # is invisible in a reader — letting it into the shadow both
            # misplaces matches AND, if a match lands inside one, the wrap
            # step replaces the comment node with a plain-text span, making
            # previously hidden text visible in the synced EPUB.
            if isinstance(node, PreformattedString):
                continue
            if any(p.name in _SKIP_NODE_PARENTS for p in node.parents if p.name):
                continue
            raw = str(node)
            if not raw.strip():
                continue
            normalized, orig_map = _normalize_with_map(raw)
            # The shadow already ends in a space (a node's own or the one put
            # between nodes): a node starting with whitespace — the text after
            # an inline element, `<i>x</i> y` or an empty noteref — would
            # double it, and a beat's span then never matches exactly.
            if cursor and normalized.startswith(" "):
                normalized, orig_map = normalized[1:], orig_map[1:]
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
            images.append(_ImageMark(shadow_pos=cursor, src=src, node=node))
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

# There used to be a lenient "backtrack" rung here: look up to 1500 chars
# BEFORE the cursor for a beat the previous match over-consumed. It could
# never fire — Pass 1 is strict (returns before the lenient rungs) and Pass 2
# calls `_find_beat` on the gap slice with cursor 0, so the window before the
# cursor was always empty. Pass 2's own backward slack (2000 chars before the
# previous match's end, searched forward from its start) already covers that
# window and more, so the rung was removed rather than revived: reviving it
# would mean searching 2000-3500 chars behind the previous match, inside text
# earlier beats already claimed. The `"backtrack"` histogram key stays (always
# 0) so existing manifest readers keep working.


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
      - ``"fuzzy"``         `difflib.SequenceMatcher` longest-common substring
                            fallback. Most lenient.

    Lenient order: long beats ``anchored`` → ``head-only`` → ``tail-only`` →
    ``fuzzy``; short beats ``exact`` → ``fuzzy``. Nothing looks before
    `cursor` — Pass 2 starts its gap slice early instead (see
    `_PASS2_BACKWARD_SLACK` in `run`).

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
        # Lenient mode for short beats: fuzzy.
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

    # 4. Tail-only forward. Claim ONLY the verified tail anchor.
    t = shadow.find(tail, cursor)
    if t != -1:
        return t, t + TAIL_ANCHOR_LEN, "tail-only"

    # 5. Last resort: fuzzy longest-common substring.
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

        # Find the node's position by IDENTITY. list.index() compares with
        # ==, and NavigableString == is string equality — with two identical
        # text nodes under one parent (repeated phrases around inline tags)
        # it returns the FIRST twin, and the replacement parts get inserted
        # there, reordering the chapter text.
        idx = next(
            i for i, c in enumerate(parent.contents) if c is node
        )
        node.extract()
        for offset, part in enumerate(new_parts):
            parent.insert(idx + offset, part)


# ---- EPUB inspection + repack -----------------------------------------------


# Media types that can be an EPUB content document (a page in the spine).
_CONTENT_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html", "image/svg+xml"})


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

        # Only EPUB content documents can be pages. The old test — any
        # media-type containing "xml" — also admitted the NCX
        # (`application/x-dtbncx+xml`), and because items are keyed by bare
        # stem, `toc.ncx` overwrote `toc.xhtml`: the spine's TOC page was then
        # read as the NCX, which is what bs4's XMLParsedAsHTMLWarning was
        # pointing at (Index v15-v17). SVG stays: EPUB 3 allows an SVG content
        # document as a spine item (full-page art).
        items: list[tuple[str, str]] = []           # (id, href)
        for item in opf.findall(".//opf:item", _OPF_NS):
            mt = (item.get("media-type") or "").strip().lower()
            if mt not in _CONTENT_MEDIA_TYPES:
                continue
            items.append((item.get("id") or "", item.get("href") or ""))

        spine_ids: list[str] = []
        spine = opf.find(".//opf:spine", _OPF_NS)
        if spine is not None:
            spine_ids = [
                r.get("idref") or "" for r in spine.findall("opf:itemref", _OPF_NS)
            ]
        in_spine = set(spine_ids)

        # Two content documents can still share a stem (`cover.svg` art beside
        # `cover.xhtml`). Map the spine's own item last so it wins: the spine
        # is what the reader walks, and what Stage 0 recorded source_parts from.
        stem_to_href: dict[str, str] = {}
        id_to_stem: dict[str, str] = {}
        for iid, href in sorted(items, key=lambda it: it[0] in in_spine):
            stem = Path(href).stem.lower()
            stem_to_href[stem] = href
            if iid:
                id_to_stem[iid] = stem

        spine_stems = [id_to_stem[i] for i in spine_ids if i in id_to_stem]
        return opf_dir, stem_to_href, spine_stems


# Injected into every XHTML of the synced EPUB. Gutenberg's Ebookmaker output
# sizes illustrations in absolute pixels (`<img style="width: 600px; …">`) and
# ships no max-width rule, so on a phone the page scrolls sideways and chapter
# headings — which in the illustrated editions ARE images — clip at the right
# edge. Measured on the demo run: Oz had 327 pixel-sized elements, 146 of them
# ≥300px, and 0 max-width rules; the three EPUBs that carry their own
# responsive CSS were unaffected.
#
# `!important` is required: an inline `style` attribute otherwise wins over a
# stylesheet rule. `height: auto` must come with it, or capping the width
# against a fixed pixel height distorts every illustration.
_RESPONSIVE_CSS = (
    "img,svg{max-width:100%!important;height:auto!important}"
    "table,pre{max-width:100%;overflow-x:auto}"
    "body{overflow-wrap:break-word}"
)
_RESPONSIVE_MARKER = "lnvox-responsive"


def _inject_responsive_css(xhtml_bytes: bytes) -> bytes | None:
    """Add the viewport-containment style to one XHTML page.

    Returns the new bytes, or None when the page needs no change (already
    injected, or not parseable as a document with a head).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(xhtml_bytes, "html.parser")
    if soup.find("style", attrs={"data-lnvox": _RESPONSIVE_MARKER}):
        return None
    head = soup.find("head")
    if head is None:
        return None
    tag = soup.new_tag("style", type="text/css")
    tag["data-lnvox"] = _RESPONSIVE_MARKER
    tag.string = _RESPONSIVE_CSS
    head.append(tag)
    return str(soup).encode("utf-8")


# Below this share of matched beats actually present as spans, something went
# wrong while writing rather than while matching (DESIGN.md §21.3). Healthy
# books sit at 88-97%; the two that shipped broken were at 7% and 3%.
_SPAN_COVERAGE_FLOOR = 0.75


def _image_owners(soup: BeautifulSoup, stem: str, key_to_chapter: dict[str, str]) -> dict[int, str]:
    """`id(<img>) -> chapter_id` for a page that `--split-chapters` cut into
    several chapters; empty for a page holding a single chapter (§23.2).

    Each chapter used to claim every picture on its shared page, so Oz's 148
    pictures surfaced as 1,453 "occurrences" — each copy snapped to the start
    of whichever chapter happened to share the page. Ownership follows the
    ingest split exactly (same markers, same keys): text before the first
    marker opens the first section, and a section that produced no chapter
    (a heading with no prose) hands its pictures to the chapter after it,
    which is the one that heading introduces.
    """
    from lnvox.ingest.epub import chapter_markers, section_key

    markers = chapter_markers(soup)
    if len(markers) < 2:
        return {}
    owners_by_section = [
        key_to_chapter.get(section_key(stem, m, i)) for i, m in enumerate(markers)
    ]
    if not any(owners_by_section):
        return {}
    following = None
    for i in range(len(owners_by_section) - 1, -1, -1):
        if owners_by_section[i] is None:
            owners_by_section[i] = following
        else:
            following = owners_by_section[i]
    preceding = None
    for i, owner in enumerate(owners_by_section):
        if owner is None:
            owners_by_section[i] = preceding
        else:
            preceding = owner

    index_of = {id(m): i for i, m in enumerate(markers)}
    owners: dict[int, str] = {}
    section = 0
    for el in soup.find_all(True):
        i = index_of.get(id(el))
        if i is not None:
            section = i
            continue
        if el.name in ("img", "image") and owners_by_section[section]:
            owners[id(el)] = owners_by_section[section]
    return owners


def align_images_to_embeds(image_marks: list[dict], embedded: list[Path]) -> list[dict]:
    """One manifest image per EMBEDDED m4b picture, in embed order (§23).

    ln-reader pairs `images[i]` with the i-th embedded picture by position —
    MP4's `covr` atom carries no per-picture name, so position is all there is.
    Stage 6 used to write one entry per `<img>` occurrence in the EPUB while
    Stage 5 embeds one picture per unique file, cover first, so the two never
    lined up: 0 of 148 Oz markers showed the right picture, 0 of 45 in Alice.

    Each picture takes its first occurrence that has a playback time. A picture
    no beat follows (end matter, or one no page references — often the cover
    image itself; a front-matter page's picture triggers at the first beat)
    still gets an entry, with `trigger_seconds` None — dropping it would shift every later
    picture by one, which is the bug this fixes. The per-occurrence list lives
    on in `visual_elements`.
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    for mark in image_marks:
        name = Path(str(mark.get("src") or "")).name
        if name:
            by_name[name].append(mark)

    aligned: list[dict] = []
    for pic in embedded:
        marks = by_name.get(pic.name, [])
        chosen = next(
            (m for m in marks if m.get("trigger_seconds") is not None),
            marks[0] if marks else None,
        )
        if chosen is None:
            aligned.append({
                "src": pic.name, "xhtml": None, "chapter_id": None,
                "trigger_seconds": None,
                "after_beat_id": None, "before_beat_id": None,
            })
        else:
            aligned.append({
                "src": chosen.get("src"),
                "xhtml": chosen.get("xhtml"),
                "chapter_id": chosen.get("chapter_id"),
                "trigger_seconds": chosen.get("trigger_seconds"),
                "after_beat_id": chosen.get("after_beat_id"),
                "before_beat_id": chosen.get("before_beat_id"),
                **({"alt": chosen["alt"]} if chosen.get("alt") else {}),
            })
    return aligned


def _count_beat_spans(epub_path: Path) -> int:
    """Unique `data-beat-id` values actually present in a written EPUB."""
    found: set[str] = set()
    with zipfile.ZipFile(epub_path, "r") as z:
        for name in z.namelist():
            if name.lower().endswith((".xhtml", ".html", ".htm")):
                found.update(
                    re.findall(rb'data-beat-id="([^"]+)"', z.read(name))
                )
    return len(found)


def _write_modified_epub(
    orig_path: Path,
    modifications: dict[str, bytes],
    output_path: Path,
) -> None:
    """Copy the EPUB, overlaying the XHTML we modified.

    Every XHTML page also gets the responsive-containment style, including
    pages with no beats on them — a reader that scrolls sideways on the
    illustration pages is just as broken as one that does it mid-chapter.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(orig_path, "r") as src, zipfile.ZipFile(
        output_path, "w"
    ) as dst:
        # EPUB OCF: `mimetype` must be the FIRST entry and stored uncompressed.
        # Some publisher EPUBs get this wrong (Index v07-v09 carry it at entry
        # 21-26) and copying entries in source order passed the defect on.
        # ln-reader unzips everything and doesn't care, but stricter readers
        # and epubcheck reject the file, so write it first regardless.
        names = src.namelist()
        if "mimetype" in names:
            mt = zipfile.ZipInfo("mimetype", date_time=src.getinfo("mimetype").date_time)
            mt.compress_type = zipfile.ZIP_STORED
            dst.writestr(mt, src.read("mimetype"))
        for item in src.infolist():
            if item.filename == "mimetype":
                continue
            if item.filename.lower().endswith((".xhtml", ".html", ".htm")):
                base = modifications.get(item.filename) or src.read(item.filename)
                injected = _inject_responsive_css(base)
                new_item = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                new_item.compress_type = item.compress_type
                new_item.external_attr = item.external_attr
                dst.writestr(new_item, injected if injected is not None else base)
                continue
            if item.filename in modifications:
                # Preserve the original ZipInfo's compression to keep
                # mimetype STORED (uncompressed) per EPUB spec.
                new_item = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                new_item.compress_type = item.compress_type
                new_item.external_attr = item.external_attr
                dst.writestr(new_item, modifications[item.filename])
            else:
                dst.writestr(item, src.read(item.filename))


# ---- visual-element placement (lecture mode) --------------------------------


def place_visual_element(
    after_paragraph: int,
    seq: list[tuple[str, int, float, float]],
) -> tuple[str | None, str | None, float | None]:
    """Map a block's `after_paragraph` to neighbouring matched beats.

    `seq` is the chapter's matched beats in playback order, each
    ``(beat_id, source_paragraph, start_seconds, end_seconds)``. Returns
    ``(after_beat_id, before_beat_id, trigger_seconds)`` — the element appears
    after the last beat whose paragraph is ≤ `after_paragraph` and before the
    first beat whose paragraph is greater. `trigger_seconds` is that before-beat's
    start (or, for an end-of-chapter element, the after-beat's end). See §13.5.
    """
    after_beat = before_beat = None
    after_end = before_start = None
    for beat_id, sp, s_sec, e_sec in seq:
        if sp <= after_paragraph:
            after_beat, after_end = beat_id, e_sec
    for beat_id, sp, s_sec, e_sec in seq:
        if sp > after_paragraph:
            before_beat, before_start = beat_id, s_sec
            break
    trigger = before_start if before_start is not None else after_end
    return after_beat, before_beat, trigger


def place_at_anchor(
    anchor: dict,
    seq: list[tuple[str, int, float, float]],
    spans: dict[str, str],
) -> tuple[str | None, str | None, float | None] | None:
    """Place an element right after the beat that reads its anchor text.

    `anchor` is ``{"paragraph": p, "text": …}``: the words just before the
    element in paragraph p — a note's marker sat at their end. The beat of
    that paragraph whose span holds the anchor's tail (the last 40, 24 or
    12 characters, normalized) is the after-beat; the next beat in playback
    is the before-beat, and its start the trigger, as `place_visual_element`
    does for a whole paragraph. None when no beat holds the anchor, so the
    caller falls back to the paragraph.
    """
    paragraph = anchor.get("paragraph", -1)
    tail = _normalize_with_map(anchor.get("text", ""))[0].strip()
    if not tail:
        return None
    candidates = [
        (k, _normalize_with_map(spans.get(beat_id, ""))[0])
        for k, (beat_id, sp, _s, _e) in enumerate(seq)
        if sp == paragraph
    ]
    for length in (40, 24, 12):
        piece = tail[-length:]
        hit = next((k for k, span in candidates if piece in span), None)
        if hit is not None:
            after_beat, _sp, _s, after_end = seq[hit]
            if hit + 1 < len(seq):
                before_beat, _sp, before_start, _e = seq[hit + 1]
                return after_beat, before_beat, before_start
            return after_beat, None, after_end
        if len(tail) <= length:
            break
    return None


# ---- timing accumulation ----------------------------------------------------


# A chapter whose replayed length differs from the length s5 measured by more
# than this has had its renders changed since the mix. Rounding noise (manifest
# durations are kept to the millisecond) stays in the low milliseconds.
_MIX_DRIFT_TOLERANCE = 0.5


def _compute_beat_timings(
    audio_dir: Path,
    *,
    intra_silence: float,
    inter_scene_silence: float,
    inter_chapter_silence: float,
    lead_in_silence: float = 0.0,
    chapter_ids: list[str] | None = None,
    chapter_bounds: dict[str, tuple[float, float]] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, tuple[float, float]]:
    """beat_id → (start_seconds, end_seconds) in the final m4b time space.

    Mirrors Stage 5's layout: intra-scene between consecutive beats of one
    scene, inter-scene at scene boundaries, inter-chapter between chapters,
    and `lead_in_silence` before EVERY beat. A beat's start is where its
    lead-in starts, as in scenario-sync (§17.4) — the reader's ~0.3 s poll
    then lands before speech — and its end is where its speech ends.

    `chapter_ids` is the chapter list s5 actually mixed, in mix order (from
    its timings sidecar). None = every `<audio_dir>/<id>/manifest.json`,
    sorted — what `lnvox s5` mixes without `--chapters`.

    `chapter_bounds` (also from the sidecar) re-anchors each chapter at the
    start s5 measured, so a chapter re-rendered after the mix cannot shift
    every later chapter; `warn` is told about each such chapter.
    """
    say = warn or (lambda _m: None)
    if chapter_ids is None:
        chapter_ids = [
            p.name for p in sorted(audio_dir.iterdir())
            if p.is_dir() and (p / "manifest.json").exists()
        ]
    timings: dict[str, tuple[float, float]] = {}
    cursor = 0.0
    for ci, chapter_id in enumerate(chapter_ids):
        mani_path = audio_dir / chapter_id / "manifest.json"
        if not mani_path.exists():
            # Skipping it would silently shift every later chapter by its
            # length — the drift this layout replay exists to prevent.
            raise FileNotFoundError(
                f"{mani_path} is missing, but chapter {chapter_id} is part of "
                f"the mix. Re-render it (`lnvox s4`) and re-mix (`lnvox s5`)."
            )
        data = json.loads(mani_path.read_text(encoding="utf-8"))
        bounds = (chapter_bounds or {}).get(chapter_id)
        if bounds is not None:
            cursor = bounds[0]
        chapter_start = cursor
        prev_scene: str | None = None
        for i, beat in enumerate(data["beats"]):
            if i > 0:
                cursor += (
                    inter_scene_silence
                    if beat["scene_id"] != prev_scene
                    else intra_silence
                )
            start = cursor
            cursor += lead_in_silence + float(beat["duration_seconds"])
            timings[beat["beat_id"]] = (round(start, 3), round(cursor, 3))
            prev_scene = beat["scene_id"]
        if bounds is not None:
            drift = (cursor - chapter_start) - (bounds[1] - bounds[0])
            if abs(drift) > _MIX_DRIFT_TOLERANCE:
                say(
                    f"  [WARN] chapter {chapter_id}: its renders are {drift:+.1f} s "
                    f"off the mixed chapter — {audio_dir.name}/{chapter_id} changed "
                    f"after `lnvox s5`. Its beat times won't match the m4b; re-mix."
                )
        if ci < len(chapter_ids) - 1:
            cursor += inter_chapter_silence
    return timings


# ---- entry point ------------------------------------------------------------


# Pads used when neither the caller nor a timings sidecar supplies one — the
# `lnvox s5` defaults.
_DEFAULT_PADS = {"intra": 0.25, "inter_scene": 1.0, "inter_chapter": 2.0, "lead_in": 0.0}


def run(
    book_id: str,
    book_dir: Path,
    epub_path: Path,
    novel_dir: Path,
    output_dir: Path,
    *,
    intra_silence: float | None = None,
    inter_scene_silence: float | None = None,
    inter_chapter_silence: float | None = None,
    lead_in_silence: float | None = None,
    mix_title: str | None = None,
    progress: Callable[[str], None] = print,
) -> dict:
    """Run Stage 6. Returns the sync manifest (also written to disk).

    The timeline follows the Stage-5 mix recorded in
    `06_final/<mix_title>.timings.json` (default: the newest sidecar): its
    pads, lead-in, chapter list and embedded-picture order. A pad argument
    left None takes the recorded value; an explicit one overrides it (with a
    warning if they differ). With no sidecar at all, the `lnvox s5` defaults
    and every chapter in 05_audio/ are assumed — the pre-sidecar behaviour.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    from lnvox.stages.s5_mix import (
        embedded_image_order,
        load_mix_layout,
        resolve_book_images,
    )

    layout = load_mix_layout(book_dir / "06_final", mix_title)
    if layout is not None and layout.audio_dir not in (None, "05_audio"):
        # `lnvox s5 --v2`: VibeVoice session manifests have one entry per
        # multi-line session, so no beat could be timed against that m4b.
        raise ValueError(
            f"{layout.m4b.name} was mixed from {layout.audio_dir}/ (VibeVoice "
            f"sessions), which has no per-beat timing (DESIGN.md §16.10). "
            f"Pass --title of a mix made from 05_audio/."
        )
    recorded = dict(_DEFAULT_PADS) if layout is None else {
        "intra": layout.intra_silence,
        "inter_scene": layout.inter_scene_silence,
        "inter_chapter": layout.inter_chapter_silence,
        "lead_in": layout.lead_in_silence,
    }
    explicit = {
        "intra": intra_silence,
        "inter_scene": inter_scene_silence,
        "inter_chapter": inter_chapter_silence,
        "lead_in": lead_in_silence,
    }
    pads = {k: recorded[k] if v is None else float(v) for k, v in explicit.items()}
    if layout is None:
        progress(
            "No 06_final/*.timings.json — assuming the s5 defaults (or the "
            "given pads) and every chapter in 05_audio/."
        )
    else:
        progress(
            f"Following the mix in {layout.sidecar.name}: pads "
            f"{pads['intra']}/{pads['inter_scene']}/{pads['inter_chapter']} s, "
            f"lead-in {pads['lead_in']} s, {len(layout.chapter_ids)} chapter(s)."
        )
        for k in pads:
            if pads[k] != recorded[k]:
                progress(
                    f"  [WARN] {k} = {pads[k]} s overrides the {recorded[k]} s "
                    f"{layout.m4b.name} was mixed with — timings won't match it."
                )
    mixed_chapters = set(layout.chapter_ids) if layout and layout.chapter_ids else None

    epub_meta_path = novel_dir / ".epub_meta.json"
    if not epub_meta_path.exists():
        raise FileNotFoundError(
            f"Missing {epub_meta_path}; was this book imported via lnvox ingest-epub "
            "(or ingest-pdf, which also writes the EPUB to sync)?"
        )
    epub_meta = json.loads(epub_meta_path.read_text(encoding="utf-8"))

    # chapter_id (e.g. "01") → list of XHTML stems (e.g. ["chapter1", "chapter1_1"])
    chapter_stems: dict[str, list[str]] = {}
    for ch in epub_meta.get("chapters", []):
        chapter_id = (ch.get("file") or "").split("-", 1)[0]
        if chapter_id:
            chapter_stems[chapter_id] = ch.get("source_parts") or []

    # A `--split-chapters` ingest (DESIGN.md §18) records source parts as
    # `<stem>#<anchor>` because several chapters share one XHTML page. The OPF
    # manifest is keyed on the stem alone, so drop the anchor here; the shadow
    # is built from the whole page and the beat matcher narrows within it.
    key_to_chapter = {
        part: cid
        for cid, parts in chapter_stems.items()
        for part in parts
        if "#" in part
    }
    chapter_stems = {
        cid: list(dict.fromkeys(st.split("#", 1)[0] for st in parts))
        for cid, parts in chapter_stems.items()
    }

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
        intra_silence=pads["intra"],
        inter_scene_silence=pads["inter_scene"],
        inter_chapter_silence=pads["inter_chapter"],
        lead_in_silence=pads["lead_in"],
        chapter_ids=(layout.chapter_ids or None) if layout else None,
        # s5's measured chapter starts only describe a layout with THESE pads.
        chapter_bounds=(
            layout.chapter_bounds if layout and pads == recorded else None
        ),
        warn=progress,
    )
    progress(f"Computed timing for {len(timings)} beat(s) from {audio_dir}")

    # Directed beats with per-chapter ordering.
    directed_dir = book_dir / "03_directed"
    chapter_beats: dict[str, list[tuple[str, dict]]] = {}
    not_mixed: list[str] = []
    for path in sorted(directed_dir.glob("*.json")):
        directed = json.loads(path.read_text(encoding="utf-8"))
        cid = directed["chapter_id"]
        if mixed_chapters is not None and cid not in mixed_chapters:
            # Not in this m4b (`lnvox s5 --chapters`): its beats have no
            # place on the timeline, and entries at 0:00 would mislead.
            not_mixed.append(cid)
            continue
        ordered: list[tuple[str, dict]] = []
        for scene in directed["scenes"]:
            for i, beat in enumerate(scene["beats"]):
                beat_id = f"{scene['scene_id']}_b{i:04d}"
                ordered.append((beat_id, beat))
        chapter_beats[cid] = ordered
    if not_mixed:
        progress(
            f"Skipping {len(not_mixed)} directed chapter(s) not in "
            f"{layout.m4b.name}: {', '.join(not_mixed)}"
        )

    modifications: dict[str, bytes] = {}
    matched_beats: list[dict] = []
    unmatched: list[dict] = []
    image_marks: list[dict] = []
    # chapter_id → [(beat_id, source_paragraph, start_s, end_s)] in playback
    # order, for placing lecture-mode visual elements (DESIGN.md §13.5).
    chapter_matched_seq: dict[str, list[tuple[str, int, float, float]]] = {}
    beat_spans: dict[str, str] = {}  # beat_id → source_span, for anchored elements
    confidence_counts: dict[str, int] = {
        "span-exact": 0,
        "exact": 0,
        "anchored": 0,
        "head-only": 0,
        "tail-only": 0,
        "backtrack": 0,  # retired rung, kept for manifest compatibility
        "fuzzy": 0,
    }

    # One soup per XHTML file, SHARED across chapters. A `--split-chapters`
    # ingest (DESIGN.md §18) puts several chapters in one spine file, and each
    # chapter used to re-read that file from the zip and overwrite
    # `modifications[arcname]` — so only the last chapter's beat spans
    # survived. Measured on the demo run: Oz kept 102 of 1396 spans, Flatland
    # 23 of 687, while the one-chapter-per-file books were untouched. Chapters
    # cover disjoint regions of a shared file, so accumulating wraps into one
    # DOM is safe.
    soup_cache: dict[str, BeautifulSoup] = {}
    owners_cache: dict[str, dict[int, str]] = {}

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
            chapter_images: list[tuple[int, str, str, str]] = []  # (master_pos, src, arc, alt)
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
                soup = soup_cache.get(arcname)
                if soup is None:
                    soup = BeautifulSoup(zf.read(arcname), "html.parser")
                    soup_cache[arcname] = soup
                soup_for_arc[arcname] = soup
                local_shadow, local_segments, local_images = _build_shadow(soup)
                for seg in local_segments:
                    seg.shadow_start += master_cursor
                    seg.shadow_end += master_cursor
                    chapter_segments.append(seg)
                    arc_for_segments[id(seg.node)] = arcname
                owners = owners_cache.get(arcname)
                if owners is None:
                    owners = _image_owners(soup, stem, key_to_chapter)
                    owners_cache[arcname] = owners
                for im in local_images:
                    if owners and owners.get(id(im.node), chapter_id) != chapter_id:
                        continue  # another chapter's section of this page
                    alt = im.node.get("alt", "") if im.node is not None else ""
                    chapter_images.append(
                        (im.shadow_pos + master_cursor, im.src, arcname, alt)
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
                chapter_matched_seq.setdefault(chapter_id, []).append(
                    (beat_id, beat.get("source_paragraph", -1), timing[0], timing[1])
                )
                beat_spans[beat_id] = beat.get("source_span") or beat.get("text", "")

            # ---- Map INLINE images to neighbouring beats --------------------
            # Images embedded directly in the chapter text (rare for LN, but
            # possible). Spine-level insert pages are handled after the loop.
            # An image at shadow position P sits *after* the last beat whose
            # span ends at/before P, and *before* the first beat whose span
            # starts at/after P. The player triggers the image when it reaches
            # before_beat_id's start_seconds.
            if chapter_images:
                by_start = sorted(chapter_matches, key=lambda m: m.shadow_start)
                for pos, src, arcname, alt in chapter_images:
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
                    # A tailpiece — after the chapter's last matched beat — has
                    # no beat to precede, so show it as that beat ends. Left at
                    # None, ln-reader places it at 0:00, i.e. the start of the
                    # BOOK: 8 of Oz's chapter-end illustrations did exactly that
                    # once §23.2 stopped them borrowing other chapters' times.
                    if before_timing is None and after_beat:
                        before_timing = timings.get(after_beat, (None, None))[1]
                    image_marks.append({
                        "src": src,
                        "xhtml": arcname,
                        "chapter_id": chapter_id,
                        "after_beat_id": after_beat,
                        "before_beat_id": before_beat,
                        "trigger_seconds": before_timing,
                        **({"alt": alt} if alt else {}),
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

    # ---- Lecture-mode visual elements (code/table/figure/…) ----------------
    # Ingest (§13.2) recorded each non-prose block with the chapter-global
    # `after_paragraph` it follows. Map that to the surrounding matched beats
    # (which carry `source_paragraph`) so the reader can trigger the element at
    # the right playback offset — the paragraph-space analog of the inline-image
    # placement above.
    ingest_visual_elements: list[dict] = []
    for ve in epub_meta.get("visual_elements", []):
        cid = ve.get("chapter_id", "")
        after_p = ve.get("after_paragraph", -1)
        seq = chapter_matched_seq.get(cid, [])
        after_beat, before_beat, trigger = (
            place_at_anchor(ve["anchor"], seq, beat_spans) if ve.get("anchor") else None
        ) or place_visual_element(after_p, seq)
        ingest_visual_elements.append({
            "kind": ve.get("kind", ""),
            "src": ve.get("src", ""),
            "html": ve.get("html", ""),
            "language": ve.get("language", ""),
            "chapter_id": cid,
            "spine_page": ve.get("spine_page", ""),
            "after_beat_id": after_beat,
            "before_beat_id": before_beat,
            "trigger_seconds": trigger,
            # A note's plain text and number (`ingest-pdf`).
            **{k: ve[k] for k in ("text", "label") if ve.get(k)},
        })

    # Unified visual-elements list (§13.5): illustrations discovered by walking
    # the EPUB (kind="image") + ingest-extracted code/table/figure/etc.
    visual_elements = [{**im, "kind": im.get("kind", "image")} for im in image_marks]
    visual_elements += ingest_visual_elements

    # Manifest images must mirror the m4b's embedded pictures one-for-one, in
    # the order Stage 5 embedded them (see align_images_to_embeds). The
    # sidecar lists exactly that, `--cover` / `--images-dir` overrides
    # included; re-resolving (without them) is only the fallback for a
    # sidecar that predates the list, or no mix at all.
    if layout is not None and layout.embedded_images is not None:
        embedded = layout.embedded_images
    else:
        _cover, _extra = resolve_book_images(book_dir, novel_dir)
        embedded = embedded_image_order(_cover, _extra)
    aligned_images = align_images_to_embeds(image_marks, embedded)
    timed = sum(1 for im in aligned_images if im["trigger_seconds"] is not None)
    progress(
        f"Aligned {len(aligned_images)} manifest image(s) to the m4b's embedded "
        f"pictures ({timed} with a playback time)."
    )

    # Write the modified EPUB.
    safe_book_id = book_id.replace("/", "_")
    output_epub = output_dir / f"{safe_book_id}.epub"
    _write_modified_epub(epub_path, modifications, output_epub)

    # Verify what actually landed in the EPUB, not just what matched. Stage 6's
    # success metric is "beats matched", computed against the shadow text —
    # both defects in DESIGN.md §21 happened AFTER matching, so the stage
    # reported success while the reader had nothing to anchor to. Counting the
    # written spans closes that gap.
    written_spans = _count_beat_spans(output_epub)
    if matched_beats:
        ratio = written_spans / len(matched_beats)
        progress(
            f"Wrote {written_spans} beat span(s) into the EPUB "
            f"({ratio:.0%} of matched beats)."
        )
        # A shortfall is normal: a match that overlaps no segment in a given
        # page never wraps, and healthy books land at 88-97%. The failure this
        # guards is STRUCTURAL — §21.1 shipped Oz at 7% and Flatland at 3% —
        # so the threshold sits well below the normal band.
        if ratio < _SPAN_COVERAGE_FLOOR:
            progress(
                f"  [WARN] only {written_spans} of {len(matched_beats)} matched "
                f"beat(s) reached the EPUB. Reader auto-follow will not "
                f"highlight the rest — see DESIGN.md §21.1."
            )

    manifest = {
        "book_id": book_id,
        "epub": str(output_epub),
        # The 06_final/ m4b these timings describe (None: no sidecar found).
        # `lnvox sample` slices this file.
        "m4b": layout.m4b.name if layout is not None else None,
        "span_class": SPAN_CLASS,
        "data_attr": SPAN_ATTR,
        "total_beats": len(matched_beats) + len(unmatched),
        "matched": len(matched_beats),
        "unmatched": len(unmatched),
        "match_confidence": confidence_counts,
        "silences": {
            "intra_seconds": pads["intra"],
            "inter_scene_seconds": pads["inter_scene"],
            "inter_chapter_seconds": pads["inter_chapter"],
            "lead_in_seconds": pads["lead_in"],
        },
        "images": aligned_images,
        "visual_elements": visual_elements,
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
    ve_note = (
        f", {len(ingest_visual_elements)} non-prose element(s)"
        if ingest_visual_elements
        else ""
    )
    progress(
        f"Done. {len(matched_beats)}/{len(matched_beats) + len(unmatched)} "
        f"beats matched ({pct:.1f}%), {len(image_marks)} image(s) located"
        f"{ve_note}. EPUB: {output_epub}"
    )
    return manifest

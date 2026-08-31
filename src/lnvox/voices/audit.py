"""Casting audit: do characters who share scenes sound different? (§20)

The voice matcher casts each character independently on demographics, so
nothing in the pipeline notices when two people who talk to each other draw
near-identical voices. This module finds those pairs and, optionally, repairs
them — before Stage 4 turns the mistake into hours of rendered audio.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from lnvox.voices.acoustics import semitones_apart
from lnvox.voices.schema import BookCasting, Voicebank

NARRATOR_NAME = "Narrator"

# Defaults. `MIN_SHARED` keeps incidental pairs out of the report: in a
# 46-character book almost everyone co-occurs with someone once.
MIN_SEMITONES = 2.5
MIN_SHARED = 3

# Quality floor for a replacement clip — no point fixing a collision by
# introducing a muffled or noisy voice (§19.2). Two tiers: the second is used
# only when the first finds nothing, because a slightly duller voice the
# listener can TELL APART beats a pristine one they cannot.
_MIN_BANDWIDTH = 10000.0
_MIN_SNR = 38.0
_RELAXED_BANDWIDTH = 9000.0
_RELAXED_SNR = 32.0

_AGE_BANDS = ("teen", "young_adult", "adult", "elder")


def _adjacent_bands(band: str) -> set[str]:
    """The band itself plus its neighbours — an adult may be cast elder."""
    if band not in _AGE_BANDS:
        return {band}
    i = _AGE_BANDS.index(band)
    return set(_AGE_BANDS[max(0, i - 1): i + 2])


@dataclass(frozen=True)
class Collision:
    a: str
    b: str
    shared: int
    semitones: float
    same_clip: bool

    @property
    def kind(self) -> str:
        return "same clip" if self.same_clip else "too close"


@dataclass(frozen=True)
class Repair:
    character: str
    old_clip_id: str
    new_clip_id: str
    new_pitch: float


def scene_cooccurrence(scene_dir: Path) -> tuple[Counter, Counter]:
    """Return ``(beats_per_speaker, shared_scene_weight_per_pair)``.

    A pair's weight is ``min(beats_a, beats_b)`` summed over the scenes they
    both speak in — how much they actually converse, not merely whether they
    appear in the same book.
    """
    beats: Counter = Counter()
    pairs: Counter = Counter()
    for path in sorted(scene_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for scene in data.get("scenes", []):
            present: Counter = Counter()
            for beat in scene.get("beats", []):
                speaker = beat.get("speaker")
                if speaker:
                    present[speaker] += 1
            beats.update(present)
            names = sorted(present)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    pairs[(a, b)] += min(present[a], present[b])
    return beats, pairs


def find_collisions(
    casting: BookCasting,
    acoustics: dict[str, dict],
    pairs: Counter,
    *,
    min_semitones: float = MIN_SEMITONES,
    min_shared: int = MIN_SHARED,
) -> list[Collision]:
    """Scene-sharing pairs whose assigned voices are not separable."""
    assigned = {
        c.character_name: c.assigned_clip_id
        for c in casting.castings
        if c.assigned_clip_id
    }
    out: list[Collision] = []
    for (a, b), shared in pairs.items():
        if shared < min_shared:
            continue
        ca, cb = assigned.get(a), assigned.get(b)
        if not ca or not cb:
            continue
        if ca == cb:
            out.append(Collision(a, b, shared, 0.0, True))
            continue
        pa = (acoustics.get(ca) or {}).get("pitch", 0.0)
        pb = (acoustics.get(cb) or {}).get("pitch", 0.0)
        if not pa or not pb:
            continue  # unmeasurable; don't guess
        gap = semitones_apart(pa, pb)
        if gap < min_semitones:
            out.append(Collision(a, b, shared, gap, False))
    return sorted(out, key=lambda c: (-c.shared, c.semitones))


def _quality(metrics: dict) -> float:
    return (
        min(metrics.get("bandwidth", 0.0), 11000.0) / 1000.0 * 3.0
        + min(metrics.get("snr", 0.0), 50.0) * 0.6
        - min(metrics.get("hiss", 0.0), 1.0) * 15.0
    )


def repair_collisions(
    casting: BookCasting,
    voicebank: Voicebank,
    acoustics: dict[str, dict],
    beats: Counter,
    pairs: Counter,
    collisions: list[Collision],
    *,
    min_semitones: float = MIN_SEMITONES,
    min_shared: int = MIN_SHARED,
) -> tuple[list[Repair], list[Collision]]:
    """Reassign the lower-speaking side of each collision.

    Returns ``(applied, unfixed)``. `casting` is mutated in place for the
    repairs that succeed.
    """
    by_name = {c.character_name: c for c in casting.castings}
    clip_by_id = {c.id: c for c in voicebank.clips}
    used = {c.assigned_clip_id for c in casting.castings if c.assigned_clip_id}

    # Every scene partner of a character, for checking a candidate against all
    # of them rather than only the pair that triggered the flag. Uses the SAME
    # threshold as detection: requiring separation from everyone a character
    # ever shares a beat with is stricter than the rule being enforced, and in
    # a book where the Narrator co-occurs with all 33 speakers it shrinks the
    # candidate pool until nothing qualifies.
    partners: dict[str, set[str]] = defaultdict(set)
    for (a, b), shared in pairs.items():
        if shared >= min_shared:
            partners[a].add(b)
            partners[b].add(a)

    def _protected(name: str) -> bool:
        cst = by_name.get(name)
        return name == NARRATOR_NAME or bool(cst and cst.locked)

    def _current_gap(a: str, b: str) -> float | None:
        """Separation between two characters AS CURRENTLY ASSIGNED.

        Collisions were computed against the starting casting; each repair
        changes it. Without re-checking, a pair already fixed by an earlier
        repair gets "fixed" again (moving a character twice) or reported
        unfixed when it is fine.
        """
        ca = by_name[a].assigned_clip_id
        cb = by_name[b].assigned_clip_id
        if not ca or not cb:
            return None
        if ca == cb:
            return 0.0
        pa = (acoustics.get(ca) or {}).get("pitch", 0.0)
        pb = (acoustics.get(cb) or {}).get("pitch", 0.0)
        if not pa or not pb:
            return None
        return semitones_apart(pa, pb)

    applied: list[Repair] = []
    unfixed: list[Collision] = []
    # Clips each character has already held this run. Blocking a character
    # from moving twice outright stalls books where both sides of a late
    # collision were already moved; blocking only a RETURN to a previous clip
    # kills the A→B→A cycle while still allowing progress.
    history: dict[str, set[str]] = defaultdict(set)
    moved: set[str] = set()
    for col in collisions:
        gap = _current_gap(col.a, col.b)
        if gap is None or gap >= min_semitones:
            continue  # already resolved by an earlier repair

        # Move whichever side speaks less — cheaper re-render, and the
        # dominant voice is the one the listener has already learned. Never
        # move the same character twice: that thrashes, and the second move
        # is chosen against constraints the first already satisfied.
        # Prefer a character not yet moved (one re-render instead of two);
        # fall back to an already-moved one rather than giving up.
        order = sorted((col.a, col.b), key=lambda n: beats.get(n, 0))
        movable = [n for n in order if not _protected(n)]
        victim = next(
            (n for n in movable if n not in moved), movable[0] if movable else None
        )
        if victim is None:
            unfixed.append(col)
            continue

        cst = by_name[victim]
        original = clip_by_id.get(cst.assigned_clip_id)
        if original is None:
            unfixed.append(col)
            continue

        avoid = [
            (acoustics.get(by_name[p].assigned_clip_id) or {}).get("pitch", 0.0)
            for p in partners[victim]
            if p in by_name and by_name[p].assigned_clip_id
        ]
        avoid = [p for p in avoid if p]

        def _search(bands: set[str], min_bw: float, min_snr: float):
            out = []
            for clip in voicebank.clips:
                if clip.id in used or clip.gender != original.gender:
                    continue
                if clip.age_band not in bands:
                    continue
                m = acoustics.get(clip.id)
                if not m or not m.get("pitch"):
                    continue
                if m.get("bandwidth", 0.0) < min_bw or m.get("snr", 0.0) < min_snr:
                    continue
                if clip.id in history[victim]:
                    continue  # never send a character back to a clip it left
                if any(semitones_apart(m["pitch"], p) < min_semitones for p in avoid):
                    continue
                out.append((clip, m))
            return out

        # Degrade in the order that costs the least: exact demographics first,
        # then a neighbouring age band, then a lower quality floor. Female
        # adult voices in particular cluster tightly, so with a narrator and a
        # lead already placed the exact-band pool is often empty.
        exact = {original.age_band}
        near = _adjacent_bands(original.age_band)
        for bands, bw, snr, tier in (
            (exact, _MIN_BANDWIDTH, _MIN_SNR, ""),
            (near, _MIN_BANDWIDTH, _MIN_SNR, " (adjacent age band)"),
            (near, _RELAXED_BANDWIDTH, _RELAXED_SNR, " (adjacent age band, relaxed quality)"),
        ):
            candidates = _search(bands, bw, snr)
            if candidates:
                break

        if not candidates:
            unfixed.append(col)
            continue

        clip, metrics = max(candidates, key=lambda cm: _quality(cm[1]))
        other = col.b if victim == col.a else col.a
        old = cst.assigned_clip_id
        cst.assigned_clip_id = clip.id
        cst.audit_note = (
            f"voice audit: was {col.semitones:.1f} semitones from {other}, "
            f"who shares its scenes{tier}"
        )
        used.discard(old)
        used.add(clip.id)
        history[victim].add(old)
        history[victim].add(clip.id)
        moved.add(victim)
        applied.append(Repair(victim, old, clip.id, metrics["pitch"]))

    return applied, unfixed

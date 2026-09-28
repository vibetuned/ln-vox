"""Series / volume helpers.

Book IDs are hierarchical: `<series>/<volume-NN>`, e.g. `toaru/volume-02`.
The pipeline auto-detects earlier volumes in the same series so `voice cast`
can reuse their casting (recurring characters keep their voice).
"""

from __future__ import annotations

import re
from pathlib import Path


def book_path(artifacts_dir: Path, book_id: str) -> Path:
    """Return the artifacts directory for a book id (with slashes preserved)."""
    return artifacts_dir / book_id


def series_root(artifacts_dir: Path, book_id: str) -> Path | None:
    """Return the parent (series) directory if the book id is nested.

    For `toaru/volume-02` → `artifacts/toaru/`. For a flat id like
    `standalone-book` → None.
    """
    if "/" not in book_id:
        return None
    series = book_id.rsplit("/", 1)[0]
    return artifacts_dir / series


_VOLUME_NUMBER_RE = re.compile(r"^(.*?)(\d+)$")


def _split_volume(name: str) -> tuple[str, int] | None:
    """`volume-02` → ("volume-", 2); None when the name has no trailing number.

    The prefix is casefolded so `Volume-07` and `volume-07` are one series.
    """
    m = _VOLUME_NUMBER_RE.match(name)
    if m is None:
        return None
    return m.group(1).casefold(), int(m.group(2))


def find_prior_volumes(artifacts_dir: Path, book_id: str) -> list[Path]:
    """Return prior-volume artifact directories, ordered oldest → newest.

    Detection rule: sibling directories under the same series root whose
    leaf has the same prefix as the current leaf (case-insensitive) and a
    strictly smaller trailing number, ordered NUMERICALLY — `volume-9` comes
    before `volume-10`, unpadded and padded numbers mix freely, and stray
    dirs (`extras`, `covers`) are never mistaken for volumes. Hidden dirs
    (`.cache`, …) are always ignored. A leaf with no trailing number falls
    back to plain name ordering (siblings whose name sorts before it).

    Order matters: voice cast folds these in sequence and the newest wins.

    Example: `toaru/volume-02` returns [`artifacts/toaru/volume-01`].
    """
    sr = series_root(artifacts_dir, book_id)
    if sr is None or not sr.exists():
        return []
    leaf = book_id.rsplit("/", 1)[-1]
    siblings = [
        p for p in sr.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]
    current = _split_volume(leaf)
    if current is None:
        return sorted(p for p in siblings if p.name < leaf)
    prefix, number = current
    priors: list[tuple[int, str, Path]] = []
    for p in siblings:
        parsed = _split_volume(p.name)
        if parsed is not None and parsed[0] == prefix and parsed[1] < number:
            priors.append((parsed[1], p.name, p))  # name breaks number ties
    return [p for _, _, p in sorted(priors)]


def latest_prior_volume(artifacts_dir: Path, book_id: str) -> Path | None:
    """Return the most-recent prior volume directory, or None."""
    priors = find_prior_volumes(artifacts_dir, book_id)
    return priors[-1] if priors else None

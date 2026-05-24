"""Series / volume helpers.

Book IDs are hierarchical: `<series>/<volume-NN>`, e.g. `toaru/volume-02`.
The pipeline auto-detects sibling volumes in the same series so it can reuse
casting and merge character lists across volumes.
"""

from __future__ import annotations

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


def find_prior_volumes(artifacts_dir: Path, book_id: str) -> list[Path]:
    """Return prior-volume artifact directories, ordered oldest → newest.

    Detection rule: same series root, same volume-* prefix in the leaf name,
    leaf name sorts strictly before the current book's leaf.

    Example: `toaru/volume-02` returns [`artifacts/toaru/volume-01`].
    """
    sr = series_root(artifacts_dir, book_id)
    if sr is None or not sr.exists():
        return []
    leaf = book_id.rsplit("/", 1)[-1]
    siblings = sorted(
        p for p in sr.iterdir() if p.is_dir() and p.name < leaf
    )
    return siblings


def latest_prior_volume(artifacts_dir: Path, book_id: str) -> Path | None:
    """Return the most-recent prior volume directory, or None."""
    priors = find_prior_volumes(artifacts_dir, book_id)
    return priors[-1] if priors else None

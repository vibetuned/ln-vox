"""Tests for prior-volume detection (lnvox.series.find_prior_volumes).

Runnable directly (`python tests/test_series_priors.py`) or under pytest.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnvox.series import find_prior_volumes, latest_prior_volume


def _series(tmp: Path, *names: str, files: tuple[str, ...] = ()) -> Path:
    artifacts = tmp / "artifacts"
    for n in names:
        (artifacts / "toaru" / n).mkdir(parents=True)
    for f in files:
        (artifacts / "toaru" / f).write_text("x", encoding="utf-8")
    return artifacts


def _names(paths):
    return [p.name for p in paths]


def test_numeric_order_unpadded():
    # Regression: string comparison put volume-10 before volume-9 (so for
    # volume-10 the volumes 2..9 were NOT priors, and for volume-2 the tenth
    # volume WAS one).
    with tempfile.TemporaryDirectory() as t:
        a = _series(Path(t), "volume-1", "volume-2", "volume-9", "volume-10", "volume-11")
        assert _names(find_prior_volumes(a, "toaru/volume-10")) == [
            "volume-1", "volume-2", "volume-9"
        ]
        assert _names(find_prior_volumes(a, "toaru/volume-2")) == ["volume-1"]
        assert latest_prior_volume(a, "toaru/volume-11").name == "volume-10"


def test_padded_and_unpadded_mix():
    with tempfile.TemporaryDirectory() as t:
        a = _series(Path(t), "volume-01", "volume-2", "volume-03")
        assert _names(find_prior_volumes(a, "toaru/volume-03")) == ["volume-01", "volume-2"]
        # Same number, different padding: not strictly earlier.
        assert find_prior_volumes(a, "toaru/volume-1") == []


def test_non_volume_siblings_ignored():
    # Regression: every sibling dir whose name sorted before the leaf counted
    # as a prior volume, including stray and hidden directories.
    with tempfile.TemporaryDirectory() as t:
        a = _series(
            Path(t), "volume-01", "volume-02", "extras", "covers", "_shared",
            ".cache", ".volume-00", "vol-01", "volume-01-notes",
            files=("volume-00",),  # a plain file, not a dir
        )
        assert _names(find_prior_volumes(a, "toaru/volume-02")) == ["volume-01"]


def test_prefix_case_insensitive():
    with tempfile.TemporaryDirectory() as t:
        a = _series(Path(t), "Volume-01", "volume-02", "Volume-07")
        # Volume-07 is a later volume, not a prior (string order said prior).
        assert _names(find_prior_volumes(a, "toaru/volume-03")) == ["Volume-01", "volume-02"]
        assert _names(find_prior_volumes(a, "toaru/VOLUME-08")) == [
            "Volume-01", "volume-02", "Volume-07"
        ]


def test_leaf_without_number_falls_back_to_name_order():
    with tempfile.TemporaryDirectory() as t:
        a = _series(Path(t), "alpha", "beta", "gamma", ".hidden")
        assert _names(find_prior_volumes(a, "toaru/gamma")) == ["alpha", "beta"]


def test_flat_or_missing_series():
    with tempfile.TemporaryDirectory() as t:
        a = _series(Path(t), "volume-01")
        assert find_prior_volumes(a, "standalone-book") == []
        assert find_prior_volumes(a, "other/volume-02") == []
        assert latest_prior_volume(a, "toaru/volume-01") is None


def test_nested_series_root():
    with tempfile.TemporaryDirectory() as t:
        a = Path(t) / "artifacts"
        for n in ("book-1", "book-2"):
            (a / "pub" / "saga" / n).mkdir(parents=True)
        assert _names(find_prior_volumes(a, "pub/saga/book-2")) == ["book-1"]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

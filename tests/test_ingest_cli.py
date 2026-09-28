"""Stage 0 `lnvox ingest` — default book id (DESIGN.md §2.1).

Runnable directly (`python tests/test_ingest_cli.py`) or under pytest. All
chapter text is invented.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typer.testing import CliRunner

from lnvox.cli import app


def _ingest(cwd: Path, folder: str) -> None:
    """Run `lnvox ingest <folder>` (no --book-id) with `cwd` as the CWD."""
    prev_cwd, prev_env = os.getcwd(), os.environ.get("LNVOX_ARTIFACTS_DIR")
    os.chdir(cwd)
    os.environ["LNVOX_ARTIFACTS_DIR"] = "artifacts"
    try:
        res = CliRunner().invoke(app, ["ingest", folder])
        assert res.exit_code == 0, res.output
    finally:
        os.chdir(prev_cwd)
        if prev_env is None:
            os.environ.pop("LNVOX_ARTIFACTS_DIR", None)
        else:
            os.environ["LNVOX_ARTIFACTS_DIR"] = prev_env


def _chapter(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "01-gate.txt").write_text("The Gate\n\nA lantern swung.", encoding="utf-8")


def test_default_book_id_keeps_the_series_under_novels():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _chapter(tmp / "novels" / "series" / "volume-01")
        _ingest(tmp, "novels/series/volume-01")
        # Used to land in artifacts/volume-01, orphaned from its series.
        assert (tmp / "artifacts" / "series" / "volume-01" / "00_text.jsonl").exists()
        assert not (tmp / "artifacts" / "volume-01").exists()


def test_default_book_id_outside_novels_is_the_folder_name():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _chapter(tmp / "elsewhere" / "my-book")
        _ingest(tmp, "elsewhere/my-book")
        assert (tmp / "artifacts" / "my-book" / "00_text.jsonl").exists()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()

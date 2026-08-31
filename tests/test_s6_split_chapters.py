"""Stage 6 against a --split-chapters ingest (DESIGN.md §18 + §20 fallout).

When several chapters share ONE spine XHTML, Stage 6 used to re-read that file
from the zip for each chapter and overwrite its entry in `modifications`, so
only the last chapter's beat spans reached the synced EPUB. Shipped as: Oz 102
spans of 1396 matched beats, Flatland 23 of 687, reader auto-follow dead on
both. Books with one chapter per file were unaffected and hid the bug.

All prose below is invented — no real book content.
"""

import json
import zipfile
from pathlib import Path

from lnvox.stages import s6_sync

CH1 = "A lantern swung over the gate. Nobody answered the knock."
CH2 = "Dust rose behind the cart. The road bent north at the mill."

PAGE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
    f'<body><h2 id="c1">One</h2><p>{CH1}</p>'
    f'<h2 id="c2">Two</h2><p>{CH2}</p></body></html>'
).encode()

OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
    'version="3.0" unique-identifier="i"><metadata '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Invented</dc:title>'
    '</metadata><manifest>'
    '<item id="p1" href="book.xhtml" media-type="application/xhtml+xml"/>'
    '</manifest><spine><itemref idref="p1"/></spine></package>'
)
CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _build(tmp_path: Path):
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("content.opf", OPF)
        z.writestr("book.xhtml", PAGE)

    novel = tmp_path / "novel"
    novel.mkdir()
    # Both chapters point at the SAME stem — what --split-chapters produces.
    (novel / ".epub_meta.json").write_text(json.dumps({
        "title": "Invented", "authors": [], "images": [], "visual_elements": [],
        "chapters": [
            {"file": "01-one.txt", "title": "One", "source_parts": ["book#c1"]},
            {"file": "02-two.txt", "title": "Two", "source_parts": ["book#c2"]},
        ],
    }))

    book = tmp_path / "artifacts"
    directed = book / "03_directed"
    directed.mkdir(parents=True)
    audio = book / "05_audio"
    for cid, text in (("01", CH1), ("02", CH2)):
        (directed / f"{cid}.json").write_text(json.dumps({
            "chapter_id": cid,
            "scenes": [{"scene_id": f"{cid}_s1", "beats": [
                {"type": "narration", "text": text, "source_span": text,
                 "speaker": "Narrator", "direction": "d", "prompt": "p"},
            ]}],
        }))
        d = audio / cid
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"beats": [
            {"beat_id": f"{cid}_s1_b0000", "scene_id": f"{cid}_s1",
             "duration_seconds": 3.0},
        ]}))
    return epub, novel, book


def _spans(epub_path: Path) -> set[str]:
    import re
    out: set[str] = set()
    with zipfile.ZipFile(epub_path) as z:
        for n in z.namelist():
            if n.endswith(".xhtml"):
                out |= set(re.findall(rb'data-beat-id="([^"]+)"', z.read(n)))
    return {s.decode() for s in out}


def test_both_chapters_sharing_one_xhtml_keep_their_spans(tmp_path):
    epub, novel, book = _build(tmp_path)
    out = book / "07_sync"
    s6_sync.run("demo/x", book, epub, novel, out, progress=lambda _m: None)

    spans = _spans(out / "demo_x.epub")
    assert "01_s1_b0000" in spans, "first chapter's spans were overwritten"
    assert "02_s1_b0000" in spans, "second chapter's spans were overwritten"


def test_manifest_and_epub_agree(tmp_path):
    """The reported symptom: manifest full of beats, EPUB nearly empty."""
    epub, novel, book = _build(tmp_path)
    out = book / "07_sync"
    manifest = s6_sync.run("demo/x", book, epub, novel, out, progress=lambda _m: None)
    matched = {b["beat_id"] for b in manifest["beats"]}
    assert matched, "nothing matched; fixture is wrong"
    assert matched <= _spans(out / "demo_x.epub")


def test_stage_warns_when_spans_are_lost_while_writing(tmp_path, monkeypatch, capsys):
    """§21.3: the stage's own metric is 'beats matched', which stayed green
    through both shipped defects. It must now notice a write-side loss."""
    epub, novel, book = _build(tmp_path)
    out = book / "07_sync"

    # Simulate the old overwrite: drop every modification on the floor.
    real = s6_sync._write_modified_epub

    def lossy(orig, modifications, output):
        return real(orig, {}, output)

    monkeypatch.setattr(s6_sync, "_write_modified_epub", lossy)
    lines: list[str] = []
    s6_sync.run("demo/x", book, epub, novel, out, progress=lines.append)
    assert any("[WARN]" in ln and "reached the EPUB" in ln for ln in lines), lines


def test_no_warning_on_a_healthy_run(tmp_path):
    epub, novel, book = _build(tmp_path)
    out = book / "07_sync"
    lines: list[str] = []
    s6_sync.run("demo/x", book, epub, novel, out, progress=lines.append)
    assert not any("[WARN]" in ln for ln in lines), lines

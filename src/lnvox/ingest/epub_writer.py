"""Write a minimal EPUB 3 from chapter text — the sync target for books that
didn't come as an EPUB (`lnvox ingest-pdf`).

One XHTML per chapter, `<h1>` title + one `<p>` per paragraph: exactly what
`lnvox.ingest.epub` reads back, so Stage 6 re-aligns the beats onto the same
text they were rendered from. An optional cover gets its own spine page and
the EPUB 3 `cover-image` property. A nav document plus an NCX keeps EPUB 2
readers happy. Standard library only.
"""

from __future__ import annotations

import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

GENERATOR = "ln-vox"
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


@dataclass
class EpubChapter:
    stem: str  # XHTML file stem, e.g. "chapter01" — `.epub_meta.json` source_parts
    title: str
    paragraphs: list[str]


def _xhtml(title: str, body: str, lang: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang={quoteattr(lang)} xml:lang={quoteattr(lang)}>\n'
        f"<head><meta charset=\"utf-8\"/><title>{escape(title)}</title></head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def is_generated(epub_path: Path) -> bool:
    """True if `epub_path` was written by this module (safe to overwrite)."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return f'<meta name="generator" content="{GENERATOR}"/>' in opf


def write_epub(
    path: Path,
    *,
    title: str,
    authors: list[str],
    chapters: list[EpubChapter],
    cover: Path | None = None,
    language: str = "en",
) -> None:
    lang = language or "en"
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    spine = []
    files: dict[str, bytes | str] = {}

    if cover is not None:
        ext = cover.suffix.lower()
        cover_href = f"images/cover{ext}"
        files[f"OEBPS/{cover_href}"] = cover.read_bytes()
        manifest.append(
            f'<item id="cover-image" href="{cover_href}" media-type="{_MEDIA_TYPES[ext]}" '
            'properties="cover-image"/>'
        )
        manifest.append('<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="cover" linear="no"/>')
        files["OEBPS/text/cover.xhtml"] = _xhtml(
            title,
            f'<section epub:type="cover"><img src="../{cover_href}" alt={quoteattr(title)}/></section>',
            lang,
        )

    nav_items, ncx_points = [], []
    for i, ch in enumerate(chapters, start=1):
        href = f"text/{ch.stem}.xhtml"
        body = f"<h1>{escape(ch.title)}</h1>\n" + "\n".join(f"<p>{escape(p)}</p>" for p in ch.paragraphs)
        files[f"OEBPS/{href}"] = _xhtml(ch.title, body, lang)
        manifest.append(f'<item id="{ch.stem}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{ch.stem}"/>')
        nav_items.append(f'<li><a href="{href}">{escape(ch.title)}</a></li>')
        ncx_points.append(
            f'<navPoint id="np{i}" playOrder="{i}"><navLabel><text>{escape(ch.title)}</text>'
            f'</navLabel><content src="{href}"/></navPoint>'
        )

    creators = "\n    ".join(f"<dc:creator>{escape(a)}</dc:creator>" for a in authors)
    files["OEBPS/content.opf"] = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang={quoteattr(lang)}>
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{book_id}</dc:identifier>
    <dc:title>{escape(title)}</dc:title>
    {creators}
    <dc:language>{escape(lang)}</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
    <meta name="generator" content="{GENERATOR}"/>{'''
    <meta name="cover" content="cover-image"/>''' if cover is not None else ""}
  </metadata>
  <manifest>
    {chr(10).join("    " + m for m in manifest).strip()}
  </manifest>
  <spine toc="ncx">
    {chr(10).join("    " + s for s in spine).strip()}
  </spine>
</package>
"""
    files["OEBPS/nav.xhtml"] = _xhtml(
        title,
        f'<nav epub:type="toc" id="toc"><h1>{escape(title)}</h1><ol>\n'
        + "\n".join(nav_items)
        + "\n</ol></nav>",
        lang,
    )
    files["OEBPS/toc.ncx"] = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{book_id}"/></head>
  <docTitle><text>{escape(title)}</text></docTitle>
  <navMap>
    {chr(10).join("    " + p for p in ncx_points).strip()}
  </navMap>
</ncx>
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        # The mimetype entry must come first and be stored uncompressed.
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>\n'
            "</container>\n",
            compress_type=zipfile.ZIP_DEFLATED,
        )
        for name, data in files.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(path)

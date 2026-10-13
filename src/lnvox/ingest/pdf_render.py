"""Rasterize parts of PDF pages for `lnvox ingest-pdf`: the cover page, and
the figures, tables and notes a born-digital PDF draws as text and vectors.

pypdfium2 (and Pillow) are imported lazily and aren't locked dependencies:
run the command as `uv run --with "pypdf[image]" --with pypdfium2 …`. Every
coordinate here is PDF user space, as pypdf's text visitor reports it — y
grows upward from the page's bottom edge.
"""

from __future__ import annotations

import math
from pathlib import Path

RENDER_SCALE = 3.0  # 216 dpi: a figure's lettering and a note's text stay crisp
INK_THRESHOLD = 235  # grey level below which a pixel counts as ink when trimming
TRIM_PAD = 12  # px of white kept around the ink


def trim(img, pad: int = TRIM_PAD):
    """`img` cropped to its ink plus `pad` px of margin; None if it's blank."""
    mask = img.convert("L").point(lambda p: 255 if p < INK_THRESHOLD else 0)
    box = mask.getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    return img.crop(
        (max(0, left - pad), max(0, top - pad), min(img.width, right + pad), min(img.height, bottom + pad))
    )


def vstack(images: list):
    """Images one above the other on white, left-aligned (a table or a note
    continued over a page break)."""
    from PIL import Image

    if len(images) == 1:
        return images[0]
    out = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images)), "white")
    y = 0
    for i in images:
        out.paste(i, (0, y))
        y += i.height
    return out


class PageRenderer:
    """Renders the pages of one PDF on demand, one page held at a time."""

    def __init__(self, pdf_path: Path, scale: float = RENDER_SCALE):
        import pypdfium2  # ImportError → the caller degrades (no figures, no note images)

        self._doc = pypdfium2.PdfDocument(str(pdf_path))
        self.scale = scale
        self._page_no = 0
        self._image = None
        self._top = 0.0  # the crop box's top edge, user space

    def _page(self, page_no: int):
        if page_no != self._page_no:
            page = self._doc[page_no - 1]
            self._image = page.render(scale=self.scale).to_pil().convert("RGB")
            self._top = page.get_cropbox()[3]
            self._page_no = page_no
        return self._image

    def band(self, page_no: int, top: float, bottom: float):
        """The full-width strip of a page between two heights, trimmed to its
        ink; None if it holds none."""
        img = self._page(page_no)
        y0 = max(0, int((self._top - top) * self.scale))
        y1 = min(img.height, math.ceil((self._top - bottom) * self.scale))
        if y1 - y0 < 2:
            return None
        return trim(img.crop((0, y0, img.width, y1)))

    def close(self) -> None:
        self._image = None
        self._doc.close()


def render_page(pdf_path: Path, page_no: int, out: Path, max_px: int) -> None:
    """A whole page → JPEG with its long side at `max_px`: a cover drawn as
    layered images, vectors and text, not one picture."""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        page = doc[page_no - 1]
        width, height = page.get_size()
        img = page.render(scale=max_px / max(width, height)).to_pil().convert("RGB")
    finally:
        doc.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90)

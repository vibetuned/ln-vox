"""Render code / table blocks to PNG (DESIGN.md §13.2c).

Code is syntax-highlighted with Pygments; tables get a clean bordered CSS; the
wrapped HTML is rasterized to PNG with Playwright (headless Chromium).

Both heavy deps live behind the optional ``render`` extra. They are imported
lazily, so importing this module is always safe — `available()` reports whether
rendering can actually run, and `render_block()` returns False (degrading the
caller to an HTML-only visual element) when it can't.
"""

from __future__ import annotations

import html as _html
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the classifier at runtime just for a type
    from lnvox.ingest.blocks import ClassifiedBlock


_PAGE_CSS = """
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: #ffffff;
         font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  pre, code { font-family: "JetBrains Mono", Menlo, Consolas, monospace;
              font-size: 15px; line-height: 1.5; }
  pre { margin: 0; padding: 16px; border-radius: 8px; overflow: visible;
        white-space: pre-wrap; word-break: break-word; background: #f6f8fa; }
  table { border-collapse: collapse; width: 100%; font-size: 15px; }
  th, td { border: 1px solid #d0d7de; padding: 6px 12px; text-align: left;
           vertical-align: top; }
  thead th, tr:first-child th { background: #f6f8fa; font-weight: 600; }
  figure { margin: 0; } figcaption { font-size: 13px; color: #57606a; margin-top: 6px; }
"""


def available() -> bool:
    """True iff Playwright (the rasterizer) is importable."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def _code_text(block_html: str) -> str:
    """Pull the raw code text out of a ``<pre>``/``<code>`` block."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(block_html, "html.parser")
    el = soup.find("pre") or soup.find("code") or soup
    return el.get_text()


def _highlight_code(code: str, language: str = "") -> str:
    """Pygments-highlight `code` into a standalone (inline-styled) HTML snippet.

    Falls back to an escaped ``<pre>`` if Pygments isn't available or can't pick
    a lexer — the output is still a faithful monospace rendering.
    """
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.util import ClassNotFound

        lexer = None
        if language:
            try:
                lexer = get_lexer_by_name(language)
            except ClassNotFound:
                lexer = None
        if lexer is None:
            try:
                lexer = guess_lexer(code)
            except ClassNotFound:
                lexer = None
        if lexer is None:
            from pygments.lexers.special import TextLexer

            lexer = TextLexer()
        formatter = HtmlFormatter(noclasses=True, nowrap=False, style="default")
        return highlight(code, lexer, formatter)
    except Exception:
        return f"<pre>{_html.escape(code)}</pre>"


def build_block_html(block: "ClassifiedBlock") -> str:
    """Wrap a classified block into a complete, self-contained HTML document.

    Pure / side-effect-free — unit-testable without Playwright.
    """
    if block.kind == "code":
        code = _code_text(block.html) if block.html else block.text
        inner = _highlight_code(code, block.language)
    else:
        # table / figure / equation / footnote → keep the verbatim markup; the
        # page CSS styles it.
        inner = block.html or _html.escape(block.text)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_PAGE_CSS}</style></head><body>{inner}</body></html>"
    )


def render_html_to_png(
    html_doc: str, out_path: Path, *, width: int = 1080, scale: int = 2
) -> bool:
    """Rasterize a full HTML document to a PNG. Returns False if unavailable."""
    if not available():
        return False
    try:
        from playwright.sync_api import sync_playwright

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": 100},
                    device_scale_factor=scale,
                )
                page.set_content(html_doc, wait_until="networkidle")
                # Screenshot the <body> element directly so the PNG is cropped
                # tight to the content (a short snippet doesn't get padded out
                # to the full viewport height).
                body = page.query_selector("body")
                if body is not None:
                    body.screenshot(path=str(out_path))
                else:  # pragma: no cover - body always present for our docs
                    page.screenshot(path=str(out_path), full_page=True)
            finally:
                browser.close()
    except Exception:
        return False
    return out_path.exists()


def render_block(
    block: "ClassifiedBlock", out_path: Path, *, width: int = 1080, scale: int = 2
) -> bool:
    """Render one classified block to `out_path` (PNG). Returns success."""
    return render_html_to_png(
        build_block_html(block), out_path, width=width, scale=scale
    )

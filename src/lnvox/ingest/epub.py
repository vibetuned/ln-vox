"""EPUB → ln-vox novels-folder converter.

Reads an EPUB file and writes the layout consumed by `lnvox ingest`:

    <output_dir>/
        01-prologue.txt
        02-chapter-1.txt
        ...
        99-afterword.txt
        images/
            Cover.jpg
            Insert1.jpg
            ...
        .epub_meta.json    (title, authors, cover_image, image list, chapter map)

Multi-part chapters (e.g. `chapter1.xhtml` + `chapter1_1.xhtml`) are merged
into a single .txt by stripping the trailing `_N` suffix and grouping. Image-
only spine items (insert*, bonus*, color*, cover, TOCimg) are skipped from
text output but their referenced image still lands under `images/`.

The .epub_meta.json sidecar is consumed by Stage 0 ingest so the cover image
can later be embedded in the final m4b by Stage 5.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET


# XHTML spine items we skip entirely (the publisher's front/back matter and
# image-only pages — the image they reference still gets extracted).
_SKIP_STEM_PATTERNS = (
    re.compile(r"^cover", re.IGNORECASE),
    re.compile(r"^toc", re.IGNORECASE),
    re.compile(r"^tocimg", re.IGNORECASE),
    re.compile(r"^copyright", re.IGNORECASE),
    re.compile(r"^signup", re.IGNORECASE),
    re.compile(r"^insert\d+", re.IGNORECASE),
    re.compile(r"^bonus\d+", re.IGNORECASE),
    re.compile(r"^color\d+", re.IGNORECASE),
)

# Recognise chapter base names so chapter1.xhtml + chapter1_1.xhtml are merged.
_CHAPTER_BASE_RE = re.compile(
    r"^(chapter\d+|prologue|epilogue|afterword|interlude\d+|intro|preface)(?:_\d+)?$",
    re.IGNORECASE,
)


_OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}


@dataclass
class EpubMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    language: str = "en"
    cover_image: str = ""  # relative to output_dir
    images: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    # Lecture mode only (DESIGN.md §13.2): non-prose blocks (code/table/figure/
    # footnote/equation) pulled out of the narrated text and surfaced by Stage 6
    # as reader-side visual elements. Empty in narration mode.
    visual_elements: list[dict] = field(default_factory=list)


def _is_skip(stem: str) -> bool:
    return any(p.match(stem) for p in _SKIP_STEM_PATTERNS)


def _slugify(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:max_len] or "untitled"


def _chapter_base(stem: str) -> str:
    """`chapter1_1` → `chapter1`; everything else → itself, lowercased."""
    m = _CHAPTER_BASE_RE.match(stem)
    return m.group(1).lower() if m else stem.lower()


def _xhtml_to_text(xhtml_bytes: bytes) -> tuple[str, str]:
    """Return ``(title_from_first_h1_or_h2, plain_text)`` for an XHTML page.

    - <img> elements are dropped (we already extracted images separately).
    - Each <p> becomes one paragraph, separated by blank lines.
    - Section-break markers like "◆◆◆" are preserved as their own paragraph.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(xhtml_bytes, "html.parser")

    h1 = soup.find(["h1", "h2"])
    title = h1.get_text(" ", strip=True) if h1 else ""

    for img in soup.find_all("img"):
        img.decompose()

    paragraphs: list[str] = []
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text:
            paragraphs.append(text)

    return title, "\n\n".join(paragraphs)


def _xhtml_to_blocks(
    xhtml_bytes: bytes,
    *,
    classifier: str = "fallback",
    llm=None,
    render_template=None,
):
    """Lecture-mode page parse (DESIGN.md §13.2).

    Returns ``(title, paragraphs, blocks)`` where:
      - ``paragraphs`` is the narratable prose (one entry per paragraph), and
      - ``blocks`` is ``[(after_paragraph, ClassifiedBlock), …]`` for every
        non-prose block, ``after_paragraph`` = 0-indexed position of the prose
        paragraph the block follows within THIS page (-1 = before any prose).

    Returns ``None`` if the whole page is boilerplate to drop (TOC / copyright /
    index / …).
    """
    from bs4 import BeautifulSoup

    from lnvox.ingest.blocks import classify_page, page_drop_reason

    soup = BeautifulSoup(xhtml_bytes, "html.parser")
    h = soup.find(["h1", "h2"])
    stem_hint = (h.get_text(" ", strip=True) if h else "")[:40]
    if page_drop_reason(soup, stem_hint) is not None:
        return None

    title, classified = classify_page(
        soup, classifier=classifier, llm=llm, render_template=render_template
    )

    paragraphs: list[str] = []
    blocks: list[tuple[int, object]] = []
    for cb in classified:
        if cb.kind == "prose":
            if cb.text:
                paragraphs.append(cb.text)
            continue
        blocks.append((len(paragraphs) - 1, cb))
    return title, paragraphs, blocks


def _visual_element_for(
    block,
    *,
    chapter_id: str,
    after_paragraph: int,
    stem: str,
    images_dir: Path,
    image_basename_to_rel: dict[str, str],
    do_render: bool,
) -> dict:
    """Build one visual-element record (DESIGN.md §13.2d), rendering code/tables
    to PNG when possible. Falls back to HTML-only when rendering is unavailable.
    """
    el = {
        "kind": block.kind,
        "src": "",
        "html": block.html or "",
        "language": getattr(block, "language", "") or "",
        "chapter_id": chapter_id,
        "after_paragraph": after_paragraph,
        "spine_page": stem,
    }

    if block.kind == "figure":
        # Prefer pointing at the already-extracted illustration.
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(block.html or "", "html.parser")
        img = soup.find(["img", "image"])
        raw = (
            (img.get("src") or img.get("xlink:href") or img.get("href") or "")
            if img
            else ""
        )
        rel = image_basename_to_rel.get(Path(raw).name) if raw else None
        if rel:
            el["src"] = rel
            return el
        # Caption-only figure with no resolvable image → render it.

    if block.kind in ("code", "table", "figure") and do_render:
        from lnvox.ingest import render as render_mod

        digest = hashlib.sha256(
            (block.kind + (block.html or block.text or "") + el["language"]).encode(
                "utf-8", "ignore"
            )
        ).hexdigest()[:12]
        out_name = f"{block.kind}_{digest}.png"
        out_path = images_dir / out_name
        if out_path.exists() or render_mod.render_block(block, out_path):
            el["src"] = f"images/{out_name}"
    return el


def extract_epub(
    epub_path: Path,
    output_dir: Path,
    *,
    mode: str = "narration",
    classifier: str = "fallback",
    render: bool = True,
    llm=None,
    progress=print,
) -> EpubMeta:
    """Extract an EPUB into the ln-vox `novels/...` layout. Returns the metadata.

    ``mode="lecture"`` (DESIGN.md §13) classifies blocks, drops boilerplate
    pages, and pulls code/tables/figures out as visual elements (rendering
    code/tables to PNG when the ``render`` extra is installed). ``llm`` is an
    optional ``LLMClient`` for the §13.2a fallback classifier; ``classifier``
    is ``"fallback"`` or ``"none"``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    with zipfile.ZipFile(epub_path) as zf:
        # 1. Locate the OPF via META-INF/container.xml.
        try:
            container_bytes = zf.read("META-INF/container.xml")
        except KeyError as e:
            raise ValueError(
                f"Not a valid EPUB (missing META-INF/container.xml): {epub_path}"
            ) from e

        container_root = ET.fromstring(container_bytes)
        rootfile = container_root.find(".//c:rootfile", _CONTAINER_NS)
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path:
            raise ValueError(f"Could not locate OPF rootfile in {epub_path}")

        opf_dir = Path(opf_path).parent.as_posix()
        if opf_dir == ".":
            opf_dir = ""

        # 2. Parse the OPF.
        opf_root = ET.fromstring(zf.read(opf_path))
        meta = EpubMeta()

        title_elem = opf_root.find(".//dc:title", _OPF_NS)
        if title_elem is not None and title_elem.text:
            meta.title = title_elem.text.strip()
        for creator in opf_root.findall(".//dc:creator", _OPF_NS):
            if creator.text:
                meta.authors.append(creator.text.strip())
        pub_elem = opf_root.find(".//dc:publisher", _OPF_NS)
        if pub_elem is not None and pub_elem.text:
            meta.publisher = pub_elem.text.strip()
        lang_elem = opf_root.find(".//dc:language", _OPF_NS)
        if lang_elem is not None and lang_elem.text:
            meta.language = lang_elem.text.strip()

        # Cover hint via <meta name="cover" content="..."/> (EPUB 2 convention).
        cover_id_from_meta: str | None = None
        for meta_elem in opf_root.findall(".//opf:meta", _OPF_NS):
            if meta_elem.get("name") == "cover":
                cover_id_from_meta = meta_elem.get("content")

        # Manifest: id → (href, media_type, properties).
        manifest: dict[str, tuple[str, str, str]] = {}
        for item in opf_root.findall(".//opf:item", _OPF_NS):
            iid = item.get("id") or ""
            manifest[iid] = (
                item.get("href") or "",
                item.get("media-type") or "",
                item.get("properties") or "",
            )

        # Spine: ordered list of item ids.
        spine: list[str] = []
        spine_elem = opf_root.find(".//opf:spine", _OPF_NS)
        if spine_elem is not None:
            for itemref in spine_elem.findall("opf:itemref", _OPF_NS):
                idref = itemref.get("idref")
                if idref:
                    spine.append(idref)

        # 3a. Extract every image referenced in the manifest (raw files).
        image_rel_by_iid: dict[str, str] = {}
        for iid, (href, mt, props) in manifest.items():
            if not mt.startswith("image/"):
                continue
            src = (opf_dir + "/" if opf_dir else "") + href
            data = zf.read(unquote(src))
            dst = images_dir / Path(href).name
            dst.write_bytes(data)
            rel = f"images/{dst.name}"
            image_rel_by_iid[iid] = rel

            is_cover = (
                "cover-image" in props
                or iid == cover_id_from_meta
                or Path(href).stem.lower() == "cover"
            )
            if is_cover and not meta.cover_image:
                meta.cover_image = rel

        # 3b. Walk the spine in document-flow order so `meta.images` reflects
        # how the reader encounters them — not manifest declaration order
        # (which gives e.g. `Insert10.jpg` before `Insert2.jpg` after the
        # alphabetic sort that s5_mix used to do). For each spine page that
        # hosts <img> elements, append the referenced images in source order.
        # Non-spine images (rare; usually just background-only manifest items)
        # land at the end so nothing is silently lost.
        ordered_image_rels: list[str] = []
        seen_images: set[str] = set()

        def _emit(rel: str) -> None:
            if rel and rel not in seen_images:
                seen_images.add(rel)
                ordered_image_rels.append(rel)

        # If the OPF flags a cover image, it almost always belongs first even
        # when its spine page (`cover.xhtml`) is later in the file order.
        if meta.cover_image:
            _emit(meta.cover_image)

        # Build href → rel for quick lookup of image refs from XHTML spine pages.
        href_to_rel: dict[str, str] = {
            href: image_rel_by_iid[iid]
            for iid, (href, mt, _props) in manifest.items()
            if iid in image_rel_by_iid
        }

        for iid in spine:
            if iid not in manifest:
                continue
            href, mt, _props = manifest[iid]
            if "html" not in mt and "xml" not in mt:
                continue
            src = (opf_dir + "/" if opf_dir else "") + href
            try:
                page_bytes = zf.read(unquote(src))
            except KeyError:
                continue
            from bs4 import BeautifulSoup as _BS

            soup_page = _BS(page_bytes, "html.parser")
            page_dir = Path(href).parent.as_posix()
            for img in soup_page.find_all(["img", "image"]):
                raw_src = (
                    img.get("src")
                    or img.get("xlink:href")
                    or img.get("href")
                    or ""
                )
                if not raw_src:
                    continue
                # Resolve relative to the page's directory, then to opf_dir.
                resolved = (
                    f"{page_dir}/{raw_src}" if page_dir and not raw_src.startswith("/") else raw_src
                )
                # Normalize "./foo", "bar/../baz", etc.
                resolved = Path(resolved).as_posix()
                rel = href_to_rel.get(resolved)
                if rel is None:
                    # Try a stripped lookup (some EPUBs use ../Images/foo.jpg
                    # while the manifest href is Images/foo.jpg).
                    candidate = resolved.replace("../", "")
                    rel = href_to_rel.get(candidate)
                if rel is None:
                    # Last resort: match by basename. Loses precision when two
                    # images share a basename in different folders, but covers
                    # the common case of href-resolution drift.
                    base = Path(raw_src).name
                    for h, r in href_to_rel.items():
                        if Path(h).name == base:
                            rel = r
                            break
                if rel:
                    _emit(rel)

        # 3c. Any images that were in the manifest but appear in no spine
        # page (back-cover-only assets, etc.) go at the end so they're not lost.
        for iid in manifest:
            rel = image_rel_by_iid.get(iid)
            if rel:
                _emit(rel)

        meta.images = ordered_image_rels

        # 4. Walk the spine, collecting chapter parts grouped by base name.
        # Each part tuple: (spine_idx, title, text, stem, paragraphs, blocks).
        # paragraphs/blocks are None in narration mode; populated in lecture.
        chapter_groups: dict[str, list[tuple]] = {}
        chapter_order: list[str] = []
        for spine_idx, iid in enumerate(spine):
            if iid not in manifest:
                continue
            href, mt, props = manifest[iid]
            if "xhtml" not in mt and "html" not in mt:
                continue

            stem = Path(href).stem
            if _is_skip(stem):
                continue

            src = (opf_dir + "/" if opf_dir else "") + href
            xhtml = zf.read(unquote(src))

            if mode == "lecture":
                parsed = _xhtml_to_blocks(
                    xhtml,
                    classifier=classifier,
                    llm=llm,
                    render_template=(llm.render if llm is not None else None),
                )
                if parsed is None:
                    progress(f"  · drop {stem}: boilerplate / contents page")
                    continue
                title, paragraphs, blocks = parsed
                text = "\n\n".join(paragraphs)
                if not text.strip() and not blocks:
                    continue
            else:
                title, text = _xhtml_to_text(xhtml)
                paragraphs, blocks = None, None
                if not text.strip():
                    continue

            base = _chapter_base(stem)
            if base not in chapter_groups:
                chapter_groups[base] = []
                chapter_order.append(base)
            chapter_groups[base].append((spine_idx, title, text, stem, paragraphs, blocks))

    # 5. Write chapter .txt files in spine order.
    image_basename_to_rel = {Path(rel).name: rel for rel in meta.images}
    do_render = mode == "lecture" and render
    if mode == "lecture" and render:
        from lnvox.ingest import render as render_mod

        if not render_mod.available():
            do_render = False
            progress(
                "  [render] Playwright not installed — code/table blocks recorded "
                "as HTML-only visual elements (install the `render` extra to "
                "rasterize them)."
            )

    for i, base in enumerate(chapter_order, start=1):
        parts = chapter_groups[base]
        # Sort parts by spine index — handles weird manifest ordering.
        parts.sort(key=lambda p: p[0])
        # First non-empty title wins; fallback to humanised base.
        title = next((t for _, t, *_ in parts if t), base.replace("_", " ").title())
        chapter_id = f"{i:02d}"

        slug = _slugify(title)
        filename = f"{i:02d}-{slug}.txt"

        if mode == "lecture":
            # Concatenate parts' paragraphs into one chapter-global list so the
            # block `after_paragraph` indices align with `split_paragraphs` over
            # the written body (the lecture stage / Stage 6 share that coord).
            chapter_paragraphs: list[str] = []
            for _, _, _, stem, paragraphs, blocks in parts:
                base_count = len(chapter_paragraphs)
                chapter_paragraphs.extend(paragraphs or [])
                for local_after, block in blocks or []:
                    # local_after is 0-indexed within the part (-1 = before any
                    # of the part's paragraphs).
                    if local_after < 0:
                        after_global = base_count - 1
                    else:
                        after_global = base_count + local_after
                    meta.visual_elements.append(
                        _visual_element_for(
                            block,
                            chapter_id=chapter_id,
                            after_paragraph=after_global,
                            stem=stem,
                            images_dir=images_dir,
                            image_basename_to_rel=image_basename_to_rel,
                            do_render=do_render,
                        )
                    )
            body = "\n\n".join(chapter_paragraphs)
            char_count = len(body)
        else:
            body = "\n\n".join(text for _, _, text, *_ in parts)
            char_count = sum(len(p[2]) for p in parts)

        # Match the existing novels/ convention: first line is the title.
        (output_dir / filename).write_text(
            f"{title}\n\n{body}\n", encoding="utf-8"
        )
        meta.chapters.append({
            "file": filename,
            "title": title,
            "source_parts": [stem for _, _, _, stem, *_ in parts],
        })
        n_blocks = (
            sum(len(p[5] or []) for p in parts) if mode == "lecture" else 0
        )
        block_note = f", {n_blocks} visual block(s)" if n_blocks else ""
        progress(
            f"  {filename}  "
            f"({char_count:,} chars from {len(parts)} part(s){block_note})"
        )

    # 6. Write the metadata sidecar.
    (output_dir / ".epub_meta.json").write_text(
        json.dumps(
            {
                "title": meta.title,
                "authors": meta.authors,
                "publisher": meta.publisher,
                "language": meta.language,
                "cover_image": meta.cover_image,
                "images": meta.images,
                "chapters": meta.chapters,
                "visual_elements": meta.visual_elements,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ve_note = (
        f", {len(meta.visual_elements)} visual element(s)"
        if meta.visual_elements
        else ""
    )
    progress(
        f"Done. {len(meta.chapters)} chapter(s), {len(meta.images)} image(s)"
        f"{ve_note} → {output_dir}"
    )
    return meta

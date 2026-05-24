from pathlib import Path

from pydantic import BaseModel


class Chapter(BaseModel):
    chapter_id: str
    title: str
    text: str


def ingest_folder(folder: Path) -> list[Chapter]:
    """Read a folder of .txt files, one per chapter, sorted by filename.

    Filename convention: `NN-<slug>.txt`. The `NN` prefix becomes `chapter_id`
    and orders the chapters. The first non-empty line of each file is taken
    as the chapter title; the remainder is the body.
    """
    chapters: list[Chapter] = []
    for path in sorted(folder.glob("*.txt")):
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        first_nl = raw.find("\n")
        if first_nl == -1:
            title, body = raw, ""
        else:
            title, body = raw[:first_nl].strip(), raw[first_nl + 1 :].strip()
        chapter_id = path.stem.split("-", 1)[0]
        chapters.append(Chapter(chapter_id=chapter_id, title=title, text=body))
    return chapters


def write_jsonl(chapters: list[Chapter], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for ch in chapters:
            f.write(ch.model_dump_json() + "\n")


def read_jsonl(path: Path) -> list[Chapter]:
    chapters: list[Chapter] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            chapters.append(Chapter.model_validate_json(line))
    return chapters

"""Read-only text views over a book's artifacts, for the Inspect node.

Plain `json` only — no lnvox / pydantic import — so it runs inside
ComfyUI's interpreter. Field names follow src/lnvox/llm/schemas.py,
src/lnvox/voices/schema.py and src/lnvox/tts/schema.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

VIEWS = (
    "summary",
    "characters",
    "casting",
    "scenes",
    "directed",
    "audio",
    "final",
    "sync",
    "voicebank",
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _table(rows: list[list[str]], header: list[str]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in header]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = min(max(widths[i], len(c)), 60)

    def fmt(r):
        return "  ".join(c[: widths[i]].ljust(widths[i]) for i, c in enumerate(r)).rstrip()

    return "\n".join([fmt(header), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:.1f} MB"


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _voicebank(repo: Path, book: dict) -> dict:
    manifest = repo / (book.get("voicebank") or "voicebank") / "manifest.json"
    if not manifest.exists():
        return {}
    return {c["id"]: c for c in _load(manifest).get("clips", [])}


def summary(repo: Path, bdir: Path, book: dict, _filter: str) -> str:
    lines = [f"book: {book['book_id']}   mode: {book.get('mode')}   dir: {bdir}"]
    if not bdir.exists():
        return lines[0] + "\n\nNo artifacts yet — run an ingest node first."

    def row(label: str, path: Path, detail: Callable[[], str]) -> list[str]:
        if not path.exists():
            return [label, "·", path.name]
        try:
            return [label, "✓", detail()]
        except Exception as e:  # a half-written file shouldn't kill the view
            return [label, "!", f"{path.name}: {e}"]

    def count_dir(d: Path) -> int:
        return len(list(d.glob("*.json"))) if d.is_dir() else 0

    def beats_in(d: Path) -> int:
        return sum(len(s["beats"]) for p in d.glob("*.json") for s in _load(p).get("scenes", []))

    def audio_detail() -> str:
        root = bdir / "05_audio"
        n, secs = 0, 0.0
        for m in root.glob("*/manifest.json"):
            data = _load(m)
            n += len(data.get("beats", []))
            secs += float(data.get("total_duration_seconds", 0.0))
        return f"{n} beat wav(s), {_hms(secs)} rendered"

    def final_detail() -> str:
        m4bs = sorted((bdir / "06_final").glob("*.m4b"))
        return ", ".join(f"{p.name} ({_mb(p)})" for p in m4bs) or "no .m4b yet"

    def sync_detail() -> str:
        sm = bdir / "07_sync" / "sync_manifest.json"
        if sm.exists():
            d = _load(sm)
            pct = 100.0 * d.get("matched", 0) / max(1, d.get("total_beats", 0))
            return f"sync_manifest.json: {d.get('matched')}/{d.get('total_beats')} beats matched ({pct:.1f}%)"
        if (bdir / "07_sync" / "play.json").exists():
            return "play.json + play.srt (scenario timeline)"
        return "empty"

    rows = [
        row("00 text", bdir / "00_text.jsonl", lambda: f"{len(_jsonl(bdir / '00_text.jsonl'))} chapter(s)"),
        row("00 script", bdir / "00_script.json", lambda: f"{len(_load(bdir / '00_script.json').get('scenes', []))} scene(s)"),
        row("01 characters", bdir / "01_characters.json", lambda: f"{len(_load(bdir / '01_characters.json').get('characters', []))} character(s)"),
        row("02 scenes", bdir / "02_scenes", lambda: f"{count_dir(bdir / '02_scenes')} chapter file(s)"),
        row("04 casting", bdir / "04_voice_assignments.json", lambda: _casting_line(bdir)),
        row("03 profiles", bdir / "03_voice_profiles.json", lambda: "present"),
        row("03 directed", bdir / "03_directed", lambda: f"{count_dir(bdir / '03_directed')} chapter(s), {beats_in(bdir / '03_directed')} beat(s)"),
        row("05 audio", bdir / "05_audio", audio_detail),
        row("05 audio v2", bdir / "05_audio_v2", lambda: f"{len(list((bdir / '05_audio_v2').glob('*/manifest.json')))} chapter(s)"),
        row("06 final", bdir / "06_final", final_detail),
        row("07 sync", bdir / "07_sync", sync_detail),
    ]
    return "\n".join(lines) + "\n\n" + _table(rows, ["stage", "", "state"])


def _casting_line(bdir: Path) -> str:
    castings = _load(bdir / "04_voice_assignments.json").get("castings", [])
    assigned = sum(1 for c in castings if c.get("assigned_clip_id"))
    locked = sum(1 for c in castings if c.get("locked"))
    return f"{assigned}/{len(castings)} assigned" + (f", {locked} locked" if locked else "")


def characters(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    path = bdir / "01_characters.json"
    if not path.exists():
        return f"No {path.name} yet."
    chars = _load(path).get("characters", [])
    rows = [
        [c.get("name", ""), c.get("gender", ""), c.get("approx_age", ""), ", ".join(c.get("aliases", []))]
        for c in chars
        if _match(flt, c.get("name", ""), *c.get("aliases", []))
    ]
    return f"{len(rows)} character(s)\n\n" + _table(rows, ["name", "gender", "age", "aliases"])


def casting(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    path = bdir / "04_voice_assignments.json"
    if not path.exists():
        return f"No {path.name} yet — run Voice Cast."
    bank = _voicebank(repo, book)
    rows = []
    for c in _load(path).get("castings", []):
        if not _match(flt, c.get("character_name", "")):
            continue
        clip = bank.get(c.get("assigned_clip_id", ""), {})
        alts = [r.get("clip_id", "") for r in c.get("ranked", [])[1:3]]
        rows.append([
            c.get("character_name", ""),
            c.get("assigned_clip_id") or "(none)",
            f"{clip.get('gender', '?')}/{clip.get('age_band', '?')}/{clip.get('accent', '?')}" if clip else "",
            "yes" if c.get("locked") else "",
            ", ".join(a for a in alts if a),
        ])
    return (
        f"voicebank: {book.get('voicebank') or 'voicebank'}  ({len(bank)} clip(s))\n\n"
        + _table(rows, ["character", "clip", "clip gender/age/accent", "locked", "alternatives"])
        + "\n\nChange one with the 'ln-vox Set Voice' node, then re-run Casting Audit."
    )


def scenes(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    d = bdir / "02_scenes"
    if not d.is_dir():
        return "No 02_scenes/ yet — run Scenes (s2)."
    rows = []
    for p in sorted(d.glob("*.json")):
        data = _load(p)
        if not _match(flt, data.get("chapter_id", "")):
            continue
        beats = [b for s in data.get("scenes", []) for b in s.get("beats", [])]
        speakers = sorted({b.get("speaker") for b in beats if b.get("type") == "dialogue" and b.get("speaker")})
        rows.append([
            data.get("chapter_id", p.stem),
            str(len(data.get("scenes", []))),
            str(sum(1 for b in beats if b.get("type") == "narration")),
            str(sum(1 for b in beats if b.get("type") == "dialogue")),
            ", ".join(speakers),
        ])
    return _table(rows, ["chapter", "scenes", "narration", "dialogue", "speakers"])


def directed(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    d = bdir / "03_directed"
    if not d.is_dir():
        return "No 03_directed/ yet — run Director (s3), Lecture or Scenario Direction."
    out = []
    shown = 0
    for p in sorted(d.glob("*.json")):
        data = _load(p)
        for s in data.get("scenes", []):
            for b in s.get("beats", []):
                if flt and not _match(flt, data.get("chapter_id", ""), b.get("speaker", ""), s.get("scene_id", "")):
                    continue
                out.append(f"[{s.get('scene_id')}] {b.get('speaker')}: {b.get('prompt', '')}")
                shown += 1
                if shown >= 40:
                    out.append("… (first 40 shown — narrow with the filter: a chapter id, scene id or speaker)")
                    return "\n".join(out)
    return "\n".join(out) or "No beats matched the filter."


def audio(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    rows = []
    directed_counts = {}
    for p in (bdir / "03_directed").glob("*.json") if (bdir / "03_directed").is_dir() else []:
        data = _load(p)
        directed_counts[data["chapter_id"]] = sum(len(s.get("beats", [])) for s in data.get("scenes", []))
    for sub in ("05_audio", "05_audio_v2"):
        for m in sorted((bdir / sub).glob("*/manifest.json")):
            data = _load(m)
            cid = data.get("chapter_id", m.parent.name)
            if not _match(flt, cid):
                continue
            n = len(data.get("beats", []))
            cached = sum(1 for b in data.get("beats", []) if b.get("cached"))
            want = directed_counts.get(cid)
            rows.append([sub, cid, f"{n}" + (f"/{want}" if want is not None and sub == "05_audio" else ""),
                         str(cached), _hms(float(data.get("total_duration_seconds", 0.0)))])
    if not rows:
        return "No rendered audio yet — run TTS (s4)."
    return _table(rows, ["dir", "chapter", "beats", "from cache", "duration"])


def final(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    d = bdir / "06_final"
    m4bs = sorted(d.glob("*.m4b")) if d.is_dir() else []
    if not m4bs:
        return "No .m4b yet — run Mix (s5)."
    out = []
    for m4b in m4bs:
        out.append(f"{m4b}  ({_mb(m4b)})")
        timings = m4b.with_suffix(".timings.json")
        if timings.exists():
            t = _load(timings)
            out.append(
                f"duration {_hms(t.get('total_duration_seconds', 0))}, target {t.get('target_lufs')} LUFS, "
                f"silences intra/scene/chapter {t.get('intra_silence')}/{t.get('inter_scene_silence')}/"
                f"{t.get('inter_chapter_silence')} s, lead-in {t.get('lead_in_silence')} s"
            )
            rows = [[c.get("chapter_id", ""), _hms(c.get("start_seconds", 0)), _hms(c.get("duration_seconds", 0)), c.get("title", "")]
                    for c in t.get("chapters", [])]
            out.append(_table(rows, ["chapter", "start", "length", "title"]))
        out.append("")
    return "\n".join(out).rstrip()


def sync(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    d = bdir / "07_sync"
    sm = d / "sync_manifest.json"
    if sm.exists():
        m = _load(sm)
        pct = 100.0 * m.get("matched", 0) / max(1, m.get("total_beats", 0))
        hist = m.get("match_confidence", {})
        out = [
            f"{m.get('matched')}/{m.get('total_beats')} beats matched ({pct:.1f}%), {m.get('unmatched')} unmatched",
            f"synced EPUB: {m.get('epub')}",
            f"images placed: {len(m.get('images', []))}",
            "",
            _table([[k, str(v)] for k, v in hist.items()], ["match confidence", "beats"]),
        ]
        return "\n".join(out)
    play = d / "play.json"
    if play.exists():
        data = _load(play)
        return "Scenario timeline (play.json)\n\n" + json.dumps(
            {k: v for k, v in data.items() if not isinstance(v, list)}, indent=2, ensure_ascii=False
        )
    return "No 07_sync/ yet — run EPUB Sync (s6) or Scenario Sync."


def voicebank(repo: Path, bdir: Path, book: dict, flt: str) -> str:
    bank = _voicebank(repo, book)
    if not bank:
        return f"No manifest.json in {repo / (book.get('voicebank') or 'voicebank')}."
    rows = [
        [c["id"], c.get("gender", ""), c.get("age_band", ""), c.get("accent", ""), f"{c.get('duration_seconds', 0):.1f}s"]
        for c in bank.values()
        if _match(flt, c["id"], c.get("gender", ""), c.get("age_band", ""), c.get("accent", ""), all_tokens=True)
    ]
    head = f"{len(rows)}/{len(bank)} clip(s)" + (f" matching '{flt}'" if flt else "")
    shown = rows[:200]
    more = "" if len(rows) <= 200 else f"\n… {len(rows) - 200} more — narrow the filter (e.g. 'female adult england')"
    return head + "\n\n" + _table(shown, ["clip id", "gender", "age", "accent", "length"]) + more


def _match(flt: str, *fields: str, all_tokens: bool = False) -> bool:
    """Case-insensitive filter: any token hits any field (or ALL tokens hit)."""
    tokens = [t for t in (flt or "").lower().split() if t]
    if not tokens:
        return True
    hay = [f.lower() for f in fields if f]
    hits = [any(t == h or t in h for h in hay) for t in tokens]
    return all(hits) if all_tokens else any(hits)


RENDERERS = {
    "summary": summary,
    "characters": characters,
    "casting": casting,
    "scenes": scenes,
    "directed": directed,
    "audio": audio,
    "final": final,
    "sync": sync,
    "voicebank": voicebank,
}


def render(view: str, repo: Path, bdir: Path, book: dict, flt: str = "") -> str:
    return RENDERERS[view](repo, bdir, book, flt)

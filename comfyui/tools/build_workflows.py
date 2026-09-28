#!/usr/bin/env python3
"""Regenerate comfyui/example_workflows/*.json from compact specs.

Widget values in a saved ComfyUI workflow are positional, so hand-written
JSON silently drifts whenever a node gains an input. This builds each
workflow from the nodes' own INPUT_TYPES instead:

    python3 comfyui/tools/build_workflows.py            # UI workflows
    python3 comfyui/tools/build_workflows.py --api NAME # print API-format prompt

Runs with a plain python3 (no ComfyUI, no torch needed).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import uuid
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
OUT = PACK / "example_workflows"

WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN"}


def _load_nodes():
    spec = importlib.util.spec_from_file_location(
        "comfyui_lnvox", PACK / "__init__.py", submodule_search_locations=[str(PACK)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comfyui_lnvox"] = mod
    spec.loader.exec_module(mod)
    return mod.NODE_CLASS_MAPPINGS


NODES = _load_nodes()

# Core nodes the examples use: (link inputs, outputs, widget names).
CORE = {
    "PreviewAudio": {"inputs": [("audio", "AUDIO")], "outputs": [("AUDIO", "AUDIO")], "widgets": []},
    "Note": {"inputs": [], "outputs": [], "widgets": ["text"]},
}


def _signature(type_name: str):
    """(link inputs, outputs, ordered widget (name, default)) for a node type."""
    if type_name in CORE:
        c = CORE[type_name]
        return c["inputs"], c["outputs"], [(w, "") for w in c["widgets"]]
    cls = NODES[type_name]
    spec = cls.INPUT_TYPES()
    links, widgets = [], []
    for section in ("required", "optional"):
        for name, decl in spec.get(section, {}).items():
            kind = decl[0]
            opts = decl[1] if len(decl) > 1 else {}
            if isinstance(kind, list):
                widgets.append((name, opts.get("default", kind[0])))
            elif kind in WIDGET_TYPES:
                widgets.append((name, opts.get("default", "" if kind == "STRING" else 0)))
            else:
                links.append((name, kind))
    outputs = list(zip(cls.RETURN_NAMES, cls.RETURN_TYPES))
    return links, outputs, widgets


class Graph:
    def __init__(self):
        self.nodes: list[dict] = []
        self.links: list[list] = []
        self.groups: list[dict] = []

    def add(self, type_name: str, pos, *, node_mode: int = 0, title: str | None = None, size=None, **widgets):
        links, outputs, wdecl = _signature(type_name)
        unknown = set(widgets) - {w for w, _ in wdecl}
        if unknown:
            raise KeyError(f"{type_name}: unknown widget(s) {sorted(unknown)}")
        values = [widgets.get(name, default) for name, default in wdecl]
        node = {
            "id": len(self.nodes) + 1,
            "type": type_name,
            "pos": list(pos),
            "size": list(size or [340, 110 + 28 * len(wdecl) + (0 if type_name in CORE else 150)]),
            "flags": {},
            "order": 0,
            "mode": node_mode,  # 0 = always, 4 = bypassed
            "inputs": [{"name": n, "type": t, "link": None} for n, t in links],
            "outputs": [{"name": n, "type": t, "links": []} for n, t in outputs],
            "properties": {"Node name for S&R": type_name},
            "widgets_values": values,
        }
        if title:
            node["title"] = title
        self.nodes.append(node)
        return node

    def link(self, src: dict, dst: dict, out_name: str = "book", in_name: str = "book"):
        o = next(i for i, x in enumerate(src["outputs"]) if x["name"] == out_name)
        i = next(i for i, x in enumerate(dst["inputs"]) if x["name"] == in_name)
        lid = len(self.links) + 1
        ltype = src["outputs"][o]["type"]
        self.links.append([lid, src["id"], o, dst["id"], i, ltype])
        src["outputs"][o]["links"].append(lid)
        dst["inputs"][i]["link"] = lid
        return lid

    def chain(self, *nodes: dict):
        for a, b in zip(nodes, nodes[1:]):
            self.link(a, b)

    def group(self, title: str, nodes: list[dict], color: str):
        xs = [n["pos"][0] for n in nodes] + [n["pos"][0] + n["size"][0] for n in nodes]
        ys = [n["pos"][1] for n in nodes] + [n["pos"][1] + n["size"][1] for n in nodes]
        self.groups.append({
            "id": len(self.groups) + 1,
            "title": title,
            "bounding": [min(xs) - 20, min(ys) - 60, max(xs) - min(xs) + 40, max(ys) - min(ys) + 80],
            "color": color,
            "font_size": 24,
            "flags": {},
        })

    def _order(self):
        incoming = {n["id"]: {l[1] for l in self.links if l[3] == n["id"]} for n in self.nodes}
        done, order = set(), 0
        while len(done) < len(self.nodes):
            ready = [n for n in self.nodes if n["id"] not in done and incoming[n["id"]] <= done]
            for n in ready:
                n["order"] = order
                order += 1
                done.add(n["id"])

    def ui(self) -> dict:
        self._order()
        for n in self.nodes:
            for o in n["outputs"]:
                if not o["links"]:
                    o["links"] = None
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "lnvox:" + ",".join(n["type"] for n in self.nodes))),
            "revision": 0,
            "last_node_id": len(self.nodes),
            "last_link_id": len(self.links),
            "nodes": self.nodes,
            "links": self.links,
            "groups": self.groups,
            "config": {},
            "extra": {"ds": {"scale": 0.65, "offset": [40, 80]}},
            "version": 0.4,
        }

    def api(self) -> dict:
        """API-format prompt (for POST /prompt). Notes and bypassed nodes are dropped."""
        live = {n["id"]: n for n in self.nodes if n["type"] != "Note" and n["mode"] == 0}
        prompt = {}
        for nid, n in live.items():
            _, _, wdecl = _signature(n["type"])
            inputs = {name: val for (name, _), val in zip(wdecl, n["widgets_values"])}
            for inp in n["inputs"]:
                if inp["link"] is None:
                    continue
                link = next(l for l in self.links if l[0] == inp["link"])
                src = link[1]
                # Follow bypassed nodes back to a live producer of the same type.
                while src not in live:
                    src_node = next(x for x in self.nodes if x["id"] == src)
                    up = next(l for l in self.links if l[3] == src and l[5] == link[5])
                    src, link = up[1], up
                inputs[inp["name"]] = [str(src), link[2]]
            prompt[str(nid)] = {"class_type": n["type"], "inputs": inputs}
        return prompt


X, Y = 380, 560  # grid pitch


def narration() -> Graph:
    g = Graph()
    book = g.add("LnVoxBook", (0, 0), book_id="series/volume-01", mode="narration",
                 book_title="My Novel — Volume 1", epub="epubs/series/volume-01.epub")
    epub = g.add("LnVoxIngestEpub", (X, 0), node_mode=4)
    ingest = g.add("LnVoxIngest", (2 * X, 0))
    g.add("Note", (-X, 0), size=[340, 300], text=(
        "ln-vox — narration mode, stage by stage.\n\n"
        "1. Set book_id / title on the Book node.\n"
        "2. EPUB source? Un-bypass 'Ingest EPUB (0a)' (Ctrl+B). A folder of .txt chapters under "
        "novels/<book_id>/ needs nothing.\n"
        "3. Pick the narrator by ear first (see the hand-casting example) and paste its clip id "
        "into Voice Cast.\n\n"
        "Nodes cache their results: re-queueing does nothing. Bump a node's 'rerun' to run it and "
        "everything after it again."))
    llm = g.add("LnVoxLLMServer", (3 * X, 0), backend="llama")
    s1 = g.add("LnVoxCharacters", (4 * X, 0))
    s2 = g.add("LnVoxScenes", (5 * X, 0))
    cast = g.add("LnVoxVoiceCast", (0, Y + 140), narrator_clip="")
    audit = g.add("LnVoxVoiceAudit", (X, Y + 140), action="report")
    s3 = g.add("LnVoxDirector", (2 * X, Y + 140))
    stop = g.add("LnVoxStopLLMServer", (3 * X, Y + 140))
    tts = g.add("LnVoxTTS", (4 * X, Y + 140), backend="dramabox", staged=True, device="auto")
    mix = g.add("LnVoxMix", (5 * X, Y + 140), pacing="auto")
    sync = g.add("LnVoxSync", (6 * X, Y + 140))
    peek = g.add("LnVoxInspect", (2 * X, 2 * Y + 200), view="casting")
    summary = g.add("LnVoxInspect", (6 * X, 2 * Y + 200), view="summary")
    load = g.add("LnVoxLoadAudio", (5 * X, 2 * Y + 200), source="final_m4b", duration_seconds=90.0)
    preview = g.add("PreviewAudio", (5 * X, 2 * Y + 620), size=[340, 140])
    g.chain(book, epub, ingest, llm, s1, s2, cast, audit, s3, stop, tts, mix, sync, summary)
    g.link(audit, peek)
    g.link(mix, load)
    g.link(load, preview, "audio", "audio")
    g.group("Ingest", [book, epub, ingest], "#444444")
    g.group("LLM phase — Gemma 4 casts and directs", [llm, s1, s2], "#3f789e")
    g.group("Casting", [cast, audit, peek], "#8a5a1f")
    g.group("TTS phase — GPU handed to Dramabox", [stop, tts], "#6b3f9e")
    g.group("Output", [mix, sync, summary, load, preview], "#3f9e5a")
    return g


def lecture() -> Graph:
    g = Graph()
    book = g.add("LnVoxBook", (0, 0), book_id="nonfiction/book", mode="lecture",
                 book_title="Book Title — Subtitle", epub="epubs/nonfiction/book.epub")
    epub = g.add("LnVoxIngestEpub", (X, 0), ingest_classifier="none", render=True)
    ingest = g.add("LnVoxIngest", (2 * X, 0))
    llm = g.add("LnVoxLLMServer", (3 * X, 0), backend="llama")
    cast = g.add("LnVoxVoiceCast", (4 * X, 0), narrator_clip="cv_REPLACE_ME")
    lec = g.add("LnVoxLecture", (5 * X, 0), normalize=True)
    stop = g.add("LnVoxStopLLMServer", (0, Y))
    tts = g.add("LnVoxTTS", (X, Y), backend="dramabox", staged=True, device="auto")
    mix = g.add("LnVoxMix", (2 * X, Y), pacing="auto")
    sync = g.add("LnVoxSync", (3 * X, Y))
    summary = g.add("LnVoxInspect", (4 * X, Y), view="sync")
    g.add("Note", (5 * X, Y), size=[340, 220], text=(
        "ln-vox — lecture mode (single voice, verbatim).\n\n"
        "Ingest EPUB runs with the rules-only classifier here, so it needs no LLM; switch it to "
        "'fallback' only after the LLM Server node.\n\n"
        "Lecture with normalize=false and a narrator clip makes zero LLM calls: bypass the LLM "
        "Server and Stop nodes (Ctrl+B) for a GPU-light run."))
    g.chain(book, epub, ingest, llm, cast, lec, stop, tts, mix, sync, summary)
    return g


def scenario() -> Graph:
    g = Graph()
    book = g.add("LnVoxBook", (0, 0), book_id="myplay", mode="scenario", book_title="My Play",
                 scenario_file="scenarios/My Play.md", voicebank="voicebank-fr")
    llm = g.add("LnVoxLLMServer", (X, 0), backend="llama")
    ing = g.add("LnVoxIngestScenario", (2 * X, 0), language="fr")
    cast = g.add("LnVoxVoiceCast", (3 * X, 0))
    peek = g.add("LnVoxInspect", (3 * X, Y), view="casting")
    direc = g.add("LnVoxScenarioDirection", (4 * X, 0))
    stop = g.add("LnVoxStopLLMServer", (0, Y))
    tts = g.add("LnVoxTTS", (X, Y), backend="dramabox", staged=True, device="auto")
    mix = g.add("LnVoxMix", (2 * X, Y), pacing="auto")
    ysync = g.add("LnVoxScenarioSync", (4 * X, Y))
    view = g.add("LnVoxInspect", (5 * X, Y), view="sync")
    load = g.add("LnVoxLoadAudio", (2 * X, 2 * Y), source="final_m4b", duration_seconds=60.0)
    preview = g.add("PreviewAudio", (3 * X, 2 * Y), size=[340, 140])
    g.chain(book, llm, ing, cast, direc, stop, tts, mix, ysync, view)
    g.link(cast, peek)
    g.link(mix, load)
    g.link(load, preview, "audio", "audio")
    return g


def hand_casting() -> Graph:
    g = Graph()
    book = g.add("LnVoxBook", (0, 0), book_id="series/volume-01", mode="narration")
    bank = g.add("LnVoxInspect", (X, 0), view="voicebank", filter="female adult")
    clip = g.add("LnVoxLoadAudio", (2 * X, 0), source="voicebank_clip", item="cv_REPLACE_ME", duration_seconds=0.0)
    listen = g.add("PreviewAudio", (3 * X, 0), size=[340, 140])
    before = g.add("LnVoxInspect", (X, Y), view="casting")
    setv = g.add("LnVoxSetVoice", (2 * X, Y), assignments="Narrator = cv_REPLACE_ME\n# Other Character = cv_…", lock=True)
    audit = g.add("LnVoxVoiceAudit", (3 * X, Y), action="report")
    after = g.add("LnVoxInspect", (4 * X, Y), view="casting")
    g.add("Note", (0, Y), size=[340, 240], text=(
        "Hand casting — the --stop-after-audit loop, in ComfyUI.\n\n"
        "Run after Voice Cast + Casting Audit (e.g. the one-shot launcher with stop_after_audit) "
        "and BEFORE the Director (s3): descriptors are baked from the casting there.\n\n"
        "Browse the bank (filter: gender / age / accent words), audition a clip, write "
        "'Name = clip_id' lines into Set Voice, and re-check with the audit."))
    g.link(book, bank)
    g.link(book, clip)
    g.link(clip, listen, "audio", "audio")
    g.link(book, before)
    g.chain(book, setv, audit, after)
    return g


def one_shot() -> Graph:
    g = Graph()
    book = g.add("LnVoxBook", (0, 0), book_id="series/volume-01", mode="narration", book_title="My Novel — Volume 1")
    run = g.add("LnVoxRunPipeline", (X, 0), llm_backend="llama", tts_backend="dramabox")
    summary = g.add("LnVoxInspect", (2 * X, 0), view="summary")
    load = g.add("LnVoxLoadAudio", (2 * X, Y), source="final_m4b", duration_seconds=90.0)
    preview = g.add("PreviewAudio", (3 * X, Y), size=[340, 140])
    g.chain(book, run, summary)
    g.link(run, load)
    g.link(load, preview, "audio", "audio")
    return g


WORKFLOWS = {
    "ln-vox narration (stage by stage)": narration,
    "ln-vox lecture": lecture,
    "ln-vox scenario": scenario,
    "ln-vox hand casting": hand_casting,
    "ln-vox one-shot launcher": one_shot,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", metavar="NAME", help="print the API-format prompt for one workflow")
    args = ap.parse_args()
    if args.api:
        print(json.dumps(WORKFLOWS[args.api]().api(), indent=2, ensure_ascii=False))
        return
    OUT.mkdir(exist_ok=True)
    for name, build in WORKFLOWS.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(build().ui(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(PACK.parent)}")


if __name__ == "__main__":
    main()

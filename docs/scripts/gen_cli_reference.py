#!/usr/bin/env python3
"""Regenerate docs/src/content/docs/reference/cli.md from the Typer app.

Run from the repo root, in the ln-vox venv:

    uv run --no-sync python docs/scripts/gen_cli_reference.py
"""
import inspect
from pathlib import Path
import click, typer
from lnvox.cli import app

LINKS = {
    "ingest": "/stages/ingest/", "ingest-epub": "/stages/ingest-epub/", "ingest-scenario": "/stages/ingest-scenario/",
    "s1": "/stages/s1-characters/", "s2": "/stages/s2-scenes/", "s3": "/stages/s3-director/",
    "lecture": "/stages/lecture/", "scenario": "/stages/scenario/", "scenario-sync": "/stages/scenario-sync/",
    "s4": "/stages/s4-tts/", "s4-phase": "/library/tts/staged/", "s5": "/stages/s5-mix/", "s6": "/stages/s6-sync/",
    "sample": "/stages/utilities/", "audio purge": "/stages/utilities/",
    "voice seed-cv": "/stages/voicebank/", "voice list": "/stages/voicebank/",
    "voice cast": "/stages/voice-cast/", "voice audit": "/stages/voice-audit/",
}
ORDER = ["ingest-epub", "ingest", "ingest-scenario", "s1", "s2", "voice", "s3", "lecture", "scenario",
         "s4", "s4-phase", "s5", "s6", "scenario-sync", "sample", "audio"]

root = typer.main.get_command(app)
out = []
def esc(s): return (s or "").replace("|", "\\|").replace("\n", " ").strip()

def default_of(p):
    if p.required: return "required"
    d = p.default
    if callable(d): d = d()
    if isinstance(p, click.Option) and p.is_flag:
        return "off" if not d else "on"
    if d is None or d == (): return "—"
    if isinstance(d, (list, tuple)): d = ", ".join(map(str, d))
    return f"`{d}`"

def emit(name, cmd):
    out.append(f"### `lnvox {name}`" + (" (hidden)" if cmd.hidden else ""))
    out.append("")
    doc = inspect.cleandoc(cmd.help or "")
    first = doc.split("\n\n")[0].replace("\n", " ")
    link = LINKS.get(name)
    out.append(first + (f" See [{link.strip('/').split('/')[-1]}]({link})." if link else ""))
    out.append("")
    args = [p for p in cmd.params if isinstance(p, click.Argument)]
    opts = [p for p in cmd.params if isinstance(p, click.Option) and p.name != "help"]
    usage = " ".join(f"<{a.name.upper()}>" for a in args)
    out.append("```bash"); out.append(f"uv run lnvox {name} {usage} [OPTIONS]".rstrip()); out.append("```"); out.append("")
    if args or opts:
        out.append("| Argument / option | Default | Meaning |"); out.append("| --- | --- | --- |")
        for a in args:
            out.append(f"| `{a.name.upper()}` | {default_of(a)} | {esc(getattr(a, 'help', '') or '')} |")
        for o in opts:
            names = ", ".join(f"`{n}`" for n in o.opts + o.secondary_opts)
            meta = "" if o.is_flag else f" `{o.type.name.upper()}`" if o.type.name not in ("text",) else " `TEXT`"
            if o.multiple: meta += " (repeatable)"
            out.append(f"| {names}{meta} | {default_of(o)} | {esc(o.help)} |")
        out.append("")

SUB_ORDER = ["seed-cv", "list", "cast", "audit", "purge"]
names = sorted(root.commands, key=lambda n: ORDER.index(n) if n in ORDER else 99)
out.append("## Stage commands"); out.append("")
for n in names:
    if not isinstance(root.commands[n], click.Group):
        emit(n, root.commands[n])
for n in names:
    grp = root.commands[n]
    if isinstance(grp, click.Group):
        out.append(f"## `lnvox {n}` — {esc(grp.help)}"); out.append("")
        for sub in sorted(grp.commands, key=lambda x: SUB_ORDER.index(x) if x in SUB_ORDER else 99):
            emit(f"{n} {sub}", grp.commands[sub])
HEADER = """---
title: CLI reference
description: Every lnvox command and option, generated from the Typer app in src/lnvox/cli.py — arguments, defaults and help text.
sidebar:
  order: 1
---

Every stage of ln-vox is a subcommand of the `lnvox` CLI (`src/lnvox/cli.py`, entry point `lnvox = "lnvox.cli:app"`). Run them from the repository root with `uv run lnvox …` so relative paths (`artifacts/`, `novels/`, `voicebank/`, `cache/`) resolve the way the launcher expects. [`scripts/run_pipeline.sh`](/launcher/run-pipeline/) chains them for you; the pages under **Pipeline stages** explain each one in depth.

This page is generated from the Typer app itself, so option names, types and defaults are exact. `BOOK_ID` is always `<series>/<volume-NN>` or a flat id, and resolves to `artifacts/<BOOK_ID>/` (see [concepts](/start/concepts/)).

:::tip[Global behaviour]
- The LLM connection for every LLM-calling command comes from [`lnvox.config.Settings`](/library/config/) — `LNVOX_LLM__ENDPOINT`, `LNVOX_LLM_MODEL`, `LNVOX_LLM_MAX_MODEL_LEN`, … (see [environment variables](/reference/environment/)).
- Every command that reads the voicebank uses `$LNVOX_VOICEBANK` (default `voicebank`).
- `uv run lnvox --help` and `uv run lnvox <command> --help` print the same information.
:::

"""

body = "\n".join(out)
out_path = Path(__file__).resolve().parents[1] / "src/content/docs/reference/cli.md"
out_path.write_text(HEADER + body + "\n", encoding="utf-8")
print(f"wrote {out_path}")

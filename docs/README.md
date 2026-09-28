# ln-vox documentation site

The user and API documentation for ln-vox, built with
[Astro Starlight](https://starlight.astro.build).

```bash
cd docs
npm install
npm run dev        # http://localhost:4321
npm run build      # static site in dist/
```

## Layout

Pages are Markdown under `src/content/docs/`; the sidebar is generated per
directory (see `astro.config.mjs`) and ordered by each page's `sidebar.order`.

| Directory | Contents |
| --- | --- |
| `start/` | Introduction, installation, quickstart, concepts |
| `guides/` | Narration / lecture / scenario modes, series, casting, Studio, Apple Silicon, render quality, ComfyUI, troubleshooting |
| `launcher/` | `scripts/run_pipeline.sh`: options and flow, every shell function, the helper scripts |
| `stages/` | One page per pipeline stage: command, launcher call, inputs/outputs, how it works |
| `library/` | One page per `lnvox` module: API reference |
| `reference/` | CLI reference, environment variables, artifact layout |

`reference/cli.md` is generated from the Typer app; regenerate it when a
command's options change rather than editing the tables by hand.

Internal links are absolute (`/library/llm/client/`). After editing, run a
build: a broken slug shows up as a 404 in `dist/`.

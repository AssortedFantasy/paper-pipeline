# Paper Pipeline

Local tooling that turns a [Zotero](https://www.zotero.org/) RDF export and its attached PDFs into a clean, file-based paper database — ready to drop into any LLM-assisted writing workflow.

## Why?

Writing papers with LLMs in an IDE works best when the model can actually read your references.
PDFs are heavy and opaque; what you really want is a folder of neatly transcribed Markdown files that any agent or context window can ingest instantly.

Paper Pipeline bridges the gap:

1. **Export from Zotero** → Zotero RDF with attached PDFs.
2. **Run the pipeline** → per-paper metadata, Nougat transcriptions, and status tracking.
3. **Use the output** → copy `papers/` into your project, feed papers to LLMs, or build further workflows on top.

The web dashboard gives you a visual overview, lets you launch transcriptions, and shows real-time progress.

## Output structure

```
papers/
  <citekey>/
    meta.md            # title, authors, date, abstract, etc.
    transcribed.md     # full Nougat transcription (Markdown + LaTeX math)
    intro_filtered.md  # GPT-5 mini intro/background extraction from the paper PDF
    method_filtered.md # GPT-5 mini methodology extraction from the paper PDF
    status.json        # lightweight run status
    nougat_raw/        # raw .mmd output and logs
```

## Quick start

```sh
uv sync
uv run paper-pipeline doctor   # check that Nougat + GPU are ready
```

### Build the paper database

Place your Zotero RDF export (and the accompanying `files/` directory) in the workspace root, then:

```sh
uv run paper-pipeline build
```

This creates `papers/<citekey>/meta.md` and a placeholder `transcribed.md` for every paper in the export.

### Run transcriptions

Transcribe one paper:

```sh
uv run paper-pipeline run-nougat --citekey <citekey>
```

Or launch the web dashboard and drive everything from the browser:

```sh
uv run paper-gui
```

The GUI starts at `http://127.0.0.1:8787` by default.

### Run the LLM workflow

Set your API key before launching the GUI:

```powershell
$env:OPENAI_API_KEY = "..."
```

Or in `cmd.exe`:

```cmd
set OPENAI_API_KEY=...
```

The dashboard has an `LLM` model field. Its default value is `gpt-5-mini-2025-08-07`.
If you want a persistent server-side default, set:

```powershell
$env:PAPER_PIPELINE_LLM_MODEL = "gpt-5-mini-2025-08-07"
```

Then open the dashboard, select one or more papers, and use `Run Intro Filter` or `Run Method Filter`.
Each action uploads the selected paper PDF to OpenAI, sends the matching prompt file from the workspace root, and writes the result into the paper folder as `intro_filtered.md` or `method_filtered.md`.

The current implementation runs one LLM request at a time and retries transient upload/generation failures with bounded exponential backoff. Each response also includes a stable `prompt_cache_key` derived from the paper PDF so repeated runs for the same paper can benefit from OpenAI prompt caching when the backend can reuse cached prefixes.

### Useful flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--workspace` | `.` | Workspace root |
| `--rdf` | auto-detect first `*.rdf` | Path to Zotero RDF export |
| `--output` | `papers` | Output directory |
| `--max-pages` | `50` | Skip PDFs longer than this |
| `--max-size-mb` | `40` | Skip PDFs larger than this |
| `--dry-run` | — | Print commands without running Nougat |

## How it works

1. Parses the Zotero RDF export to extract paper metadata and attachment paths.
2. Creates per-paper folders under `papers/`.
3. Runs [Nougat](https://github.com/facebookresearch/nougat) as a subprocess to transcribe each PDF.
4. Stores raw output, logs, and status for recovery and inspection.
5. Serves a local web dashboard (FastAPI + htmx + Alpine.js) for browsing papers, launching Nougat jobs, and running prompt-based LLM filters against selected PDFs.

Nougat runs are serialised with a workspace lock to prevent overlapping GPU jobs.

## Future directions

Planned extensions include:

- **Richer metadata** — manifest-based state model, re-import reconciliation, self-contained database.
- **More LLM workflows** — additional prompt templates, structured outputs, and reusable workflow presets.
- **Better UI** — detail panels, PDF preview, sorting/filtering, copy-to-clipboard.

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design notes.

## Requirements

- Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/)
- CUDA-capable GPU (for Nougat transcription)
- `OPENAI_API_KEY` set when using the PDF-based LLM workflow
- Optional: `PAPER_PIPELINE_LLM_MODEL` to change the server-side default LLM model
- A Zotero RDF export (`File → Export Library → Zotero RDF`, with "Export Files" checked)

## License

[MIT](LICENSE)

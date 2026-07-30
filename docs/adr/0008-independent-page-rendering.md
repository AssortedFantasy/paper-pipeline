# ADR-0008: Independent local page rendering

Status: Accepted

## Context

Marker conversion produces Markdown and extracted figures that the Markdown
references. Paper Pipeline also rendered every source-PDF page to a compact PNG
inside the Marker adapter. Page rendering did not consume Marker output, but a
rendering failure failed the transcription attempt, used the conversion
timeout, and made remote conversion transfer every page image over SSH.

Pages are useful filesystem reading artifacts, but the dashboard, recipes, and
indexes do not require them.

## Decision

Transcription conversion owns only `transcription.md` and `figures/`. These
artifacts remain one atomic bundle because the Markdown can reference the
extracted figures.

Page images are produced by a separate local PDFium renderer:

- each attempt runs in a fresh child process;
- every mutation uses the existing shared job system and paper lane;
- at most two page-render jobs run concurrently;
- the renderer reads a hash-verified snapshot of the locally owned source PDF;
- `pages/` is validated and atomically replaced without touching transcription
  or figures;
- `paper.json` records the source hash, renderer, DPI, page count, artifact
  paths and hashes, completion time, and latest attempt;
- failed rerenders preserve the last valid page set; and
- remote SSH conversion neither renders nor transports pages.

Page rendering is explicit rather than automatically chained to conversion.
This avoids introducing workflow dependencies and lets users choose whether
large page-image sets are useful.

Existing unrecorded `pages/` directories remain permitted as legacy artifacts.
They are preserved by transcription reruns and become fully tracked after an
explicit page-render job.

The library format remains version 2. The new `pages` record is additive with a
default empty value, fixed artifact paths are unchanged, and existing format-2
records and page directories remain readable. This compatibility decision was
reviewed alongside the serialized model change.

## Consequences

A valid transcription no longer depends on page rendering. Remote conversion
has a smaller output contract and avoids page-image transfer. Pages have their
own freshness, integrity, retry, and pending-selection behavior.

The core installation now includes PDFium and Pillow, while Marker and GPU
dependencies remain optional.

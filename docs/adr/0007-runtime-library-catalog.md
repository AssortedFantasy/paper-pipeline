# ADR-0007: Runtime library catalog

Status: Accepted

## Context

The filesystem is the canonical paper-reading interface and `paper.json` is
the durable record for each paper. The dashboard initially rebuilt its Papers
view directly from that storage on every request. Sorting or filtering 70
papers therefore parsed every record three times, hashed installed artifacts,
and reopened every source PDF to count pages. A presentation-only interaction
took about 1.5 seconds even though the actual in-memory sort was negligible.

A durable database would make reads fast but would duplicate library truth and
violate the portable folder model. Client-owned paper state would also conflict
with the server-authoritative htmx design.

## Decision

Each `LibraryRuntime` owns one in-memory `LibraryCatalog`. The catalog is an
immutable, atomically replaceable projection containing:

- parsed `PaperRecord` values;
- verified conversion and recipe freshness for presentation;
- source-PDF page counts and large-document classification;
- refresh generation, timestamp, and library problems.

Interactive paper listing, searching, filtering, and sorting query this
prepared snapshot. They do not scan the library, hash artifacts, or inspect
PDFs.

Every canonical `PaperSession.write_record()` updates the affected catalog
entry after the atomic `paper.json` write succeeds. This central hook covers
imports, conversion results, recipe results, retries, and future paper-lane
mutations. Live queued and running job state remains in the job queue and is
overlaid on catalog entries; it is not stored in the catalog.

Paper Pipeline cannot observe every out-of-process filesystem mutation.
The Papers view therefore provides an explicit **Refresh** operation. Refresh
runs one library read, builds a complete replacement snapshot, and preserves
the user's current filters and sort. A throttled stat-based fingerprint can
warn that paper records or known source PDFs changed on disk without turning
ordinary UI requests back into full validation.

PDF page counts are cached in `.pp/catalog-cache.json`, keyed by source
identity and file stat signature. This file is disposable acceleration only:
it is never artifact truth, may be missing or malformed, and may be deleted
without affecting the library. Cache writes are best effort and cannot fail a
canonical paper write.

Full validation remains an explicit library operation. The catalog is a fast
read model, not proof that every artifact is currently intact.

## Consequences

- Papers interactions are bounded by in-memory query and template-rendering
  cost rather than library size times filesystem inspection cost.
- Normal Paper Pipeline writes appear without a manual refresh.
- Changes made by another process become visible after Refresh; likely changes
  can be indicated cheaply before then.
- Opening an uncached library performs one complete projection build. Later
  process starts can reuse disposable PDF inspection results.
- The catalog and its cache do not change the versioned library format.
- Tests must establish that interactive queries do not call raw
  `Library.list_papers()` or `pdf_page_count()`.

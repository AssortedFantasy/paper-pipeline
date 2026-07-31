# ADR-0009: Durable Batch recipe orchestration

Status: Accepted

## Context

The original recipe path created one paper-lane job per paper and performed
several immediate Responses API calls sequentially while holding that lane. A
later request failure made the paper job fail after earlier requests had
already spent tokens, combined unrelated recipe diagnostics into one log, and
could not be recovered after process restart. Uploaded PDF IDs were cached only
in process memory and had no reliable deletion policy.

A 15-paper, four-recipe experiment established that one OpenAI Batch containing
60 Responses requests completed successfully while preserving per-paper prompt
cache reuse. The PDF can be uploaded once and referenced by every recipe for
that paper.

## Decision

One user queue action creates logical recipe runs across all compatible papers
and recipes, partitioned only by the provider's 50,000-request Batch limit.
Oversized PDFs and request files are rejected explicitly; Paper Pipeline does
not attempt to predict the provider's queued-token accounting. The paper is the
unit of input caching and artifact installation, not normally the unit of
remote Batch submission.

Recipe execution has three phases:

1. Brief paper-lane work resolves, hashes, and snapshots each declared input.
2. A remote-scope coordinator uploads each distinct PDF once, submits and polls
   the Batch without holding paper lanes, downloads output and error files, and
   maps every line by `custom_id`.
3. Per-paper finalizers reacquire paper lanes, reject stale results, and
   independently install every valid recipe output atomically.

Resumable operational state lives under `.pp/recipe-runs/<run-id>/`. Its
manifest is the request mapping; state records provider file and Batch IDs,
downloaded outcomes, finalization progress, and cleanup work. It is disposable
recovery data, never artifact truth. Unreadable run directories are discarded.
Provider resource IDs never enter `paper.json`.

OpenAI PDF requests place the uploaded PDF first, mark an explicit cache
breakpoint after it, put the recipe prompt second, use explicit-only 30-minute
prompt caching, derive the cache key from the input hash, and set `store=false`.
Uploads receive a 48-hour safety expiry and are deleted after terminal results
are durably collected. Cleanup failures are warnings and are retried separately
from recipe outcomes.

Successful recipe requests install independently when sibling requests fail,
expire, or are cancelled. A partial run is a first-class terminal job state.
Retrying a partial run creates a fresh Batch containing only failed requests;
local collection or installation recovery never repeats a paid request.

Batch creation is treated as non-idempotent. Submission intent is persisted
before the create call and automatic SDK retries are disabled for that call.
When a crash leaves submission ambiguous, Paper Pipeline reconciles recent
Batches by the persisted input file ID and proceeds only when exactly one match
exists. It never automatically submits a replacement when acceptance is
uncertain.

Human-readable diagnostics consist of one run summary and one immutable log per
paper/recipe attempt. Raw prompts, paper contents, credentials, and provider
error bodies are excluded from logs.

## Consequences

- Remote waiting no longer blocks imports, conversion, page rendering, or
  other mutations of the affected papers.
- Process restarts can resume submitted work without a second database.
- One failed recipe no longer discards successful paid siblings.
- Batch-only execution removes the old immediate-call provider and queue path;
  no transition implementation is retained.
- Closing or shutting down the application detaches from remote work rather
  than cancelling it. Explicit user cancellation stops remaining remote
  requests, retains already installed artifacts, and discards uninstalled
  operational results.
- Provider Batch lifecycle, partial results, stale installation, cleanup, and
  ambiguous submission require dedicated failure-injection tests.

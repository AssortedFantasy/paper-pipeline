# ADR-0006: LLM usage and spend accounting

Status: Accepted

## Context

Recipe provenance needs enough provider usage information to inspect cost and
prompt-cache effectiveness without maintaining a separate accounting store.

## Decision

Provider results report input, cached-input, cache-write, and output tokens
when supplied by the provider. The OpenAI adapter computes cost from a reviewed
model price table. Batch results apply the documented Batch discount. Unknown
pricing fails the affected result visibly instead of recording a false zero
cost. Paper Pipeline has no immediate Responses execution path.

Installed recipe records persist token counts and computed cost in
`paper.json`. The dashboard totals those records rather than storing separate
spend data.

For models that support explicit prompt caching, requests use a stable key
derived from the paper input hash and place the cache boundary after the paper
content. Recipes for the same paper can therefore reuse the common input
prefix.

GPT-5.6 Batch requests use explicit-only caching so changing recipe suffixes do
not create avoidable cache writes. One uploaded PDF file ID is reused by every
recipe request for that input inside its remote Batch (ADR-0009).

Provider payloads, credentials, and API endpoints are not stored in usage
records or logs.

## Consequences

Usage remains inspectable after restart and without the application running.
Adding a model requires an explicit pricing rule and tests. Changes to provider
usage fields, pricing, or caching behavior must update the adapter and its
contract tests together.

# ADR-0006: LLM usage and spend accounting

Status: Accepted (2026-07-15)

## Context

Recipe runs previously exposed neither token use nor cost, so operators could
not see spend or determine whether same-paper sequential execution produced
prompt-cache hits. GPT-5.6 also bills cache writes at 1.25 times ordinary input
while cache reads cost 10 percent of ordinary input, so treating all input as
one rate misstates spend.

## Decision

1. `ProviderResult` reports prompt, cached-read, cache-write, and completion
   tokens plus computed USD cost. `RecipeRecord` persists those values with the
   installed artifact.
2. The OpenAI adapter reads Responses API `usage.input_tokens`,
   `usage.input_tokens_details.cached_tokens`,
   `usage.input_tokens_details.cache_write_tokens`, and `usage.output_tokens`.
3. Cost uses a reviewed standard-processing price table in the adapter. For
   GPT-5.6, only provider-reported cache writes use the 1.25x write rate;
   ordinary uncached input uses the input rate and cached input uses the
   90-percent discounted read rate. Its greater-than-272K-token input and output
   multipliers are also applied. Unknown model pricing fails visibly; it never
   records a plausible-looking zero.
4. Recipe job logs show per-recipe and batch totals plus cache-hit rate. The
   dashboard sums durable recipe records; it does not create a second spend
   store.
5. GPT-5.6 requests use a stable, SHA-256-derived `prompt_cache_key` for each
   paper input and place an explicit cache breakpoint immediately after the
   transcription or PDF block. The recipe prompt follows the breakpoint, so
   different recipes share the exact reusable paper prefix. The request uses
   the model family's 30-minute cache TTL.
6. Marker 1.10.2 declares OpenAI 1.x for Marker-owned LLM services. Paper
   Pipeline disables those services (`use_llm=False`), overrides that stale
   package constraint, and owns the OpenAI 2.45+ recipe integration. Both a
   real Marker conversion and a real first-write/second-read cache test gate
   this decision.

The pricing and cache behavior were checked against the official
[GPT-5.6 announcement](https://openai.com/index/gpt-5-6/) and
[prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
on 2026-07-15. Pricing changes require updating the table and its contract
tests together.

## Consequences

- Usage and spend survive restarts in `paper.json` and remain inspectable
  without the application.
- Cache effectiveness can be measured across real batches before changing the
  required sequential per-paper scheduling policy.
- An unrecognized model cannot run until its pricing rule is deliberately
  added, avoiding inaccurate spend records.

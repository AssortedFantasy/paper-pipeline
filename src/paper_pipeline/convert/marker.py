"""Marker conversion backend adapter.

Implemented by WP-2B.3 (see PLAN.md), gated on the WP-2B.0 quality/runtime
spike. Translates ``ConversionRequest`` into a Marker invocation and
normalizes Marker's output (markdown file, extracted images, metadata JSON)
into a ``ConversionResult``. All Marker-specific flags and quirks live here.

Requires the ``marker`` extra; must only be imported in the conversion child
process.
"""

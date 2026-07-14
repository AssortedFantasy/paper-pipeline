"""Child-process isolation for converters.

Implemented by WP-2B.2 (see PLAN.md). Runs a converter in a fresh child
process per paper so GPU memory and backend crashes are reclaimed at paper
boundaries. Handles timeout, cancellation (kill process tree), exit-code
mapping, and stdout/stderr capture into diagnostics.

The child process entry point is the only place heavy backend imports
(marker, torch) may occur.
"""

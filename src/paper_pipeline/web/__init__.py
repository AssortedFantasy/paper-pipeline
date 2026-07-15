"""Web API and client: an operations dashboard with lightweight inspection.

Approach (ADR-0001): server-rendered Jinja2 templates progressively enhanced
with htmx; job progress streams over Server-Sent Events. No SPA framework,
no client-side paper/job database.

Rules:

- Routes translate HTTP <-> service calls; no business logic in routes,
  templates, or JavaScript.
- Server-owned state is never duplicated as independent client truth; the
  client owns only ephemeral interaction state (selections, open panels).
- Inline DOM manipulation outside htmx/SSE swaps is prohibited.
- Important views have stable URLs: ``/``, ``/papers``, ``/papers/{citekey}``,
  ``/jobs``, ``/import``.
- Empty, loading, error, disconnected, cancelled, and interrupted states are
  designed, not accidental.
- Closing the browser must not affect server-side jobs.

Modules: ``app`` (FastAPI factory), ``api`` (JSON + fragment routes), and
``templates/`` and ``static/`` (client).
"""

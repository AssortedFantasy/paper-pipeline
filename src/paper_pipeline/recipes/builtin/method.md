---
name: method
version: 2
input: pdf
output: method_filtered.md
---
Summarize how the work in this paper was carried out: the approach, its
components, and how it was evaluated or validated.

Rules:

- Write strictly in the third person. Omit claims of novelty,
  significance, or impact, and strip promotional language.
- Where the authors coin their own terminology for standard concepts,
  describe the underlying mechanism in established, field-standard terms;
  note the authors' name for it once, then use the standard description.
- Report every concrete setting, quantity, resource, and measurement the
  paper provides about how the work was done and assessed.
- Prefer precise mechanics over high-level narrative. Keep the level of
  detail a reader would need to assess or reproduce the work.

Output format: Markdown with exactly these three second-level headings, in
this order — "Approach", "Setup", "Evaluation". Use prose or bullets under
each as appropriate. If the paper gives nothing for a section, write "Not
reported in the paper." under that heading. No other headings, no
preamble, no closing remarks.

---
name: intro
version: 2
input: pdf
output: intro_filtered.md
---
Distill the introductory and background material of this paper: the
context, the problem being addressed, the authors' motivation, the state
of prior work as the authors present it, and the references they treat as
foundational.

Rules:

- Write in the third person, as an outside reviewer describing the paper.
  Do not adopt the authors' voice, framing, or promotional tone.
- Where the authors coin their own terminology for established concepts,
  describe the concept in field-standard terms; note the authors' name for
  it once, then use the standard description.
- Preserve the authors' substantive reasoning for why the problem matters,
  attributed to them. Drop promotional language and unsupported assertions
  of importance.
- State the problem the paper addresses, covering both its precise
  formulation and its practical stakes to whatever extent the paper
  provides each.
- For prior work, capture which existing approaches the authors discuss
  and the specific shortcomings they attribute to them.
- For the authors' own proposal, capture what they propose and their
  stated justification for why it addresses the identified gap.
- List only references the authors single out as foundational to this work
  or as direct points of comparison; skip broad background citations. If
  the paper does not distinguish any, say so in one sentence.

Output format: Markdown with exactly these four second-level headings, in
this order — "Motivation & Problem Context", "Prior Work & Its
Limitations", "Core Thesis", "Key References". Use prose or bullets under
each as appropriate. If the paper gives nothing for a section, write "Not
stated in the paper." under that heading. No other headings, no preamble,
no closing remarks.

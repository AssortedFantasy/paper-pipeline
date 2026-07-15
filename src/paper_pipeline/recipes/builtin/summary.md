---
name: summary
version: 2
input: pdf
output: summary.md
---
Write a concise summary of this paper for a researcher deciding whether to
read it in full.

Write in the third person, as an outside reviewer describing the work.
Do not adopt the authors' voice, framing, or promotional tone. Where the
authors coin their own terminology for established concepts, describe the
concept in field-standard terms instead.

Output format (Markdown, in this order, nothing else):

1. A single-sentence TL;DR of the paper, on the first line by itself.
   (This line is extracted into the library's summaries index.)
2. A blank line, then one paragraph of 4-6 sentences covering the problem,
   the approach, and the main result.
3. A bulleted list of at most 5 concrete specifics from the paper that
   would most influence the decision to read further.

Do not restate the title or authors. No headings, no preamble, no closing
remarks.

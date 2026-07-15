---
name: summary
version: 1
input: transcription
output: summary.md
---
Write a concise summary of this paper for a researcher deciding whether to
read it in full.

- First line: a single-sentence TL;DR of the paper, on one line by itself.
  (This line is extracted into the library's summaries index.)
- Then one short paragraph (4-6 sentences): the problem, the approach, and
  the main result.
- Then a bulleted list of at most 5 notable specifics (datasets, methods,
  headline numbers, limitations).

Output only the summary in Markdown. Do not restate the title or authors.

# Architecture Decision Records

Durable implementation decisions for Paper Pipeline v2. `README.md`,
`AGENTS.md`, and the release checklist define product behavior; ADRs define
*how* where a choice is not obvious and would be expensive to relitigate.

## Rules

- Versioned contracts (converter, provider, recipe template, job model, and
  library format) are expected to improve through feedback. Change them in a
  new or amended ADR and update their compatibility tests; do not treat an
  early skeleton as an irreversible design.
- Number sequentially. Never delete an ADR; supersede it and link both ways.
- Keep each ADR short: context, decision, consequences.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-technology-stack.md) | Technology stack | Accepted |
| [0002](0002-library-layout.md) | Generated library layout | Accepted |
| [0003](0003-recipe-template-format.md) | Recipe template format | Accepted |
| [0004](0004-job-execution-model.md) | Job execution and interruption recovery | Accepted |
| [0005](0005-remote-conversion-over-ssh.md) | Remote conversion over SSH | Accepted |

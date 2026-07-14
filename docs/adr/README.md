# Architecture Decision Records

Durable implementation decisions for Paper Pipeline v2. REFACTOR.md defines
*what* the product does; ADRs define *how*, where the choice is not obvious
and would be expensive to relitigate.

## Rules

- Contracts marked FROZEN in source docstrings (converter, provider, recipe
  template, job model, library format) may only change through a new or
  amended ADR.
- Number sequentially. Never delete an ADR; supersede it and link both ways.
- Keep each ADR short: context, decision, consequences.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-technology-stack.md) | Technology stack | Accepted |
| [0002](0002-library-layout.md) | Generated library layout | Accepted |
| [0003](0003-recipe-template-format.md) | Recipe template format | Accepted |
| [0004](0004-job-execution-model.md) | Job execution and interruption recovery | Accepted |

---
description: Establish the project's durable constitution (must/never/prefer rules), including what NOT to build.
allowed-tools: Read, Edit, Bash
---

# /plan-constitution

Establish the durable, project-wide rules that every later stage must respect.

1. Read `planning/vision.md` and `planning/contract.yaml`.
2. Propose a SHORT list of constitution rules — aim for 5–8, never more than ~10
   (there is a real compliance cliff: more rules means the agent follows fewer).
   Each rule is one of:
   - `must`   — a hard requirement ("MUST do the simplest thing that meets the acceptance criteria")
   - `never`  — a prohibition, especially **what not to build** ("NEVER add a runtime dependency without a decision")
   - `prefer` — a soft default
3. Write them to `constitution[]` in `planning/contract.yaml` with ids `CON-1`, `CON-2`, …
   Keep each rule to one imperative line. Put the most important first.
4. Confirm the rules with the user before finalizing — these are load-bearing.
5. Update `meta.updated_at` (today) and append a `changelog` entry. If
   `meta.stage` is `constitution`, leave it; the next stage is `/plan-vision`.

Do not invent product scope here — only rules. The contract validation hook will
reject malformed entries; if it does, read the error and fix the YAML.

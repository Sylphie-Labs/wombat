---
name: schema-reference
description: The planning-worx master contract schema — node and governance shapes, ids, enums, and required fields. Use whenever reading or writing planning/contract.yaml so edits stay valid.
---

# planning-worx contract schema (quick reference)

Source of truth is `planning/contract.yaml`; the formal schema is
`planning/contract.schema.json`. A PostToolUse hook validates every write — if it
blocks, read the error and fix the YAML.

## Top level
`schema_version: "1.0"`, `kind: contract`, `meta`, `vision`, `constitution[]`,
`non_goals[]`, `constraints[]`, `tech_stack[]`, `nodes[]`, `governance[]`, `changelog[]`.

`meta`: `project_id` (lowercase-kebab), `title`, `status` (planning|building|shipped|paused),
`stage` (constitution|vision|clarify|design|tickets|analyze|implement), `created_at`, `updated_at`.

## Nodes — one self-similar shape, `kind` sets the level
Flat list; the tree comes from `parent`. Ids: `FEAT-`, `EP-`, `TK-`, `TASK-` + number,
matching `kind` feature|epic|ticket|task.

Common fields: `id`, `kind`, `parent` (id or null), `title`, `intent`,
`status` (backlog|todo|in_progress|blocked|done|canceled), `priority` (P1|P2|P3),
`estimate` (S|M|L|XL), `engineering_level` (prototype|mvp|production|regulated),
`complexity_budget`, `acceptance_criteria[]` ({given,when,then}), `non_goals[]`,
`depends_on[]`, `blocks[]`, `files_in_scope[]`, `design_refs[]`, `code_refs[]`,
`test_refs[]`, `poc` ({question,hypothesis,status,result}), `created_at`, `updated_at`.

**Tickets MUST have** ≥1 `acceptance_criteria`, `engineering_level`, and `priority`.

## Governance — one base, `type` discriminator
Ids by type: `Q-` open_question, `ASMP-` assumption, `RISK-` risk, `ISS-` issue,
`DEP-` dependency, `DEC-` decision, `DEF-` deferral, `NG-` non_goal.
Common: `id`, `type`, `title`, `status`, `owner`, `scope` (node id or 'project'),
`date_raised`, `links[]`, `converted_from`, `resolution`.

Per-type required fields:
- decision → `context`, `decision`, `consequences` (+ optional `alternatives`, `supersedes`/`superseded_by`). **Append-only**.
- deferral → `revisit_trigger` (the condition that brings it back; this is what makes it "later" not "never").
- risk → `probability`, `impact` (low|medium|high), `mitigation` (+ optional `trigger`).
- assumption → `validation_method` (+ optional `becomes`).

## Discipline
Mutable: node status, governance status. Append-only: `decisions`, `changelog`
(a hook enforces this — supersede, never edit). Always bump `meta.updated_at` and
add a `changelog` entry on a real change.

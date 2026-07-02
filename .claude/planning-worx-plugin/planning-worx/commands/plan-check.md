---
description: Validate the master contract against its schema and integrity rules; fix any reported errors.
allowed-tools: Read, Edit, Bash
---

# /plan-check

Run:

```
node "${CLAUDE_PLUGIN_ROOT}/scripts/validate.js"
```

If it reports `VALID`, summarize the counts and stop.

If it reports `INVALID`, read each error, open `planning/contract.yaml`, and fix
the underlying problem (bad enum, missing required field, broken reference, cycle,
duplicate id). Re-run until valid. Do not work around a real integrity error by
deleting data — fix the data. Append-only history (decisions, changelog) must be
preserved; the append-guard hook will block edits that violate it.

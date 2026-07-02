---
description: Show plan progress — rollups per feature, the next ready ticket, open questions, and blockers.
allowed-tools: Bash
---

# /plan-status

Run:

```
node "${CLAUDE_PLUGIN_ROOT}/scripts/state_digest.js" --status
```

Show the output to the user as-is, then add one line pointing at the single most
useful next action (usually the "next ready ticket", or resolving a blocker /
open question if one is holding things up).

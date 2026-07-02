# cog-worx bug queue

One line per report, newest first. `[OPEN]` / `[IN-PROGRESS]` / `[FIXED]` / `[NEEDS-INFO]` / `[WONTFIX]`.

<!-- Example:
- [OPEN] [2026-07-02-model-timeout-swallowed](2026-07-02-model-timeout-swallowed.md) — blocker — Model.complete timeout raises nothing, hangs the loop
-->

- [OPEN] [2026-07-02-stagegraph-route-guard-is-runtime-only](2026-07-02-stagegraph-route-guard-is-runtime-only.md) — medium — undeclared Wait-to-self edge passes registration + full suite, crashes only on first real-Engine drive; guard fires too late
- [OPEN] [2026-07-02-no-stagecontext-test-double](2026-07-02-no-stagecontext-test-double.md) — low — no StageContext double in cogworx.testing; every stage-building consumer hand-rolls one

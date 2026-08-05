# 09 — Browser and computer use (TK-371, SWEEP 09)

Run date: 2026-08-05, per `protocol.md`, one sweep at a time per DEC-86.

**Headline: FEAT-10 is fully built and completely unwired.** The Playwright
capability, the browse stage and the web-page ingest stage all exist and are
tested. **None of them is registered into the production runtime**, so there is
no live surface to drive and every one of this sweep's four acceptance criteria
is BLOCKED on the same root cause. Routed as **ISS-66**.

| AC | Check | Class | Result |
|---|---|---|---|
| AC1 | a real a11y-tree task driven end to end | A | **BLOCKED** — no browser capability at runtime |
| AC2 | login/credential step hands off to the human | A | **BLOCKED** — same |
| AC3 | form submit written to the trail before it happens, approval blocks | A | **BLOCKED** — same |
| AC4 | external tier fails closed, no self-grant | A | **PASS (structural)** — the one thing this sweep could establish |

---

## The root cause, established four ways

**1. The capability is not registered in the composition root.**
`grep -n "browser\|Playwright\|playwright" src/wombat/bootstrap.py` → **zero
hits.** Nothing in `assemble_runtime` constructs or registers a browser
capability.

**2. The stages are not wired into any pathway.**
`grep -rn "browse_and_read\|BrowseAndRead\|PlaywrightCapability"
src/wombat/bootstrap.py src/wombat/pathways/ src/wombat/runtime.py` → **zero
hits.** No pathway routes to a browse stage.

**3. The code genuinely exists** — this is not a missing feature, it is an
unconnected one:
- `src/wombat/capabilities/playwright_capability.py`
- `src/wombat/stages/browse_and_read.py` (dispatches `BROWSER_CAPABILITY =
  "browser"`)
- `src/wombat/stages/ingest_web_page.py`

**4. Playwright is actually installed** — `playwright-1.61.0.dist-info` is
present in `.venv`, so the `--extra browser` sync was done. The dependency is
there; the wiring is not.

**The stale pointer that made this look wired.** `browse_and_read.py:38-39`
says the capability name is *"the exact literal this stage dispatches, so real
wiring (TK-153) is a drop-in registration, not a rename."* **TK-153 is not the
wiring ticket** — it is *"Web-page ingest taint call-site — wire IngestWebPage
to the shared taint machinery"*, a P3 taint-tagging split out of TK-148, and it
is legitimately `done` for what it actually covers. No ticket in the contract
ever registered the browser capability into the runtime. The docstring points at
a ticket that was never going to do it.

## AC4 — external tier fails closed, no self-grant — **PASS (structural)**

The one criterion establishable without a live browser, and it passes for a
reason that is stronger than the AC anticipated.

`bind_external_tier(...)` — described in-tree as *"the ONE sanctioned admission
call site (TK-151/DEC-22)"* — is called at **exactly two** places in the entire
source tree:

- `src/wombat/integrations/gmail/draft_composer.py:219`
- `src/wombat/stages/dispatch_approved.py:118`

Both are approved `AwaitHuman` dispatch stages, which is precisely the DEC-20/
DEC-22 posture the AC demands. **No browser stage admits the external tier**, and
nothing self-grants at runtime.

So an external-tier browser action fails closed today in the strongest possible
sense: not merely refused by policy, but unreachable — the capability is not
registered, and the stage that would dispatch it is not admitted.

**Honesty about what this does and does not prove.** The AC asked for the
refusal to be *observed* rather than read from config. What is observed here is
the absence of any admission path, which is a stronger structural guarantee but
a *different* observation than watching a boot refuse an attempted action. When
FEAT-10 is wired, this check must be re-run in its intended form.

## AC1, AC2, AC3 — **BLOCKED**

All three require driving a real browser task through the running product. There
is no browser capability in the running product. Each is blocked on ISS-66, not
on anything about the checks themselves.

Worth stating plainly, because these are the safety criteria: **the login
handoff (AC2) and the review-before-submit gate (AC3) are the two seams that are
the entire reason this feature is constrained** — and neither has ever been
exercised against a live surface on this host. The unit tests for
`browse_and_read.py` cover the degrade path, but a test dispatching a fake
capability proves a different thing than a browser that stops at a password
field. That distinction is the whole thesis of DEC-84.

**Not a safety risk today.** Nothing can reach out and act on the world, because
nothing is wired. The risk is entirely in the future: whenever FEAT-10 *is*
connected, it will be connected without any of these three checks ever having
been performed live.

## Findings routed

- **ISS-66 (new, MAJOR)** — FEAT-10 is built, tested, dependency-installed and
  entirely unregistered: no browser capability in `bootstrap.py`, no browse
  stage in any pathway, and the in-code pointer to its "real wiring" names a
  ticket (TK-153) that covers taint tagging instead. Blocks all of TK-371's
  behavioural ACs.

## State left behind

Read-only sweep. No runtime started or stopped, no browser launched, no site
visited, no credential touched, no capability tier enabled, no source or test
file edited. The voice-enabled runtime restored at the end of TK-370 was left
untouched.

## What this sweep does NOT claim

Per DEC-85(c) the sweep ran and its findings were routed. It does **not** assert
FEAT-10 has PASSED — three of four ACs are blocked and the fourth passed in a
different form than intended. TK-377 alone adjudicates.

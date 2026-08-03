# ios/ — wombat DeviceSurface companion apps (DRAFT SOURCE, tier A)

## This source has never been compiled

**This source has never been compiled.** It was written on a Windows host with no Mac
and no Apple Developer account. **The first Mac session is expected to produce compile
errors.** That is not a failure of this ticket — it is the honesty cost DEC-82(f) names
up front rather than discovering it later. Nothing in this tree has been opened in
Xcode, built, run in a Simulator, or run on a device. Any comment, commit message, or
ticket status that implies otherwise is wrong and should be corrected.

Every acceptance criterion behind this source was checked by **reading the code against
`planning/design/wire-contract.md`**, not by compiling it. That is a real but different
bar (DEC-82(f)): does the code exist at the named path, does every wire call go through
`Shared/WireContract.swift`, is a projection function reviewably pure, is a dead state a
real UI state rather than a TODO. It is not the project's usual
every-acceptance-criterion-passes-a-runnable-check bar, because that bar cannot hold for
Swift with no compiler available (Q-136).

## The three gates (DEC-82)

Writing Swift needs neither a Mac nor money — it is text, and the wire it speaks was
locked payload-level by DEC-83. Compiling and running it needs a Mac session with a free
Apple ID. Installing on real hardware needs the paid Apple Developer Program. DEC-82
names these as three gates, not one, and splits every EP-42/EP-43 ticket into a tier
accordingly:

| Tier | What it needs | Tickets |
|---|---|---|
| **A — Draft source** | Nothing. Runs today, on Windows, for free. | TK-355, TK-356, TK-357, TK-358, TK-359, TK-360 |
| **B — Compile & Simulator** | A Mac session, macOS, Xcode, a free Apple ID. No payment. Not a ticket — an operational step recorded so the first Mac session compiles and Simulator-runs the whole draft *before* paying for the developer program. | (none — DEC-82(b) deliberately mints no tickets for this step) |
| **C — Device verification** | The paid Apple Developer Program, a provisioned build, real hardware. Stays gated on Q-134. | TK-349, TK-350, TK-351, TK-352, TK-353 |

Every tier-A ticket maps to the tier-C ticket that will later verify its work on real
hardware:

| Draft (tier A) | Verified on hardware (tier C) |
|---|---|
| TK-355 — Xcode project, both targets, WireContract.swift, ATS, Keychain pairing store | *(foundation — no direct tier-C counterpart)* |
| TK-356 — pairing, HealthKit authorization, anchored sync, DEC-80 projection | TK-349 |
| TK-357 — background sync, offline buffer, app-side reset | TK-350 |
| TK-358 — push-to-talk capture, WebSocket playback, phone-side WatchConnectivity | TK-351 |
| TK-359 — watchOS push-to-talk, direct-POST-primary, token handoff | TK-352 |
| TK-360 — watchOS playback, charging/unreachable dead states | TK-353 |

## The wire contract

`ios/Shared/WireContract.swift` **mirrors** `planning/design/wire-contract.md` (DEC-83);
it never authors the wire. Every URL, request header name, Codable payload struct, and
enum for the five locked routes lives in that one file, and nowhere else in this tree.
If a wombat-side field, unit, or enum changes, the fix is a one-file edit on the Swift
side, not a hunt through call sites.

## The Xcode project structure (R8, binding)

This project uses **Xcode 16 file-system-synchronized root groups**
(`PBXFileSystemSynchronizedRootGroup`) for `ios/WombatCompanion`,
`ios/WombatCompanion Watch App`, and `ios/Shared` — **not** the traditional
file-by-file `PBXGroup`/`PBXBuildFile` listing. `ios/Shared` is referenced by **both**
targets.

**This means later draft tickets add source files by dropping a `.swift` file into the
relevant directory. They must not, and do not need to, edit `project.pbxproj`.** Xcode
picks the new file up automatically because target membership for a synchronized group
is directory membership, not a listed file reference. This is why five sibling tier-A
tickets (TK-356 through TK-360) can each add files to this tree without any of them
contending for edits to one unmergeable project file.

## What is out of scope here (TK-355)

No HealthKit queries, no audio capture or playback, no background execution, no
WatchConnectivity logic — TK-356 through TK-360 own all of it. No claim that anything
compiles, runs, or was tested. No runtime use of a paid-account-dependent capability;
declaring an entitlement in a plist is not using it.

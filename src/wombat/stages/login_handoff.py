"""wombat.stages.login_handoff — LoginHandoffStage + LoginConfirmStage: detect a login page in
the current browser page's a11y snapshot, park ``AwaitHuman`` for a human to complete login by
hand, and resume once they confirm (TK-136, Q-114 rulings (f)-(j)).

STANDALONE STAGE PAIR, NOT ``ProposeDispatchStage`` (``dispatch_base.py``, TK-149) subclasses —
mirrors ``draft_composer.py``'s Q-92 precedent, for the same structural reason: detection must
PASS THROUGH cleanly (``Done``, not ``AwaitHuman``) when no login page is present, and
``ProposeDispatchStage.run()`` is non-overridable and ALWAYS parks. Both stages REUSE the TK-149/
``dispatch_approved`` seams rather than reimplementing them: ``ProposalWriter``
(``dispatch_base.py``) for ``record_proposal``, ``ApprovalTrailWriter`` + ``MissingApprovalAnswer``
(``dispatch_approved.py``) for the decision-read/refusal machinery. ``action_id =
f"{run_id}:login_handoff"`` is ALWAYS keyed to the propose stage's own name (mirrors
``draft_dispatch.py``'s derivation), so both stages' trail writes land on the same
``action_trail_projection`` row.

``PageStateProvider`` seam (an injected async callable returning ``{"url": str, "snapshot": ...}``
for the CURRENT page) stands in for a direct, UNGATED read off the shared ``BrowserSession`` — the
same "standalone ``snapshot`` action exists for direct, ungated use" note
``playwright_capability.py``'s module docstring records. NEVER a gated ``ctx.dispatch``: by the
time a login page might be showing, the drive is very likely already tainted from an earlier
navigate/read, and a gated dispatch would raise ``TierViolation`` (Q-114 ruling f). Tests inject
fakes; the real BrowserSession-backed provider and pathway/runtime wiring are out of scope for
this ticket.

``page_has_login_textbox`` is a PURE function walking the JSON-native aria snapshot (nested
lists/dicts/strings — the exact shape ``playwright_capability.py``'s
``yaml.safe_load(aria_snapshot())`` produces) for any leaf/dict-key string of the form
``textbox "<name>"`` whose ``<name>`` case-insensitively equals one of the closed set
``password``/``passcode``/``passphrase``. RECORDED LIMITATION: aria snapshots carry no ``type``
attribute, so this is a courtesy handoff, not a guarantee — the STRONG guarantee is the deny-
always ``_checked_fill`` guard in ``playwright_capability.py`` (TK-136's other deliverable). No
pixel matching. No credential is ever read or stored anywhere in this module — only the
snapshot's role/name tree is ever inspected, never a field's value.

``LoginHandoffStage`` (name ``login_handoff``, transitions ``("login_confirm",)``): detection-
negative -> ``Done("wombat.login_check_passed")``, a clean pass-through with zero trail writes.
Detection-positive -> ``record_proposal`` (``ActionType.LOGIN_HANDOFF``, target = the login page's
domain, ``human_summary = f"login required at {domain}"``) then ``AwaitHuman(to="login_confirm")``.

``LoginConfirmStage`` (name ``login_confirm``, BEHAVIORALLY self-parks on a still-incomplete login
— every real run() either ``Done``s or returns ``AwaitHuman(to="login_confirm")``): its DECLARED
``transitions`` is ``("login_confirm", "login_confirm_terminal")`` — the second entry is a
declared-but-NEVER-taken stub edge, identical in shape and purpose to wombat's OWN established
``BriefTimerTerminalStage`` (``brief_pathway.py``, TK-97/Q-80-as-amended, itself citing a TK-53
``_WaitForeverStage``/``_TerminalStage`` precedent): cog-worx's ``StageGraph`` construction-time
invariant requires SOME reachable stage with ``transitions == ()`` (Done never declares an edge,
so a purely self-looping stage otherwise makes ANY graph containing it un-constructible —
proven directly: a bare ``{login_handoff, login_confirm}`` graph raises ``StageGraphError: no
terminal stage reachable``). ``LoginConfirmTerminalStage`` is that stub; ``run()`` never routes to
it and it raises if ever entered (a wiring bug). Locates the LATEST committed step whose
``stage_name`` is EITHER
``"login_handoff"`` OR ``"login_confirm"`` — the reverse journal walk from
``dispatch_approved.py``/``draft_dispatch.py``, WIDENED to both names, because a re-park here lands
its own next answer at ITS OWN step, not the original propose step (``Engine.provide_human_input``
records at the last committed step's own ``step_index``). Reads the decision there via
``ctx.read_human_input``. ``decision == "login-complete"``: a FRESH ``PageStateProvider`` read
decides the outcome — the password textbox now GONE means ``mark_dispatched`` +
``Done("wombat.login_confirmed")``; still PRESENT means ``AwaitHuman`` again to itself. Any other/
absent/malformed decision: ``record_refusal`` + raise ``MissingApprovalAnswer`` (loud, never a
silent no-op — mirrors ``dispatch_approved.py``/``draft_dispatch.py``'s refuse-loud shape exactly).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import AwaitHuman, Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.stages.dispatch_approved import ApprovalTrailWriter, MissingApprovalAnswer
from wombat.stages.dispatch_base import ProposalWriter
from wombat.trail.schema import ActionType

# This module's own committed output kinds.
LOGIN_CHECK_PASSED = "wombat.login_check_passed"
LOGIN_HANDOFF_PROPOSAL = "wombat.login_handoff_proposal"
LOGIN_CONFIRMED = "wombat.login_confirmed"

_LOGIN_HANDOFF_STAGE_NAME = "login_handoff"
_LOGIN_CONFIRM_STAGE_NAME = "login_confirm"
_LOGIN_CONFIRM_TERMINAL_STAGE_NAME = "login_confirm_terminal"
_LOGIN_STEP_STAGE_NAMES = (_LOGIN_HANDOFF_STAGE_NAME, _LOGIN_CONFIRM_STAGE_NAME)

_VALID_DECISIONS = ("login-complete",)

# The closed, case-insensitive set of accessible names the detector treats as a login textbox
# (recorded limitation, see module docstring: exact match, not substring — a courtesy handoff).
_LOGIN_TEXTBOX_NAMES = frozenset({"password", "passcode", "passphrase"})
_TEXTBOX_RE = re.compile(r'^textbox\s+"([^"]*)"')

PageState = Mapping[str, Any]
PageStateProvider = Callable[[], Awaitable[PageState]]
"""Injected seam: ``() -> {"url": str, "snapshot": <JSON-native a11y tree>}`` for the CURRENT
page. See the module docstring for why production wiring must be a direct, ungated
``BrowserSession`` read rather than a gated ``ctx.dispatch``."""


def _is_login_textbox_label(label: str) -> bool:
    match = _TEXTBOX_RE.match(label.strip())
    if match is None:
        return False
    return match.group(1).strip().casefold() in _LOGIN_TEXTBOX_NAMES


def page_has_login_textbox(snapshot: Any) -> bool:
    """Pure detector (TK-136): walk a JSON-native a11y snapshot (nested lists/dicts/strings, the
    shape ``playwright_capability.py``'s ``_capture_snapshot`` produces) for a textbox node whose
    accessible name case-insensitively matches the closed set password/passcode/passphrase. See
    the module docstring for the recorded limitation (no ``type`` attribute in an aria snapshot —
    this is a courtesy handoff, the strong guarantee is the fill-time guard)."""
    if isinstance(snapshot, str):
        return _is_login_textbox_label(snapshot)
    if isinstance(snapshot, Mapping):
        return any(
            page_has_login_textbox(key) or page_has_login_textbox(value)
            for key, value in snapshot.items()
        )
    if isinstance(snapshot, list):
        return any(page_has_login_textbox(item) for item in snapshot)
    return False


def _domain_from_url(url: str) -> str:
    """The trail row's ``target``/human summary domain — ``url``'s netloc, falling back to the
    raw ``url`` itself if it carries none (e.g. a bare host or malformed value)."""
    netloc = urlparse(url).netloc
    return netloc or url


class LoginHandoffStage:
    """Detects a login page in the current page state and parks for a human to complete login by
    hand (TK-136). Detection-negative is a clean pass-through — see module docstring."""

    name: str = _LOGIN_HANDOFF_STAGE_NAME
    transitions: tuple[str, ...] = (_LOGIN_CONFIRM_STAGE_NAME,)

    def __init__(self, *, writer: ProposalWriter, page_state: PageStateProvider) -> None:
        self._writer = writer
        self._page_state = page_state

    async def run(self, ctx: StageContext) -> StageResult:
        state = await self._page_state()
        url = str(state["url"])
        snapshot = state["snapshot"]
        now = ctx.clock()

        if not page_has_login_textbox(snapshot):
            return Done(
                output=Artifact(
                    kind=LOGIN_CHECK_PASSED,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                    data={"url": url},
                )
            )

        domain = _domain_from_url(url)
        human_summary = f"login required at {domain}"
        action_id = f"{ctx.run_id}:{self.name}"

        self._writer.record_proposal(
            action_id=action_id,
            action_type=ActionType.LOGIN_HANDOFF,
            human_summary=human_summary,
            target=domain,
            proposed_at=now,
        )

        return AwaitHuman(
            question=human_summary,
            to=_LOGIN_CONFIRM_STAGE_NAME,
            output=Artifact(
                kind=LOGIN_HANDOFF_PROPOSAL,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"action_id": action_id, "url": url, "domain": domain},
            ),
        )


class LoginConfirmStage:
    """Resumes once a human answers ``login-complete``; re-parks to itself if a fresh page-state
    read still shows the password textbox (TK-136). See module docstring for the widened
    stage-identity journal walk, the refuse-loud shape, and why ``transitions`` carries a second,
    declared-but-never-taken stub edge."""

    name: str = _LOGIN_CONFIRM_STAGE_NAME
    # The self-park edge PLUS the never-taken ``LoginConfirmTerminalStage`` stub edge — see the
    # module docstring for why the stub is required to satisfy cog-worx's "the graph can end"
    # construction invariant (mirrors wombat's own ``BriefTimerTerminalStage`` precedent).
    transitions: tuple[str, ...] = (_LOGIN_CONFIRM_STAGE_NAME, _LOGIN_CONFIRM_TERMINAL_STAGE_NAME)

    def __init__(self, *, writer: ApprovalTrailWriter, page_state: PageStateProvider) -> None:
        self._writer = writer
        self._page_state = page_state

    async def _locate_latest_login_step_index(self, ctx: StageContext) -> int | None:
        """Walk this run's committed step history in reverse for the LATEST step whose
        ``stage_name`` is EITHER ``login_handoff`` OR ``login_confirm`` — widened (unlike
        ``dispatch_approved.py``/``draft_dispatch.py``'s single-name lookup) because a self-park
        here commits its OWN next step under ``login_confirm``, and the newest answer always lands
        at the newest park, whichever name it carries."""
        run = await ctx.journal.load_run(ctx.run_id)
        if run is None:
            return None
        for step in reversed(run.steps):
            if step.stage_name in _LOGIN_STEP_STAGE_NAMES:
                return step.step_index
        return None

    async def run(self, ctx: StageContext) -> StageResult:
        action_id = f"{ctx.run_id}:{_LOGIN_HANDOFF_STAGE_NAME}"
        now = ctx.clock()

        step_index = await self._locate_latest_login_step_index(ctx)
        if step_index is None:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=(
                    f"login confirmation refused: no {_LOGIN_HANDOFF_STAGE_NAME!r}/"
                    f"{_LOGIN_CONFIRM_STAGE_NAME!r} step found in this run's step history"
                ),
                target=_LOGIN_HANDOFF_STAGE_NAME,
                proposed_at=now,
            )
            raise MissingApprovalAnswer(
                f"{self.name}: no {_LOGIN_HANDOFF_STAGE_NAME!r}/{_LOGIN_CONFIRM_STAGE_NAME!r} "
                f"step in run {ctx.run_id!r}'s step history for action_id={action_id!r} — "
                "cannot locate the parked answer"
            )

        answer = await ctx.read_human_input(step_index)
        decision = answer.data.get("decision") if answer is not None else None

        if decision not in _VALID_DECISIONS:
            self._writer.record_refusal(
                action_id=action_id,
                human_summary=(
                    f"login confirmation refused: no valid answer at step {step_index} "
                    f"(decision={decision!r})"
                ),
                target=_LOGIN_HANDOFF_STAGE_NAME,
                proposed_at=now,
            )
            raise MissingApprovalAnswer(
                f"{self.name}: no valid 'decision' in human input at step {step_index} for "
                f"action_id={action_id!r} (got {decision!r})"
            )

        state = await self._page_state()
        url = str(state["url"])
        snapshot = state["snapshot"]

        if page_has_login_textbox(snapshot):
            domain = _domain_from_url(url)
            return AwaitHuman(
                question=f"login required at {domain}",
                to=_LOGIN_CONFIRM_STAGE_NAME,
                output=Artifact(
                    kind=LOGIN_HANDOFF_PROPOSAL,
                    produced_by=self.name,
                    provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                    data={"action_id": action_id, "url": url, "domain": domain},
                ),
            )

        self._writer.mark_dispatched(action_id, now)
        return Done(
            output=Artifact(
                kind=LOGIN_CONFIRMED,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
                data={"action_id": action_id, "url": url},
            )
        )


class LoginConfirmTerminalStage:
    """A never-reached terminal stub that exists ONLY to satisfy cog-worx's structural invariant
    that every ``StageGraph`` has a reachable terminal stage — mirrors wombat's own established
    ``BriefTimerTerminalStage`` (``brief_pathway.py``, TK-97/Q-80-as-amended, itself citing a
    TK-53 ``_WaitForeverStage``/``_TerminalStage`` precedent).

    ``LoginConfirmStage.run()`` only ever returns ``Done`` or ``AwaitHuman(to="login_confirm")``
    — it never routes here; this stub's declared edge on ``LoginConfirmStage.transitions`` is a
    purely STRUCTURAL edge that closes the graph. Entering it is a wiring bug, so it raises.
    """

    name: str = _LOGIN_CONFIRM_TERMINAL_STAGE_NAME
    transitions: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - never reached
        msg = (
            f"{_LOGIN_CONFIRM_TERMINAL_STAGE_NAME} must never be entered; "
            f"{_LOGIN_CONFIRM_STAGE_NAME} always ends via Done or re-parks on "
            f"AwaitHuman(to={_LOGIN_CONFIRM_STAGE_NAME!r})"
        )
        raise RuntimeError(msg)


__all__ = [
    "LOGIN_CHECK_PASSED",
    "LOGIN_CONFIRMED",
    "LOGIN_HANDOFF_PROPOSAL",
    "LoginConfirmStage",
    "LoginConfirmTerminalStage",
    "LoginHandoffStage",
    "PageState",
    "PageStateProvider",
    "page_has_login_textbox",
]

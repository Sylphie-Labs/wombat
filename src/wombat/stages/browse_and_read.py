"""BrowseAndRead — the drive-boundary Stage that taints on a web navigate/read (TK-133, Q-113
rulings c+f, EP-25).

The taint latch here is NOT bespoke stage code (Q-113c): TK-131's ``browser`` Capability is
registered on the EXTERNAL tier without the ``trusted-output`` tag, so dispatching it through
``ctx.dispatch`` (cog-worx's gate-dispatched chokepoint, ``dispatch_one``) structurally latches
``TaintState`` BEFORE ``invoke`` runs — the navigate dispatch itself both taints the drive AND
still executes (Q-113c). Any SUBSEQUENT external dispatch in the same drive then raises
``TierViolation``. Consequently this stage makes EXACTLY ONE ``ctx.dispatch`` call, with
``action="navigate"``: the navigate action returns the a11y snapshot in that same invoke (the
ruled one-dispatch shape, Q-113c) — a separate follow-up ``snapshot`` dispatch would never be
reached, and it matches the one-URL-per-invocation non_goal.

This stage performs NO injection-detection of its own (DEC-19): the page's a11y-derived text is
returned verbatim as inert ``data`` on the output ``Artifact`` — never interpreted, never used to
decide a follow-up tool call. Whatever the page says, it is just a string in a JSON field.

FAILURE (unreachable URL, empty a11y snapshot, or a structured capability error) returns
``Degraded`` (cog-worx's first-class S8 result) with the cause — this stage NEVER falls back to
a screenshot automatically (that decision belongs to a caller, per the ticket's non_goals) and
NEVER lets an exception escape to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Degraded, Done, StageResult
from cogworx.loop.stage import StageContext

WEB_PAGE_READ_REQUEST = "wombat.web_page_read_request"
WEB_PAGE_READ = "wombat.web_page_read"
WEB_PAGE_READ_ERROR = "wombat.web_page_read_error"

BROWSER_CAPABILITY = "browser"
"""The capability name TK-131's ``PlaywrightCapability`` registers under (Q-113b) — the exact
literal this stage dispatches, so real wiring (TK-153) is a drop-in registration, not a rename."""


def web_page_read_request_from_artifact_data(data: dict[str, Any]) -> str:
    """The upstream wire contract: the URL to read, and nothing else — no runtime/pathway
    registration decision is made here (out of scope, TK-153)."""
    return str(data["url"])


def web_page_read_to_artifact_data(url: str, readable_text: str) -> dict[str, Any]:
    """Serialize this stage's success output. ``tainted`` is always ``True`` here: reaching this
    branch means the ``browser`` dispatch already ran, which structurally latches the drive
    (Q-113c) regardless of the page's content."""
    return {"url": url, "readable_text": readable_text, "tainted": True}


def web_page_read_error_to_artifact_data(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "error": error}


def _structured_error(response: Any) -> str | None:
    """Detect a structured capability error (e.g. ``{"ok": False, "error": ...}``, mirroring
    the click/type/select action convention in ``playwright_capability.py``). Returns the error
    detail, or ``None`` when ``response`` carries no such signal."""
    if isinstance(response, Mapping) and response.get("ok") is False:
        return str(response.get("error", "capability reported failure"))
    return None


def _extract_readable_text(snapshot: Any) -> str:
    """Flatten an a11y snapshot (a JSON-native nested structure of dicts/lists/strings, per
    ``playwright_capability._capture_snapshot``) into one readable-text string.

    Recurses through lists and dict values and joins every string leaf with a space — content-
    agnostic by design (DEC-19): this never inspects, filters, or interprets the text, it only
    reshapes it from a tree into a flat string."""
    if snapshot is None:
        return ""
    if isinstance(snapshot, str):
        return snapshot
    if isinstance(snapshot, Mapping):
        return " ".join(
            part for part in (_extract_readable_text(v) for v in snapshot.values()) if part
        )
    if isinstance(snapshot, list | tuple):
        return " ".join(
            part for part in (_extract_readable_text(item) for item in snapshot) if part
        )
    return str(snapshot)


class BrowseAndRead:
    """Navigates to ONE URL via the gate-dispatched ``browser`` capability and returns
    structured, untrusted content — with the drive structurally tainted on the same dispatch
    (S10/DEC-19). No write actions, no model call anywhere in this stage.
    """

    name: str = "browse_and_read"
    transitions: tuple[str, ...] = ()

    def __init__(self, upstream_stage_name: str) -> None:
        self._upstream_stage_name = upstream_stage_name

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output(self._upstream_stage_name)
        if art is None:
            msg = f"browse_and_read: no {self._upstream_stage_name!r} output available yet"
            raise RuntimeError(msg)
        url = web_page_read_request_from_artifact_data(art.data)

        # The drive-boundary crossing: this is the ONE dispatch of action "navigate" this run
        # makes. It goes through the gate (ctx.dispatch -> dispatch_one), so it structurally
        # latches TaintState in the same call that fetches the page (Q-113c) — the a11y snapshot
        # comes back from THIS invoke, never a separate follow-up dispatch.
        try:
            response = await ctx.dispatch(BROWSER_CAPABILITY, {"action": "navigate", "url": url})
        except Exception as exc:  # unreachable URL / any capability failure — never raise (S8)
            return Degraded(
                reason=str(exc),
                output=Artifact(
                    kind=WEB_PAGE_READ_ERROR,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=web_page_read_error_to_artifact_data(url, str(exc)),
                ),
            )

        error = _structured_error(response)
        if error is not None:
            return Degraded(
                reason=error,
                output=Artifact(
                    kind=WEB_PAGE_READ_ERROR,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=web_page_read_error_to_artifact_data(url, error),
                ),
            )

        snapshot = response.get("snapshot") if isinstance(response, Mapping) else None
        readable_text = _extract_readable_text(snapshot)
        if not readable_text.strip():
            reason = f"empty a11y snapshot for {url}"
            return Degraded(
                reason=reason,
                output=Artifact(
                    kind=WEB_PAGE_READ_ERROR,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=web_page_read_error_to_artifact_data(url, reason),
                ),
            )

        return Done(
            output=Artifact(
                kind=WEB_PAGE_READ,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=web_page_read_to_artifact_data(url, readable_text),
            )
        )


__all__ = [
    "BROWSER_CAPABILITY",
    "WEB_PAGE_READ",
    "WEB_PAGE_READ_ERROR",
    "WEB_PAGE_READ_REQUEST",
    "BrowseAndRead",
    "web_page_read_error_to_artifact_data",
    "web_page_read_request_from_artifact_data",
    "web_page_read_to_artifact_data",
]

"""IngestWebPage — the drive-boundary Stage that taints on an untrusted web-page read
(TK-153, EP-25 closer, Q-113 ruling h).

This is the read-tier counterpart of TK-133's ``BrowseAndRead`` (which taints via an
UNTAGGED external-tier ``browser`` dispatch, Q-113c) and the web-side twin of TK-148's
``IngestEmailBody`` (``wombat.stages.ingest_email_body``): it registers a NEW read-tier
capability, ``read_web_page``, tagged ``"untrusted-source"`` at registration time, and crosses
the drive boundary through ``ctx.dispatch`` — cog-worx's gate-dispatched chokepoint
(``dispatch_one``). That dispatch is what structurally latches the drive's ``TaintState``
(unmodified cog-worx machinery, Q-64 ruling #1) — a plain Python string read here would never
call the gate and would silently NOT taint (CF-3.2-B), exactly the untagged-read violation this
module's tests hold open.

Per Q-113 ruling h, ``taint.py``'s own non-goal pins it to exactly ONE capability-name constant
(``READ_EMAIL_BODY_CAPABILITY``) — this module therefore defines its OWN
``READ_WEB_PAGE_CAPABILITY`` constant, its own capability-building/registration helpers, and its
own ``PageProvider`` seam type, all HERE rather than in ``wombat.safety.taint``. It imports
ONLY the shared, both-direction tag literal ``UNTRUSTED_SOURCE_TAG`` from that module — no
competing tag literal and no bespoke latch logic exists in this file (AC2).

Non-goals (DEC-19, mirrors TK-148): no content-filter / injection-detection logic — the latch is
structural, it never inspects page content. No modification to ``wombat.safety.taint``. No
browser interaction logic (TK-131/TK-132) and no runtime/pathway registration decision — this
stage only defines and dispatches the tagged read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from cogworx.capability.base import Capability
from cogworx.capability.registry import Registry, function_capability
from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.safety.taint import UNTRUSTED_SOURCE_TAG

WEB_PAGE_INGEST_REQUEST = "wombat.web_page_ingest_request"
WEB_PAGE_INGESTED = "wombat.web_page_ingested"

READ_WEB_PAGE_CAPABILITY = "read_web_page"
"""The one capability name this ticket's web call site registers and dispatches (Q-113 ruling
h) — lives here, not in ``wombat.safety.taint``, preserving that module's single-constant
non-goal (``READ_EMAIL_BODY_CAPABILITY`` only)."""

PageProvider = Callable[[str], Awaitable[str]]
"""Injected seam: ``url_or_key -> page_text``. Production wires the real page-fetch source;
tests wire fixture content, including adversarial injection payloads (the outcome must be
content-independent — the whole point of a STRUCTURAL latch)."""


def web_page_ingest_request_from_artifact_data(data: dict[str, Any]) -> str:
    """The upstream wire contract for this stage: the URL (or provider key) to read, and
    nothing else — the page text itself never rides this wire, it is reachable solely through
    the injected ``page_provider`` behind ``read_web_page``."""
    return str(data["url"])


def web_page_ingested_to_artifact_data(url: str, page_text: str) -> dict[str, Any]:
    """Serialize this stage's terminal output. Downstream sanitization into a trusted-output
    artifact is out of scope for this ticket (non_goal)."""
    return {"url": url, "page_text": page_text}


def make_read_web_page_capability(page_provider: PageProvider) -> Capability:
    """Build the ``read_web_page`` capability (tier="read") backed by an injected provider.

    The capability itself carries no tags — tags are assigned at REGISTRATION time
    (``Registry.register(..., tags=...)``, code-assigned, the model cannot inject or remove
    them). Use :func:`register_read_web_page` to register it with the required
    ``"untrusted-source"`` tag in one step.
    """

    async def _read_web_page(url: str) -> str:
        return await page_provider(url)

    return function_capability(
        _read_web_page,
        name=READ_WEB_PAGE_CAPABILITY,
        tier="read",
    )


def register_read_web_page(registry: Registry, page_provider: PageProvider) -> None:
    """Register the tagged ``read_web_page`` capability on ``registry`` (Q-113 ruling h).

    This is HOW a drive accesses web-page content through this call site. Registering it with
    the literal ``"untrusted-source"`` tag (imported from ``wombat.safety.taint`` — the SHARED
    both-direction tag convention) is what makes the read structurally latch ``TaintState`` on
    dispatch (``TaintState.update`` — cog-worx machinery, unmodified). Any drive-side page
    access NOT routed through this capability never crosses the gate and never taints — the
    exact integrator-obligation gap this module's tests hold open.
    """
    registry.register(
        make_read_web_page_capability(page_provider),
        tags=(UNTRUSTED_SOURCE_TAG,),
    )


class IngestWebPage:
    """Reads an untrusted web page via the tagged ``read_web_page`` capability.

    Structurally taints the drive for the remainder of its lifetime (cog-worx's ``TaintState``,
    unmodified) — regardless of the page's content (DEC-19: the latch is structural, not
    content-filtered). This stage performs NO injection-detection of its own.
    """

    name: str = "ingest_web_page"
    transitions: tuple[str, ...] = ()

    def __init__(self, upstream_stage_name: str) -> None:
        self._upstream_stage_name = upstream_stage_name

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output(self._upstream_stage_name)
        if art is None:
            msg = f"ingest_web_page: no {self._upstream_stage_name!r} output available yet"
            raise RuntimeError(msg)
        url = web_page_ingest_request_from_artifact_data(art.data)

        # The drive-boundary crossing: this dispatch goes through the gate (ctx.dispatch ->
        # dispatch_one), so the tagged read structurally latches TaintState. Any page access NOT
        # routed through this capability would be the AC2 untagged-read violation.
        page_text = await ctx.dispatch(READ_WEB_PAGE_CAPABILITY, {"url": url})

        return Done(
            output=Artifact(
                kind=WEB_PAGE_INGESTED,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=web_page_ingested_to_artifact_data(url, page_text),
            )
        )


__all__ = [
    "READ_WEB_PAGE_CAPABILITY",
    "WEB_PAGE_INGESTED",
    "WEB_PAGE_INGEST_REQUEST",
    "IngestWebPage",
    "PageProvider",
    "make_read_web_page_capability",
    "register_read_web_page",
    "web_page_ingest_request_from_artifact_data",
    "web_page_ingested_to_artifact_data",
]

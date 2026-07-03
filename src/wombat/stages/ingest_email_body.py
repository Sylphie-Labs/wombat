"""IngestEmailBody — the drive-boundary Stage that taints on untrusted Gmail body reads
(TK-148, Q-64 ruling #4).

TK-75's ``GmailPoller`` is TRANSPORT: it fetches ``body_text`` and enqueues it, holds no tools,
and is not itself a drive (its own AC asserts it never holds ``gmail.drafts.create``). The
DRIVE-BOUNDARY crossing — the act this ticket structures — happens here: ``IngestEmailBody``
reads the body via the tagged ``read_email_body`` capability (``wombat.safety.taint``) through
the drive's ``ctx.dispatch`` seam, which is cog-worx's gate-dispatched chokepoint
(``dispatch_one``). That crossing is what structurally latches the drive's taint — a plain
Python string read here would never call the gate and would silently NOT taint (CF-3.2-B).

FROZEN CONTRACT for TK-75 (not yet built): the enqueued payload carries ``message_id``; the raw
body is reachable ONLY through the injected ``body_provider`` behind ``read_email_body`` — never
inline on the upstream wire. This stage's input wire (``EMAIL_INGEST_REQUEST``) mirrors that
contract with a representative ``{"message_id": ...}`` shape so TK-75 can satisfy it later with
zero rework here.
"""

from __future__ import annotations

from typing import Any

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.safety.taint import READ_EMAIL_BODY_CAPABILITY

EMAIL_INGEST_REQUEST = "wombat.email_ingest_request"
EMAIL_BODY_INGESTED = "wombat.email_body_ingested"


def email_ingest_request_from_artifact_data(data: dict[str, Any]) -> str:
    """The frozen TK-75 contract (Q-64): the enqueued payload carries ``message_id`` only — the
    body itself never rides this wire, it is reachable solely through the injected
    ``body_provider`` behind ``read_email_body``.
    """
    return str(data["message_id"])


def email_body_ingested_to_artifact_data(message_id: str, body_text: str) -> dict[str, Any]:
    """Serialize this stage's terminal output. Downstream sanitization into a trusted-output
    artifact (stripping ``body_text`` before it can cross into an untainted drive) is TK-80's
    job, not this ticket's (non_goal)."""
    return {"message_id": message_id, "body_text": body_text}


class IngestEmailBody:
    """Reads an untrusted Gmail body via the tagged ``read_email_body`` capability.

    Structurally taints the drive for the remainder of its lifetime (cog-worx's ``TaintState``,
    unmodified) — regardless of the body's content (DEC-19: the latch is structural, not
    content-filtered). This stage performs NO injection-detection of its own.
    """

    name: str = "ingest_email_body"
    transitions: tuple[str, ...] = ()

    def __init__(self, upstream_stage_name: str) -> None:
        self._upstream_stage_name = upstream_stage_name

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output(self._upstream_stage_name)
        if art is None:
            msg = f"ingest_email_body: no {self._upstream_stage_name!r} output available yet"
            raise RuntimeError(msg)
        message_id = email_ingest_request_from_artifact_data(art.data)

        # The drive-boundary crossing: this dispatch goes through the gate (ctx.dispatch ->
        # dispatch_one), so the tagged read structurally latches TaintState. Any body access NOT
        # routed through this capability would be the AC4 untagged-read violation.
        body_text = await ctx.dispatch(READ_EMAIL_BODY_CAPABILITY, {"message_id": message_id})

        return Done(
            output=Artifact(
                kind=EMAIL_BODY_INGESTED,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=email_body_ingested_to_artifact_data(message_id, body_text),
            )
        )


__all__ = [
    "EMAIL_BODY_INGESTED",
    "EMAIL_INGEST_REQUEST",
    "IngestEmailBody",
    "email_body_ingested_to_artifact_data",
    "email_ingest_request_from_artifact_data",
]

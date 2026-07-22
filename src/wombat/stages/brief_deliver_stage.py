"""BriefDeliverStage — deliver the rendered brief as text and/or terse voice (TK-101, Q-78).

FINAL stage of the morning-brief cluster (TERMINAL, ``transitions=()``): reads the rendered
``BriefText`` (TK-100, ``ctx.last_output("brief_compose")`` -> ``brief_text_from_artifact_data``),
appends it to an append-only text sink file, echoes it to stdout, and OPTIONALLY speaks it via an
injected ``speak`` sink (EP-30 narrowed to a sink seam — no voice provider is built here). NO model
call — this stage never phrases anything new, it only delivers what ``brief_compose`` already
rendered.

IDEMPOTENCY (AC4): the linear morning-brief pathway visits ``brief_deliver`` exactly once per run,
so a run-id-keyed marker embedded in the appended header is sufficient for intra-run crash-replay
exactly-once delivery. Before doing anything, ``run()`` scans the sink file for a header line
carrying ``[run=<ctx.run_id>]``. If found, the append/stdout echo/speak are ALL skipped and the
Done artifact reports ``replay=True`` — the file was already written on a prior attempt that
crashed before the engine committed the ``Done`` step, so this resumed ``run()`` re-enters here and
must not double-deliver. If not found, the block is appended, echoed, and (optionally) spoken, and
the Done reports ``replay=False``.

A sink WRITE failure RAISES loud (never swallowed): on resume, the marker is still absent, so the
next attempt retries the write from scratch (self-healing). Voice is best-effort: a ``speak()``
failure only logs a warning — the text delivery already stands and ``run()`` never raises for it.

``ctx`` surface is exactly ``ctx.run_id`` + ``ctx.clock`` (header timestamp, DEC-21 canonical tz,
never bare UTC) + ``ctx.last_output("brief_compose")`` — this stage NEVER touches ``ctx.journal``.

TK-288 (DEC-64 gap A): an optional ctor kwarg ``on_spoken`` (``(item_id, text) -> None``, default
``None``) feeds ``voice.reply_context.LastSpokenRegister`` — fired with ``("brief:" + ctx.run_id,
text)`` exactly when ``voice_spoken`` flips ``True`` below (the ``speak()`` try's ``else`` branch),
never on replay, voice-off, missing-speak-seam, or a raising ``speak()``. A raising hook is caught,
logged as exactly ONE WARNING, and delivery is otherwise unaffected — the register is a pure side
effect, never load-bearing for this stage's own result (mirrors ``sinks/speak.py``'s own guard).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.loop.result import Done, StageResult
from cogworx.loop.stage import StageContext

from wombat.stages.artifacts import (
    BRIEF_DELIVERED,
    brief_delivered_to_artifact_data,
    brief_text_from_artifact_data,
)

logger = logging.getLogger(__name__)


def _marker(run_id: str) -> str:
    """The run-id marker embedded in an appended header — bracketed so a run id that is a
    prefix of another (e.g. ``run-1`` vs ``run-10``) can never false-positive match (AC4)."""
    return f"[run={run_id}]"


class BriefDeliverStage:
    """Delivers the rendered morning brief as text and/or voice; TERMINAL (TK-101)."""

    name: str = "brief_deliver"
    transitions: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        sink_path: Path,
        tz: ZoneInfo,
        voice_enabled: bool,
        speak: Callable[[str], None] | None = None,
        on_spoken: Callable[[str, str], None] | None = None,
    ) -> None:
        self._sink_path = sink_path
        self._tz = tz
        self._voice_enabled = voice_enabled
        self._speak = speak
        self._on_spoken = on_spoken

    async def run(self, ctx: StageContext) -> StageResult:
        art = await ctx.last_output("brief_compose")
        if art is None:
            msg = "brief_deliver: no brief_compose output available yet"
            raise RuntimeError(msg)
        text, _degraded, _tokens_spent = brief_text_from_artifact_data(art.data)

        delivered_at_local = ctx.clock().astimezone(self._tz)
        delivered_at_iso = delivered_at_local.isoformat()
        marker = _marker(ctx.run_id)

        existing = self._sink_path.read_text(encoding="utf-8") if self._sink_path.exists() else ""

        if marker in existing:
            # Intra-run crash-replay (AC4): the file was already appended on a prior attempt
            # that crashed before the engine committed this stage's Done. Skip the append,
            # stdout echo, and speak entirely — exactly-once delivery.
            return Done(
                output=Artifact(
                    kind=BRIEF_DELIVERED,
                    produced_by=self.name,
                    provenance=Provenance(
                        source="system", confidence=1.0, recorded_at=ctx.clock()
                    ),
                    data=brief_delivered_to_artifact_data(
                        delivered_at=delivered_at_iso, voice_spoken=False, replay=True
                    ),
                )
            )

        header = f"{marker} delivered_at={delivered_at_iso}"
        block = f"{header}\n{text}\n\n"

        # Sink WRITE failure RAISES loud (never swallowed) — on resume the marker is still
        # absent, so the next attempt retries the write from scratch (self-healing).
        with self._sink_path.open("a", encoding="utf-8") as fh:
            fh.write(block)

        print(block, end="")

        voice_spoken = False
        if self._voice_enabled:
            if self._speak is None:
                logger.warning(
                    "brief_deliver: voice_enabled but no speak sink is wired; text-only delivery"
                )
            else:
                try:
                    self._speak(text)
                except Exception:
                    logger.warning(
                        "brief_deliver: speak sink raised; text delivery stands", exc_info=True
                    )
                else:
                    voice_spoken = True
                    if self._on_spoken is not None:
                        # TK-288: a pure side effect — never changes the Done below. Fires ONLY
                        # here, after speak() returned without raising, so the register only ever
                        # sees text that was genuinely heard.
                        try:
                            self._on_spoken("brief:" + ctx.run_id, text)
                        except Exception:
                            logger.warning(
                                "brief_deliver: on_spoken hook raised; ignoring", exc_info=True
                            )

        return Done(
            output=Artifact(
                kind=BRIEF_DELIVERED,
                produced_by=self.name,
                provenance=Provenance(source="system", confidence=1.0, recorded_at=ctx.clock()),
                data=brief_delivered_to_artifact_data(
                    delivered_at=delivered_at_iso, voice_spoken=voice_spoken, replay=False
                ),
            )
        )


__all__ = ["BriefDeliverStage"]

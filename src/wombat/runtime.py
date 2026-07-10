"""wombat.runtime — the ONE standing process (TK-53, Q-71).

``serve()`` starts/drives/stops the composition ``bootstrap.assemble_runtime`` already built —
it registers NOTHING here (no pathways, no sources; that is composition-root work per the
ticket's own non_goal). The standing loop is genuinely just two shipped cog-worx primitives
wired end to end (DEC-12 — wombat authors no loop of its own):

  1. ONE initial drive (``engine.run`` on the registered drain pathway with a fresh run_id and a
     heartbeat ``Artifact``) — this drains whatever is already queued, then self-parks on a
     durable ``Wait`` (TK-5's idle heartbeat) once the queue runs dry.
  2. ``cogworx.runtime.sweeper.Sweeper.run_forever`` — the shipped poller that leases due timers
     off the journal and calls ``engine.fire_timer`` to re-drive them. This IS the standing
     loop; nothing here re-implements it. TK-209 (DEC-37(g)): its injected ``clock`` (built by
     ``_sweeper_clock`` below) ALSO polls ``bundle.live_persona``'s cheap settings-file mtime
     check on every interval beat — the existing beat, not a new scheduler — so an app edit to
     the persona keys hot-applies without a restart.

RESTART (v1, Q-36/TK-14 cold-boot in-memory substrate): a restart is a clean slate — the journal
is empty, so ``serve()`` always starts exactly ONE fresh drain run. This is safe because the
Postgres queue (TK-2) is at-least-once: anything left un-acked before a crash redelivers on the
next drain. Real-substrate resume interplay (a durable parked run surviving a restart ALONGSIDE
a freshly-started one) is explicitly OUT of v1 scope — revisit once a real (non-cold-boot)
journal adapter is first wired.

SHUTDOWN is minimal by ruling (Q-71): a cooperative cancellation (``asyncio.CancelledError``) or
a ``KeyboardInterrupt`` stops the ``SourceRegistry`` and closes the queue/daily-ledger/pending-
journal/behavior-event-log/action-trail-writer (TK-184, when present) connections best-effort via
a ``finally`` — there is no signal-handler machinery here. Terminate-before-restart is the
OPERATOR's obligation (ASMP-2): this process assumes at most one live instance runs against a
given Postgres at a time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.runtime.sweeper import Sweeper

from wombat.bootstrap import RuntimeBundle, assemble_runtime
from wombat.config import ConfigurationError, load_config
from wombat.params import OperatingParams, load_operating_params
from wombat.pathways.brief_pathway import brief_timer_tick_artifact
from wombat.pathways.dream_trigger import dream_timer_tick_artifact
from wombat.safety.local_residency import check_config

_HEARTBEAT_ARTIFACT_KIND = "drain-tick"
_RUNTIME_RUN_ID_PREFIX = "wombat-drain"
# TK-97: the schedule pathway's initial-drive run-id prefix — arms the brief timer at boot (which
# is also the crash-miss catch: a boot after this morning's brief_time fires the missed brief once).
_SCHEDULE_RUN_ID_PREFIX = "wombat-brief-schedule"
# TK-52: the dream schedule pathway's initial-drive run-id prefix — arms the nightly dream timer
# at boot (also the crash-miss catch, mirrors _SCHEDULE_RUN_ID_PREFIX above).
_DREAM_SCHEDULE_RUN_ID_PREFIX = "wombat-dream-schedule"


def _sweeper_clock(bundle: RuntimeBundle) -> Callable[[], datetime]:
    """Build the Sweeper's ``clock=`` callable (DEC-37(g), TK-209).

    ``cogworx.runtime.sweeper.Sweeper.run_forever`` calls its injected ``clock`` exactly once per
    interval beat (``sweeper.py:72-77``) — this piggybacks ``bundle.live_persona``'s cheap mtime
    poll onto that EXISTING beat before returning the real wall clock, so an app edit to
    ``wombat.settings.json``'s persona keys (the settings-app path, TK-197/TK-200) hot-applies
    without a new scheduler. ``poll_settings_file()`` never raises (its own CON-3 guarantee), so
    this stays a safe drop-in for the plain ``lambda: datetime.now(UTC)`` it replaces — cog-worx
    itself is untouched.
    """

    def _clock() -> datetime:
        bundle.live_persona.poll_settings_file()
        return datetime.now(UTC)

    return _clock


def _heartbeat_artifact() -> Artifact:
    """The initial drive's input — a system-provenanced, contentless heartbeat (mirrors
    ``scripts/demo_drain.py``'s own ``drain-tick`` convention)."""
    now = datetime.now(UTC)
    return Artifact(
        kind=_HEARTBEAT_ARTIFACT_KIND,
        produced_by="wombat.runtime",
        provenance=Provenance(source="system", confidence=1.0, recorded_at=now),
        data={},
    )


async def _drive_and_serve(bundle: RuntimeBundle, *, params: OperatingParams) -> None:
    """Start the source registry, fire the ONE initial drive, then run the Sweeper forever.

    Stops the registry and closes the queue/daily-ledger/pending-journal connections in a
    ``finally`` so both a cooperative cancellation and a ``KeyboardInterrupt`` land the same
    best-effort cleanup (Q-71 ruling 7).
    """
    await bundle.source_registry.start()
    try:
        run_id = f"{_RUNTIME_RUN_ID_PREFIX}-{uuid4()}"
        await bundle.engine.run(
            run_id=run_id,
            session_id=run_id,
            pathway_id=bundle.drain_pathway_id,
            initial=_heartbeat_artifact(),
        )
        # TK-97: a SECOND initial drive arms the once-daily brief timer (and catches a brief missed
        # while the process was down: a boot past this morning's brief_time fires it once). Only
        # when the schedule pathway was registered — a brief-path-less boot skips it, never crashes.
        if bundle.brief_schedule_pathway_id is not None:
            schedule_run_id = f"{_SCHEDULE_RUN_ID_PREFIX}-{uuid4()}"
            await bundle.engine.run(
                run_id=schedule_run_id,
                session_id=schedule_run_id,
                pathway_id=bundle.brief_schedule_pathway_id,
                initial=brief_timer_tick_artifact(datetime.now(UTC)),
            )
        # TK-52: a THIRD initial drive arms the once-nightly dream timer (and catches a dream run
        # missed while the process was down: a boot past tonight's dream_time fires it once).
        # Mirrors the brief schedule drive above exactly. Only when the dream schedule pathway was
        # registered — None -> skip, never crashes (registration is unconditional per Q-85, so
        # this is expected to always fire, but the field stays checked to mirror the brief shape).
        if bundle.dream_schedule_pathway_id is not None:
            dream_schedule_run_id = f"{_DREAM_SCHEDULE_RUN_ID_PREFIX}-{uuid4()}"
            await bundle.engine.run(
                run_id=dream_schedule_run_id,
                session_id=dream_schedule_run_id,
                pathway_id=bundle.dream_schedule_pathway_id,
                initial=dream_timer_tick_artifact(datetime.now(UTC)),
            )
        sweeper = Sweeper(
            journal=bundle.journal,
            fire=bundle.engine.fire_timer,
            clock=_sweeper_clock(bundle),
        )
        await sweeper.run_forever(
            interval=timedelta(seconds=params.sweeper_interval_seconds),
            lease_ttl=timedelta(seconds=params.sweeper_lease_ttl_seconds),
        )
    finally:
        await bundle.source_registry.stop()
        bundle.queue.close()
        bundle.daily_ledger.close()
        bundle.pending_journal.close()
        # TK-111 (Q-98): closes the SAME leak class TK-173/CR-15 closed for DailyLedger —
        # behavior_event_log is constructed UNCONDITIONALLY by assemble_runtime, so this is
        # never a no-op (unlike action_trail_writer below).
        bundle.behavior_event_log.close()
        # TK-184 (CR2-10): closes the SAME leak class TK-173/CR-15 closed for DailyLedger — the
        # ActionTrailWriter assemble_runtime constructs only on a Google-creds-and-token boot
        # (WIRE 2/3) is None on a Google-less boot, so this is a no-op there.
        if bundle.action_trail_writer is not None:
            bundle.action_trail_writer.close()


async def serve() -> None:
    """Boot wombat as ONE standing process: assemble the composition, then start/drive/stop it.

    ``check_config(config)`` runs FIRST, right after ``load_config()`` (TK-150, Q-87 ruling 4) —
    the same-host storage-residency guard refuses (``RemoteStorageConfigError``, naming the
    offending config key) before anything else, including the ``WOMBAT_PG_DSN``-required check
    below.

    Requires ``WOMBAT_PG_DSN`` (Q-36: the queue is pg-only) — fails loud with
    ``ConfigurationError`` naming it when absent, rather than starting silently broken. It is
    deliberately NOT part of ``REQUIRED_ENV`` so tests and the demo stay bootable without it.
    """
    config = load_config()
    check_config(config)
    dsn = config.wombat_pg_dsn
    if not dsn:
        raise ConfigurationError(
            "missing required environment variable WOMBAT_PG_DSN; wombat will not start"
        )
    params = load_operating_params()
    bundle = assemble_runtime(config=config, dsn=dsn, params=params)
    await _drive_and_serve(bundle, params=params)


__all__ = ["serve"]

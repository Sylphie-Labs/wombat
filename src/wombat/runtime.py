"""wombat.runtime — the ONE standing process (TK-53, Q-71).

``serve()`` starts/drives/stops the composition ``bootstrap.assemble_runtime`` already built —
it registers NOTHING here (no pathways, no sources; that is composition-root work per the
ticket's own non_goal). The standing loop is genuinely just two primitives wired end to end
(DEC-12 — wombat authors no loop of its own beyond the TK-230 pump below):

  1. ``_run_drain_pump`` (TK-230, DEC-41): a wombat-owned pump, NOT a cog-worx primitive. Each
     beat it peeks ``bundle.queue.pending_count()`` (a plain read-only SELECT, never journaled)
     and, while it reports work, fires fresh sequential ``engine.run`` drives — each a fresh
     run_id on the registered drain pathway with a heartbeat ``Artifact`` — chained back-to-back
     until the peek reports empty, then sleeps one beat and re-peeks. This REPLACES the old "one
     initial drive that self-parks on empty" shape: a cog-worx ``Done`` run cancels its own
     timers and the Sweeper never re-drives a terminal run, so a stage-owned idle-heartbeat-Wait
     could never actually be woken again once Done — every item enqueued after the first was
     stranded until a restart. DEC-8's idles-on-empty guarantee now lives here: an idle beat
     starts ZERO runs.
  2. ``cogworx.runtime.sweeper.Sweeper.run_forever`` — the shipped poller that leases due timers
     off the journal and calls ``engine.fire_timer`` to re-drive them (e.g. a run parked
     AwaitHuman mid-pathway, or any other durable ``Wait``). This IS cog-worx's own standing
     loop; nothing here re-implements it. TK-209 (DEC-37(g)): its injected ``clock`` (built by
     ``_sweeper_clock`` below) ALSO polls ``bundle.live_persona``'s cheap settings-file mtime
     check on every interval beat — the existing beat, not a new scheduler — so an app edit to
     the persona keys hot-applies without a restart.

The pump and the Sweeper run CONCURRENTLY (``asyncio.gather``), but each is internally
sequential — ASMP-2 (exactly one draining process-wide) holds because the pump never starts a
second ``engine.run`` before the previous one returns.

RESTART (v1, Q-36/TK-14 cold-boot in-memory substrate): a restart is a clean slate — the journal
is empty, so ``serve()``'s pump starts draining from its very first beat. This is safe because
the Postgres queue (TK-2) is at-least-once: anything left un-acked before a crash redelivers on
the next drain. Real-substrate resume interplay (a durable parked run surviving a restart
ALONGSIDE a freshly-started one) is explicitly OUT of v1 scope — revisit once a real
(non-cold-boot) journal adapter is first wired.

TK-222 (EP-32, Q-110(d) ruling 5): when ``bundle.chat_surface`` is wired (``config.wombat_chat_
handshake_file`` non-blank), ``_drive_and_serve`` starts it right after the source registry and
writes EXACTLY ONE ``{"port": ..., "token": ...}`` handshake JSON line to its configured path
(parent dirs created, overwrite) — the Electron main process's read side (TK-223) is OUT of this
ticket's scope. Both the start and the write are GUARDED (CON-3): any failure — a bind failure,
an unwritable handshake path — is caught, logged as ONE loud WARNING, and the rest of assembly/
serve proceeds exactly as it would chat-less. Stopped, also guarded, in the SAME ``finally`` as
every other seam below.

SHUTDOWN is minimal by ruling (Q-71): a cooperative cancellation (``asyncio.CancelledError``) or
a ``KeyboardInterrupt`` stops the ``SourceRegistry`` and closes the queue/daily-ledger/pending-
journal/behavior-event-log/action-trail-writer (TK-184, when present) connections best-effort via
a ``finally`` — there is no signal-handler machinery here. Terminate-before-restart is the
OPERATOR's obligation (ASMP-2): this process assumes at most one live instance runs against a
given Postgres at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from cogworx.claims.provenance import Artifact, Provenance
from cogworx.runtime.sweeper import Sweeper

from wombat.bootstrap import _DRAIN_POLL_INTERVAL_SECONDS, RuntimeBundle, assemble_runtime
from wombat.chat.surface import ChatSurface
from wombat.config import ConfigurationError, load_config, resolve_wombat_zone
from wombat.external_store import EXTERNAL_ITEMS_PRUNE_DAYS
from wombat.params import PARAMS_APP_EDITABLE, OperatingParams, load_operating_params
from wombat.pathways.brief_pathway import brief_timer_tick_artifact
from wombat.pathways.dream_trigger import dream_timer_tick_artifact
from wombat.safety.local_residency import check_config
from wombat.scratchpad import SCRATCHPAD_PURGE_DAYS
from wombat.settings_store import SettingsStore, import_legacy_settings_file

logger = logging.getLogger(__name__)

_HEARTBEAT_ARTIFACT_KIND = "drain-tick"
_RUNTIME_RUN_ID_PREFIX = "wombat-drain"
# TK-97: the schedule pathway's initial-drive run-id prefix — arms the brief timer at boot (which
# is also the crash-miss catch: a boot after this morning's brief_time fires the missed brief once).
_SCHEDULE_RUN_ID_PREFIX = "wombat-brief-schedule"
# TK-52: the dream schedule pathway's initial-drive run-id prefix — arms the nightly dream timer
# at boot (also the crash-miss catch, mirrors _SCHEDULE_RUN_ID_PREFIX above).
_DREAM_SCHEDULE_RUN_ID_PREFIX = "wombat-dream-schedule"


def _read_params_overlay(dsn: str) -> dict[str, str]:
    """DEC-67(d), ruling v2.172 r3: read the EIGHT ``wombat_param_*`` rows ``wombat_settings``
    may carry, for ``load_operating_params``'s boot-time overlay.

    ANY failure — a first-ever boot before ``assemble_runtime``'s schema preflight has ever run
    (the ``wombat_settings`` table doesn't exist yet), a connection error, anything — degrades to
    an EMPTY overlay plus ONE ``logger.warning`` (CON-3 posture): this must never block boot, and
    the eventual schema preflight inside ``assemble_runtime`` runs regardless, healing the very
    next restart.
    """
    try:
        store = SettingsStore(dsn)
        try:
            raw = store.get_all()
        finally:
            store.close()
    except Exception:
        logger.warning(
            "could not read the wombat_settings operating-parameter overlay (e.g. a first-ever "
            "boot before the schema preflight has run); booting with wombat_params.yaml only",
            exc_info=True,
        )
        return {}
    # TK-315 (ISS-31 1): a deliberately-cleared setting (empty/None value) is not an error — drop
    # it from the overlay silently so load_operating_params falls back to wombat_params.yaml for
    # that key; genuinely-garbage (non-empty, invalid) values still overlay through and warn once
    # in params.py, unchanged.
    return {
        key: str(value)
        for key, value in raw.items()
        if key in PARAMS_APP_EDITABLE and value is not None and str(value) != ""
    }


def _sweeper_clock(bundle: RuntimeBundle) -> Callable[[], datetime]:
    """Build the Sweeper's ``clock=`` callable (DEC-37(g), TK-209; retargeted to Postgres by
    TK-243/DEC-43).

    ``cogworx.runtime.sweeper.Sweeper.run_forever`` calls its injected ``clock`` exactly once per
    interval beat (``sweeper.py:72-77``) — this piggybacks ``bundle.live_persona``'s cheap
    ``wombat_settings`` value-diff poll onto that EXISTING beat before returning the real wall
    clock, so an app edit to the persona keys (the settings-app path, TK-197/TK-200) hot-applies
    without a new scheduler. ``poll_settings()`` never raises (its own CON-3 guarantee), so this
    stays a safe drop-in for the plain ``lambda: datetime.now(UTC)`` it replaces — cog-worx itself
    is untouched.
    """

    def _clock() -> datetime:
        bundle.live_persona.poll_settings()
        return datetime.now(UTC)

    return _clock


class DrainWake:
    """Interruptible wait for the drain pump's idle beat (TK-269, DEC-56a). ``ChatSource.poll()``
    calls ``set()`` right after a chat message lands in ``wombat_queue``, so ``wait()`` — the
    pump's replacement for a plain ``asyncio.sleep(beat)`` — returns early instead of waiting out
    the whole beat. ``wait()`` swallows its own timeout (the ordinary idle case, byte-identical to
    the old sleep) and ALWAYS clears the event before returning, whichever way it returned, so N
    sets landing during one drive episode coalesce into at most one extra early wake rather than
    firing once per set (AC2)."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        """Wake a pump currently (or about to be) waiting. Safe to call any number of times
        between two ``wait()`` calls — extra sets before the next wait collapse into the one
        pending flag."""
        self._event.set()

    async def wait(self, timeout: float) -> None:
        """Block up to ``timeout`` seconds for a wake, returning immediately once one lands."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            self._event.clear()


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


def _existing_handshake_port(path: Path) -> int | None:
    """Read ``path`` and return its recorded port IF it parses as a handshake (one JSON object
    with an int ``port`` and a ``token``) — ``None`` for an absent or unparsable file (TK-268,
    ISS-20). Never raises: any read/parse failure is just "not a handshake"."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or "token" not in data:
        return None
    return port


def _handshake_port_is_live(port: int) -> bool:
    """Exclusive-bind-then-close probe (TK-268/ISS-20; repaired at the TK-335 batch-review round
    3 MAJOR finding): answers ``True`` iff something already holds ``port`` on 127.0.0.1.

    This attempts the IDENTICAL bind ``wombat.__main__._acquire_singleton_lock`` performs — same
    socket family/type (``AF_INET``/``SOCK_STREAM``), no ``SO_REUSEADDR`` or other options set,
    never ``listen()`` — because a plain TCP *connect* cannot see a bind-only holder. The
    singleton-lock socket ``_acquire_singleton_lock`` binds is NEVER ``listen()``-ed (it is a
    pure OS-level mutex, not a server socket), so a connect against it always gets
    ``ECONNREFUSED`` even while a real wombat runtime genuinely holds that exact port — this was
    live-proven on-machine with two real ``-m wombat`` runtime processes running: the old
    connect-based probe reported "not live" against a port that in fact refused the connect
    because a live runtime owned it. Mirroring the SAME bind exactly (rather than reusing
    ``connect``) is what makes this probe agree with what ``_acquire_singleton_lock`` itself would
    see, including on Windows, where ``SO_REUSEADDR`` semantics differ from POSIX and could
    otherwise make a naively-reused-option probe falsely report "not live" against a genuine
    holder.

    If OUR OWN bind succeeds, the port is free: release it immediately (never listen, never hold
    it) and report ``False``. Any ``OSError`` (address already in use, etc.) means something else
    already owns it: ``True``. There is no timeout dimension any more — a bind is a local,
    non-blocking-on-the-network syscall, unlike the connect it replaces, so it also answers a
    black-holed/firewalled port instantly rather than waiting out a bounded timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return True
    else:
        return False
    finally:
        sock.close()


def _write_chat_handshake(surface: ChatSurface) -> None:
    """Write EXACTLY ONE ``{"port": ..., "token": ...}`` handshake JSON line for ``surface``
    (TK-222 ruling 5) — parent dirs created, overwrite. Raises on any filesystem failure; the
    caller (``_start_chat_surface``) is the guard.

    TK-268 (ISS-20): before overwriting, if the file at ``surface.handshake_path`` already exists
    and parses as a handshake, probe its recorded port. If that port still answers, some OTHER
    live process owns it — REFUSE the overwrite (leave the file byte-unchanged), log exactly one
    loud WARNING naming the path and the live port, and return normally (this is a logged no-write,
    not a failure — the new surface still serves fine on its own port, only the file write is
    skipped). A dead/refused port, an absent file, or an unparsable file all fall through to the
    exact same unconditional write as before."""
    path = Path(surface.handshake_path)
    existing_port = _existing_handshake_port(path) if path.exists() else None
    if existing_port is not None and _handshake_port_is_live(existing_port):
        logger.warning(
            "serve: refusing to overwrite live chat handshake at %s — port %d still answers "
            "(leaving the file untouched; this launch's chat surface serves on its own port)",
            path,
            existing_port,
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"port": surface.port, "token": surface.token}), encoding="utf-8"
    )


async def _start_chat_surface(surface: ChatSurface | None) -> None:
    """Start ``surface`` and write its handshake file, GUARDED (CON-3, TK-222 ruling 5): a
    ``None`` surface (chat disabled) is a silent no-op; any OTHER failure — a bind failure, an
    unwritable handshake path — is caught, logged as ONE loud WARNING, and never propagates —
    the drain loop/brief/other sources are unaffected either way. Takes the bare ``ChatSurface``
    (not the whole ``RuntimeBundle``) so this seam is testable/callable standalone."""
    if surface is None:
        return
    try:
        await surface.start()
        _write_chat_handshake(surface)
    except Exception:
        logger.warning(
            "serve: chat surface failed to start; the chat input surface is disabled for this "
            "run (drain loop/brief/other sources unaffected)",
            exc_info=True,
        )


async def _stop_chat_surface(surface: ChatSurface | None) -> None:
    """Stop ``surface``, GUARDED — mirrors ``_start_chat_surface``'s posture so a stop failure
    never blocks the rest of the ``finally`` teardown below."""
    if surface is None:
        return
    try:
        await surface.stop()
    except Exception:
        logger.warning("serve: chat surface failed to stop cleanly", exc_info=True)


class _PendingCountableQueue(Protocol):
    """The one queue method the drain pump needs — a structural seam so tests can inject a bare
    stub instead of the real ``WombatQueue`` (mirrors ``DrainQueueStage``'s own ``_DrainableQueue``
    seam, TK-230/DEC-41)."""

    def pending_count(self) -> int: ...


class _DrivableEngine(Protocol):
    """The one engine method the drain pump needs — mirrors ``cogworx.runtime.engine.Engine.run``'s
    keyword-only signature so a real ``Engine`` satisfies this structurally with no cast, while
    tests can inject a bare spy instead."""

    async def run(
        self,
        *,
        run_id: str,
        session_id: str,
        pathway_id: str,
        initial: Artifact,
        pathway_version: int = 1,
    ) -> object: ...


async def _run_drain_pump(
    *,
    queue: _PendingCountableQueue,
    engine: _DrivableEngine,
    drain_pathway_id: str,
    beat: float,
    wake: DrainWake | None = None,
) -> None:
    """The ONE process-wide draining pump (TK-230, DEC-41, ASMP-2).

    Each beat: while ``queue.pending_count()`` (peeked fresh each check) reports work, fire a
    fresh ``engine.run`` drive — a fresh ``wombat-drain-<uuid>`` run_id on the drain pathway with
    a heartbeat ``Artifact`` — and await it before starting the next. Runs are chained strictly
    sequentially (never concurrent), which is what holds ASMP-2 (exactly one draining process-
    wide). Once the peek reports empty, idle: ``wake=None`` (the default; every non-chat caller —
    demo/tests) sleeps ``beat`` seconds byte-identically to before TK-269; a real boot passes a
    ``DrainWake`` (TK-269, DEC-56a), whose ``wait(beat)`` returns early the instant a chat enqueue
    sets it, so an interactive message starts draining well inside the beat instead of waiting it
    out — and returns at the ordinary ``beat`` timeout otherwise, same as a plain sleep. Either
    way an idle beat starts ZERO runs and (since ``pending_count`` is a plain SELECT) writes ZERO
    journal records; this is where DEC-8's idles-on-empty guarantee now lives, replacing the old
    stage-owned idle Wait that a Done run could never be woken back out of (see the module
    docstring).
    """
    while True:
        while queue.pending_count() > 0:
            run_id = f"{_RUNTIME_RUN_ID_PREFIX}-{uuid4()}"
            await engine.run(
                run_id=run_id,
                session_id=run_id,
                pathway_id=drain_pathway_id,
                initial=_heartbeat_artifact(),
            )
        if wake is None:
            await asyncio.sleep(beat)
        else:
            await wake.wait(beat)


async def _drive_and_serve(bundle: RuntimeBundle, *, params: OperatingParams) -> None:
    """Start the source registry, fire the schedule pathways' initial drives, then run the drain
    pump and the Sweeper forever (TK-230, DEC-41).

    Stops the registry and closes the queue/daily-ledger/pending-journal connections in a
    ``finally`` so both a cooperative cancellation and a ``KeyboardInterrupt`` land the same
    best-effort cleanup (Q-71 ruling 7).
    """
    await bundle.source_registry.start()
    await _start_chat_surface(bundle.chat_surface)
    try:
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
        # TK-269 (DEC-56a): a fresh DrainWake, constructed here (ON the running loop — asyncio
        # primitives bind to the loop they're created on) so an interactive chat enqueue wakes
        # the pump instead of it waiting out the idle beat. Wired ONLY into ChatSource (per the
        # ticket's frame: background pollers/gmail/gcal/feedback/asr and the Sweeper get NO wake)
        # and only when chat is actually enabled (``bundle.chat_source`` is ``None`` otherwise).
        wake = DrainWake()
        if bundle.chat_source is not None:
            bundle.chat_source.wake = wake.set
        # TK-230 (DEC-41): the drain pump SUBSUMES the old "one initial drive" — its first beat
        # drains whatever is already queued, exactly like the initial drive used to, but every
        # later beat keeps draining too (the bug this ticket fixes: a Done run's cancelled timers
        # meant nothing ever re-drove it after the first item).
        pump = _run_drain_pump(
            queue=bundle.queue,
            engine=bundle.engine,
            drain_pathway_id=bundle.drain_pathway_id,
            beat=_DRAIN_POLL_INTERVAL_SECONDS,
            wake=wake,
        )
        # TK-310/TK-313 (DEC-68(a)/(c)/(e)): the ambient-observability pollers ride the SAME
        # gather as the pump/Sweeper — constructed by assemble_runtime ONLY when their own
        # toggle is on, so a toggle-off boot adds nothing here (structural inertness holds).
        # Each poller's own run() loop never raises except on cancellation (poll_once/process_
        # beat are internally guarded — see observe_screen.py/observe_mic.py), so this never
        # changes the pump/Sweeper's own failure behavior.
        drivers: list[Coroutine[Any, Any, None]] = [
            pump,
            sweeper.run_forever(
                interval=timedelta(seconds=params.sweeper_interval_seconds),
                lease_ttl=timedelta(seconds=params.sweeper_lease_ttl_seconds),
            ),
        ]
        if bundle.screen_collector is not None:
            drivers.append(bundle.screen_collector.run())
        if bundle.mic_probe is not None:
            drivers.append(bundle.mic_probe.run())
        await asyncio.gather(*drivers)
    finally:
        await bundle.source_registry.stop()
        await _stop_chat_surface(bundle.chat_surface)
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
        # TK-310/TK-313 (DEC-68(a)/(c)/(e)): best-effort close — each poller's own run() already
        # closes any open segment on its own CancelledError, so this is belt-and-suspenders for
        # the non-cancellation exit paths (mirrors action_trail_writer's None-safe posture above).
        if bundle.screen_collector is not None:
            bundle.screen_collector.close()
        if bundle.mic_probe is not None:
            bundle.mic_probe.close()
        # Batch-review repair: ObservationStore owns its own lazy psycopg connection (TK-310,
        # Q-46) — closed best-effort beside the other stores above; None (a no-op) unless a
        # wombat_observe_* toggle constructed it at assemble_runtime time.
        if bundle.observation_store is not None:
            bundle.observation_store.close()


async def serve() -> None:
    """Boot wombat as ONE standing process: assemble the composition, then start/drive/stop it.

    ``check_config(config)`` runs FIRST, right after ``load_config()`` (TK-150, Q-87 ruling 4) —
    the same-host storage-residency guard refuses (``RemoteStorageConfigError``, naming the
    offending config key) before anything else, including the ``WOMBAT_PG_DSN``-required check
    below.

    Requires ``WOMBAT_PG_DSN`` (Q-36: the queue is pg-only) — fails loud with
    ``ConfigurationError`` naming it when absent, rather than starting silently broken. It is
    deliberately NOT part of ``REQUIRED_ENV`` so tests and the demo stay bootable without it.

    ``resolve_wombat_zone(config)`` (TK-228, DEC-40) is resolved EXACTLY ONCE here and threaded
    explicitly into ``assemble_runtime`` — the ONE place a real wall-clock zone enters the
    composition, so the brief timer, the daily/dream civil-day boundary, and every other tz
    consumer downstream agree on the SAME zone (never a caller independently defaulting to UTC).

    ``settings_store.import_legacy_settings_file(dsn)`` (TK-240, DEC-44) runs immediately AFTER
    ``assemble_runtime`` (whose schema pre-flight already created ``wombat_settings``) and BEFORE
    ``_drive_and_serve`` — this is one of exactly TWO production call sites ever (DEC-44), the
    other being the ``settings_app`` ``__main__`` entry point (TK-242). ``load_config()`` above has
    already run, so a fresh legacy import's non-persona fields ride defaults/env until the next
    restart (v2.58 ruling (b), deliberately ACCEPTED — this call is never moved ahead of
    ``load_config``); persona rows heal on the first Sweeper beat (TK-243).

    ``bundle.external_item_store.prune_older_than(EXTERNAL_ITEMS_PRUNE_DAYS)`` (TK-245, ruling
    v2.68 r5) runs exactly ONCE here, guarded on the field being non-``None`` — ``assemble_runtime``
    always constructs it on this ``dsn``-required path, so the guard is defensive (a hand-rolled
    ``RuntimeBundle`` elsewhere may leave it ``None``).

    ``bundle.scratchpad_store.purge_stale(SCRATCHPAD_PURGE_DAYS)`` (TK-247, DEC-46, ruling v2.68
    r5) runs exactly ONCE here, guarded the SAME way — non-``None`` on every real boot, defensive
    against a hand-rolled store-less ``RuntimeBundle``.

    ``bundle.chat_turn_store.purge_older_than(7)`` (TK-295, DEC-65e, ruling v2.159 r1) runs
    exactly ONCE here, guarded the SAME way — non-``None`` on every real boot, defensive against a
    hand-rolled store-less ``RuntimeBundle``. The 7-day window is the SAME pinned retention
    ``chat_turns._RETENTION_DAYS`` documents; passed as a literal here per the ruling (no shared
    importable constant, mirroring this ticket's own "no knob" pin).

    ``bundle.observation_store.prune_older_than(21)`` (TK-310, DEC-68(a)/(c)) runs exactly ONCE
    here, guarded the SAME way — non-``None`` ONLY when ``config.wombat_observe_screen`` was true
    at ``assemble_runtime`` time (structural inertness: the toggle off leaves this field ``None``,
    so this line is a no-op then). The 21-day window is the SAME pinned retention
    ``observations._OBSERVATION_RETENTION_DAYS`` documents; passed as a literal here per the SAME
    chat_turn_store-precedent ruling (no shared importable constant).

    ``_read_params_overlay(dsn)`` (TK-302, DEC-67(d), ruling v2.172 r3) runs strictly BETWEEN
    ``resolve_wombat_zone(config)`` and ``load_operating_params()`` — ``dsn`` is already validated
    loud above. It reads the EIGHT ``wombat_param_*`` ``wombat_settings`` rows and feeds them to
    ``load_operating_params`` as its overlay; ANY read failure (including a first-ever boot, before
    ``assemble_runtime``'s schema preflight below has ever created the table) degrades to an empty
    overlay plus one WARNING rather than blocking boot.
    """
    config = load_config()
    check_config(config)
    dsn = config.wombat_pg_dsn
    if not dsn:
        raise ConfigurationError(
            "missing required environment variable WOMBAT_PG_DSN; wombat will not start"
        )
    tz = resolve_wombat_zone(config)
    params = load_operating_params(overlay=_read_params_overlay(dsn))
    bundle = assemble_runtime(config=config, dsn=dsn, params=params, tz=tz)
    import_legacy_settings_file(dsn)
    if bundle.external_item_store is not None:
        bundle.external_item_store.prune_older_than(EXTERNAL_ITEMS_PRUNE_DAYS)
    if bundle.scratchpad_store is not None:
        bundle.scratchpad_store.purge_stale(SCRATCHPAD_PURGE_DAYS)
    if bundle.chat_turn_store is not None:
        bundle.chat_turn_store.purge_older_than(7)
    if bundle.observation_store is not None:
        bundle.observation_store.prune_older_than(21)
    await _drive_and_serve(bundle, params=params)


__all__ = ["serve"]

"""wombat.persona.live — LivePersona: the ONE composition-root-owned runtime authority for the
current PersonaMatrix (TK-209, EP-33, DEC-34 Jim authority + DEC-37(g); storage tier ported to
Postgres by TK-243, DEC-43).

Single-process asyncio (ASMP-2's precedent — exactly one draining WombatQueue process-wide) — a
plain attribute swap IS the atomicity story for the in-memory matrix; no lock, no async
coordination. NOT a voice feature: a voice-off or default-config boot constructs this identically
to any other boot (``matrix_from_config(config)`` returns ``DEFAULT_MATRIX`` absent any persona
env/settings override).

``instruction(mouth)`` delegates to the pure TK-207 ``instruction_for`` builder over the CURRENT
matrix — the four mouth call sites (``ComposeStage``, ``BriefComposeStage``, ``DraftComposer``,
``ReflectionComposeStage``) read this at RENDER time each turn via an OPTIONAL constructor arg
(TK-209), so a matrix change lands on the NEXT rendered turn, no restart.

TK-243 (DEC-43): the constructor takes an optional ``SettingsStore`` (``wombat.settings_store``,
TK-240) in place of a settings-file path. Construction is FULLY LAZY (BINDING v2.61 ruling 2) —
ZERO store I/O happens here even when a store is given, matching ``assemble_runtime``'s documented
invariant (every adapter lazy, exactly one eager Postgres touch). A store-less instance (tests,
the demo) runs fully in-memory: persistence is honestly absent, logged ONCE (loud) at
construction, and every write path degrades to a no-op rather than crashing.

``set(matrix)`` swaps the in-memory matrix FIRST, then best-effort persists ONLY the five
``wombat_persona_*`` keys plus ``wombat_persona_pins`` via ``store.put()`` — a key-level upsert,
so the old read-modify-write is unnecessary by construction (every other row in the table is left
untouched). ``store.put`` only ever raises ``SecretFieldRefused`` for a ``SecretStr``-typed key,
which none of these six keys are. A persistence failure (a raising store, a store-less instance
degrading silently, ...) leaves the in-memory matrix applied regardless — ``set()`` never raises
into the drain loop (CON-3) — and logs EXACTLY ONE loud WARNING naming the failure.

``poll_settings()`` (DEC-37(g), retargeted by TK-243) is the app-edit hot-apply seam: ONE small
``store.get_all()`` per Sweeper beat (the table only ever holds admitted keys, so this is cheap),
value-diffed against what this instance last observed — no file, no mtime. The FIRST poll after
boot is special: since construction never touched the store, this beat unconditionally hydrates
the in-memory matrix and pins from whatever the table holds (first-beat persona healing, the
accepted v2.58 ruling (b) posture — a fresh legacy import's persona rows heal on this beat rather
than at boot). Every later beat is a no-op unless the six keys differ from that last-observed
snapshot; a real diff is treated as an app edit exactly like a poll-detected TK-200 UI edit always
was — reconciled, pin-stamped, and best-effort re-persisted (mirroring ``set()``'s own guarded
write). This method NEVER raises (CON-3) — it rides ``wombat.runtime``'s existing Sweeper clock
beat (``runtime.py``'s ``clock=`` callable), so a raise here would break the standing sweep. A
failed read/apply warns ONCE PER FAILURE STREAK (consecutive failing beats; a success resets the
guard) and retries next beat — the TK-227 malformed-generation retry collapses naturally into this
single guard, since there is no longer a separate mtime cursor to defer.

TK-214 (DEC-36/DEC-37(h), Q-112(f)) adds the explicit-set PIN mechanics the nightly
``dream_persona`` tuner reads via ``pinned_axes()``: pins live under the ``wombat_persona_pins``
key in the SAME table, upserted alongside the five persona keys by the SAME ``_persist`` call —
``{axis_name: aware-UTC ISO timestamp}``. ``set()`` has a keyword-only ``explicit: bool = True``
(the TK-212 voice-command call site stays byte-untouched, since it never passes this kwarg): an
explicit set stamps a pin (``now``, UTC) for exactly the axes whose level CHANGED vs the pre-swap
matrix; ``set(explicit=False)`` (the dream nudge path, TK-214) stamps NOTHING. A successful
persist (from either ``set()`` or a poll-triggered reconcile) updates this instance's own
last-observed snapshot to match what it just wrote — the same precedent the old mtime cursor
served: an own write is never mistaken by the NEXT poll for an external app edit (critical for the
dream-nudge path, which must never accidentally acquire a pin). ``pinned_axes(now)`` returns the
axes pinned within the last ``PERSONA_PIN_DAYS`` days.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from wombat.persona.builder import Mouth, instruction_for
from wombat.persona.matrix import PersonaMatrix, from_strings, to_strings
from wombat.settings_store import SettingsStore

logger = logging.getLogger(__name__)

# The five app-editable persona keys' wire names in wombat_settings (a fixed subset of
# wombat.config.APP_EDITABLE_FIELDS) — the ONLY keys this class ever reads or writes (plus the
# TK-214 pins key below, its own dedicated non-persona-axis key).
_PERSONA_KEYS: tuple[str, ...] = (
    "wombat_persona_brevity",
    "wombat_persona_warmth",
    "wombat_persona_directness",
    "wombat_persona_humor",
    "wombat_persona_proactivity",
)
_PERSONA_KEY_PREFIX = "wombat_persona_"

# The five PersonaMatrix axis field names — the pin-tracking unit (TK-214).
_AXIS_NAMES: tuple[str, ...] = ("brevity", "warmth", "directness", "humor", "proactivity")

# The wire key pins persist under in wombat_settings (TK-214, DEC-37(h), Q-112(f)).
_PERSONA_PINS_KEY = "wombat_persona_pins"

# A pin stamped more than this many days ago no longer blocks the nightly tuner (TK-214).
PERSONA_PIN_DAYS = 7


class LivePersona:
    """The ONE runtime authority for the current PersonaMatrix (TK-209). See module docstring."""

    def __init__(
        self,
        initial_matrix: PersonaMatrix,
        assistant_name: str,
        store: SettingsStore | None = None,
    ) -> None:
        self._matrix = initial_matrix
        self._assistant_name = assistant_name
        self._store = store
        # TK-243: pins are NOT loaded here (fully lazy construction) — they hydrate on the first
        # poll_settings() beat, same as the matrix's persisted axes.
        self._pins: dict[str, str] = {}
        # The value-diff cursor: None until the first successful poll (or the first successful
        # persist) — its own last-observed snapshot of the six store keys, in wire-string form.
        self._last_values: dict[str, Any] | None = None
        self._poll_fail_streak_warned = False
        if store is None:
            logger.warning(
                "LivePersona: constructed without a SettingsStore — persona axis/pin changes "
                "apply in-memory only for this process and will NOT survive a restart"
            )

    @property
    def matrix(self) -> PersonaMatrix:
        """The CURRENT matrix (read-only) — e.g. the TK-215 gate-side seam reads proactivity
        here at scoring time."""
        return self._matrix

    def instruction(self, mouth: Mouth) -> str:
        """Render ``mouth``'s system instruction from the CURRENT matrix (TK-207's pure builder,
        evaluated fresh on every call) — the render-time read the four mouth call sites use."""
        return instruction_for(mouth, self._matrix, self._assistant_name)

    def set(self, matrix: PersonaMatrix, *, explicit: bool = True) -> None:
        """Swap the in-memory matrix, then best-effort persist it. See module docstring.

        ``explicit`` (TK-214, default ``True`` — the TK-212 voice-command call site stays
        byte-untouched) stamps a pin (``now``, aware UTC) for exactly the axes whose level
        CHANGED vs the pre-swap matrix. ``explicit=False`` (the dream nudge path, TK-214) stamps
        NOTHING.
        """
        before = self._matrix
        self._matrix = matrix
        if explicit:
            changed = [
                axis for axis in _AXIS_NAMES if getattr(before, axis) != getattr(matrix, axis)
            ]
            if changed:
                now_iso = datetime.now(UTC).isoformat()
                for axis in changed:
                    self._pins[axis] = now_iso
        try:
            self._persist(matrix)
        except Exception:
            logger.warning(
                "LivePersona.set: failed to persist the persona matrix to the settings store; "
                "the in-memory matrix is still applied, but the change will not survive a "
                "restart",
                exc_info=True,
            )
        else:
            # An own successful write is never mistaken by the NEXT poll for an external app
            # edit (critical for the explicit=False dream-nudge path — see module docstring).
            self._last_values = self._local_snapshot()

    def pinned_axes(self, now: datetime) -> frozenset[str]:
        """Axes explicitly set within the last ``PERSONA_PIN_DAYS`` days (TK-214) — the nightly
        ``dream_persona`` tuner's custody boundary: a pinned axis never receives a nudge step
        regardless of its in-window feedback signal. A malformed stamp is silently skipped
        (never raises)."""
        cutoff = now - timedelta(days=PERSONA_PIN_DAYS)
        pinned: set[str] = set()
        for axis, stamped_at in self._pins.items():
            try:
                parsed = datetime.fromisoformat(stamped_at)
            except ValueError:
                continue
            if parsed >= cutoff:
                pinned.add(axis)
        return frozenset(pinned)

    def poll_settings(self) -> None:
        """ONE small ``store.get_all()`` per Sweeper beat (TK-243, DEC-37(g)) — value-diffed
        against this instance's last-observed snapshot. NEVER raises (CON-3); rides the existing
        Sweeper clock beat (``runtime.py``). See module docstring for the first-beat hydration
        and failure-streak semantics.
        """
        if self._store is None:
            return  # no persistence configured (construction already warned once) — nothing to poll
        try:
            values = self._store.get_all()
            self._apply_poll(values)
        except Exception:
            if not self._poll_fail_streak_warned:
                logger.warning(
                    "LivePersona.poll_settings: failed to read/apply persona settings from the "
                    "store; the current in-memory matrix stands, retrying on the next beat",
                    exc_info=True,
                )
                self._poll_fail_streak_warned = True
            return
        self._poll_fail_streak_warned = False  # a success resets the guard

    def _apply_poll(self, values: dict[str, Any]) -> None:
        """The successful half of ``poll_settings`` — first-beat hydrate, or a value-diffed
        reconcile on every later beat."""
        if self._last_values is None:
            self._hydrate(values)
            self._last_values = self._snapshot_from_store(values)
            return
        if self._snapshot_from_store(values) == self._last_values:
            return  # no-op poll — nothing in the six keys changed since we last looked
        self._reconcile(values)

    def _hydrate(self, values: dict[str, Any]) -> None:
        """First-beat healing (BINDING v2.61 ruling 2): adopt whichever of the five persona keys
        and the pins map are present in ``values`` wholesale — absent/malformed pieces leave the
        construction-time defaults standing, NEVER raises past a bad axis value (that propagates
        to ``poll_settings``'s own guard, which retries next beat)."""
        bare = to_strings(self._matrix)
        found_any = False
        for key in _PERSONA_KEYS:
            if key in values:
                bare[key.removeprefix(_PERSONA_KEY_PREFIX)] = values[key]
                found_any = True
        if found_any:
            self._matrix = from_strings(bare)
        pins = values.get(_PERSONA_PINS_KEY)
        if isinstance(pins, dict):
            self._pins = {
                axis: stamped_at
                for axis, stamped_at in pins.items()
                if isinstance(axis, str) and isinstance(stamped_at, str)
            }

    def _reconcile(self, values: dict[str, Any]) -> None:
        """A post-hydration value diff (TK-214: an app edit) — reload whichever persona keys are
        present, stamp + best-effort persist a pin for every axis that actually changed, and
        leave ``self._last_values`` reflecting the store's ACTUAL resulting state (the local
        post-write snapshot on a successful persist, or the just-read snapshot on a failed one —
        the same distinction the old mtime cursor's re-stat captured for free)."""
        bare = to_strings(self._matrix)
        found_any = False
        for key in _PERSONA_KEYS:
            if key in values:
                bare[key.removeprefix(_PERSONA_KEY_PREFIX)] = values[key]
                found_any = True
        if not found_any:
            self._last_values = self._snapshot_from_store(values)
            return  # only the pins key differed — nothing on the axis side to reconcile
        reloaded = from_strings(bare)
        changed = [
            axis for axis in _AXIS_NAMES if getattr(self._matrix, axis) != getattr(reloaded, axis)
        ]
        self._matrix = reloaded
        if not changed:
            self._last_values = self._snapshot_from_store(values)
            return
        now_iso = datetime.now(UTC).isoformat()
        for axis in changed:
            self._pins[axis] = now_iso
        try:
            self._persist(self._matrix)
        except Exception:
            logger.warning(
                "LivePersona.poll_settings: failed to persist pins for an app-detected explicit "
                "edit (axes=%s); the in-memory matrix/pins still apply",
                changed,
                exc_info=True,
            )
            self._last_values = self._snapshot_from_store(values)
        else:
            self._last_values = self._local_snapshot()

    def _snapshot_from_store(self, values: dict[str, Any]) -> dict[str, Any]:
        """The subset of ``values`` this instance tracks, keyed exactly like ``_local_snapshot``
        so the two are directly comparable."""
        return {key: values[key] for key in (*_PERSONA_KEYS, _PERSONA_PINS_KEY) if key in values}

    def _local_snapshot(self) -> dict[str, Any]:
        """This instance's CURRENT matrix + pins, in the same wire shape ``_persist`` writes —
        what the store looks like after a successful write of it."""
        bare = to_strings(self._matrix)
        snapshot: dict[str, Any] = {
            key: bare[key.removeprefix(_PERSONA_KEY_PREFIX)] for key in _PERSONA_KEYS
        }
        snapshot[_PERSONA_PINS_KEY] = dict(self._pins)
        return snapshot

    def _persist(self, matrix: PersonaMatrix) -> None:
        """Upsert ONLY the five persona keys plus ``wombat_persona_pins`` via ``store.put()`` — a
        no-op for a store-less instance (persistence honestly absent, warned once at
        construction)."""
        if self._store is None:
            return
        bare = to_strings(matrix)
        mapping: dict[str, Any] = {
            key: bare[key.removeprefix(_PERSONA_KEY_PREFIX)] for key in _PERSONA_KEYS
        }
        mapping[_PERSONA_PINS_KEY] = dict(self._pins)
        self._store.put(mapping)


__all__ = ["PERSONA_PIN_DAYS", "LivePersona"]

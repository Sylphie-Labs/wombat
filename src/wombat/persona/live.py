"""wombat.persona.live — LivePersona: the ONE composition-root-owned runtime authority for the
current PersonaMatrix (TK-209, EP-33, DEC-34 Jim authority + DEC-37(g)).

Single-process asyncio (ASMP-2's precedent — exactly one draining WombatQueue process-wide) — a
plain attribute swap IS the atomicity story for the in-memory matrix; no lock, no async
coordination. NOT a voice feature: a voice-off or default-config boot constructs this identically
to any other boot (``matrix_from_config(config)`` returns ``DEFAULT_MATRIX`` absent any persona
env/settings override).

``instruction(mouth)`` delegates to the pure TK-207 ``instruction_for`` builder over the CURRENT
matrix — the four mouth call sites (``ComposeStage``, ``BriefComposeStage``, ``DraftComposer``,
``ReflectionComposeStage``) read this at RENDER time each turn via an OPTIONAL constructor arg
(TK-209), so a matrix change lands on the NEXT rendered turn, no restart. ``instruction_for`` itself
now routes through the TK-219 ``wombat.persona.expression.render_expression`` seam internally (a
``ClauseAlgebraStrategy`` over ``EMPTY_CUES``) — this class's own signature and behavior are
unchanged by that refactor; every mouth reroute stays byte-identical.

``set(matrix)`` swaps the in-memory matrix, THEN best-effort persists ONLY the five
``wombat_persona_*`` keys to the ``wombat.settings.json`` tier via a read-modify-write — every
other key already in the file is preserved verbatim; never ``.env``, never a secret field. A
persistence failure (a read-only path, a monkeypatched write, malformed existing JSON, ...) leaves
the in-memory matrix applied regardless — ``set()`` never raises into the drain loop (CON-3) — and
logs EXACTLY ONE loud WARNING naming the failure.

``poll_settings_file()`` (DEC-37(g)) is the app-edit hot-apply seam: a cheap ``os.stat`` mtime
check on the settings file. On a changed mtime, it reloads whichever of the five persona keys are
PRESENT in the file (any key absent keeps its current in-memory value — a partial app-edit is
never fatal) and swaps the matrix. This method NEVER raises (CON-3) — it rides
``wombat.runtime``'s existing Sweeper clock beat (``runtime.py``'s ``clock=`` callable), so a raise
here would break the standing sweep. Last-write-wins on this single-user local file is the accepted
DEC-37(g) posture — no lock, no merge beyond the plain key-level overlay above.

TK-227: the mtime cursor advances ONLY after a successful generation — a vanished file (``mtime is
None``, a legitimate observation) or a reload that completed (including a valid dict that carries
none of the five persona keys — still a successful read). A malformed generation — any exception
during read/parse/apply, OR a non-dict top level (JSON that parses to e.g. a list or scalar) — is
now classified as malformed and leaves the cursor standing, so the NEXT Sweeper beat retries rather
than the edit being silently and permanently dropped. A ``_last_warned_mtime`` guard keeps a
persistently malformed file's WARNING to once per failing mtime generation; a subsequent DIFFERENT
mtime re-warns.

TK-214 (DEC-36/DEC-37(h), Q-112(f)) adds the explicit-set PIN mechanics the nightly
``dream_persona`` tuner reads via ``pinned_axes()``: pins live under the ``wombat_persona_pins``
key in the SAME ``wombat.settings.json`` tier — ``{axis_name: aware-UTC ISO timestamp}`` — loaded
best-effort at construction (absent/malformed = no pins, NEVER raises) and persisted alongside the
five persona keys in the SAME read-modify-write (``_persist``), never a second file/seam.
``set()`` gains a keyword-only ``explicit: bool = True`` (the TK-212 voice-command call site stays
byte-untouched, since it never passes this kwarg): an explicit set stamps a pin (``now``, UTC) for
exactly the axes whose level CHANGED vs the pre-swap matrix; ``set(explicit=False)`` (the dream
nudge path, TK-214) stamps NOTHING. Because ``set()`` already advances the mtime cursor past its
own write, a nudge is never mistaken by the next poll for an app edit. ``poll_settings_file``
mirrors this: a reloaded axis whose level DIFFERS from the current in-memory value is itself
classified as an explicit app edit (the TK-200 UI path) — its pin is stamped and best-effort
persisted via the same guarded write, and the cursor advances past THAT write too (the ``set()``
else-branch precedent), keeping the TK-227 malformed-generation cursor-defer discipline intact for
the read/parse step itself. ``pinned_axes(now)`` returns the axes pinned within the last
``PERSONA_PIN_DAYS`` days.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wombat.config import WOMBAT_SETTINGS_FILE
from wombat.persona.builder import Mouth, instruction_for
from wombat.persona.matrix import PersonaMatrix, from_strings, to_strings

logger = logging.getLogger(__name__)

# The five app-editable persona keys' wire names in wombat.settings.json (a fixed subset of
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

# The wire key pins persist under in wombat.settings.json (TK-214, DEC-37(h), Q-112(f)).
_PERSONA_PINS_KEY = "wombat_persona_pins"

# A pin stamped more than this many days ago no longer blocks the nightly tuner (TK-214).
PERSONA_PIN_DAYS = 7


class LivePersona:
    """The ONE runtime authority for the current PersonaMatrix (TK-209). See module docstring."""

    def __init__(
        self,
        initial_matrix: PersonaMatrix,
        assistant_name: str,
        settings_path: str = WOMBAT_SETTINGS_FILE,
    ) -> None:
        self._matrix = initial_matrix
        self._assistant_name = assistant_name
        self._settings_path = Path(settings_path)
        self._pins: dict[str, str] = self._load_pins()
        self._last_mtime = self._current_mtime()
        self._last_warned_mtime: float | None = None

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
                "LivePersona.set: failed to persist the persona matrix to %s; the in-memory "
                "matrix is still applied, but the change will not survive a restart",
                self._settings_path,
                exc_info=True,
            )
        else:
            self._last_mtime = self._current_mtime()

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

    def poll_settings_file(self) -> None:
        """Cheap mtime check (DEC-37(g)) — reload + swap on change. NEVER raises (CON-3); rides
        the existing Sweeper clock beat (``runtime.py``). TK-227: the cursor advances only after
        a successful generation (or an observed vanished file) — a malformed generation leaves it
        standing so the next beat retries instead of silently dropping the edit forever.

        TK-214: a reloaded axis whose level DIFFERS from the current in-memory value counts as an
        explicit app edit (the TK-200 UI path) — its pin is stamped and best-effort persisted via
        the SAME guarded ``_persist`` write, and the cursor advances past THAT write too (the
        ``set()`` else-branch precedent).
        """
        mtime = self._current_mtime()
        if mtime == self._last_mtime:
            return
        if mtime is None:
            self._last_mtime = mtime  # the file vanished — a legitimate observation
            return  # keep the current in-memory matrix
        try:
            loaded = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("settings file top level is not a JSON object")
            bare = to_strings(self._matrix)
            found_any = False
            for key in _PERSONA_KEYS:
                if key in loaded:
                    bare[key.removeprefix(_PERSONA_KEY_PREFIX)] = loaded[key]
                    found_any = True
            if found_any:
                reloaded = from_strings(bare)
                changed = [
                    axis
                    for axis in _AXIS_NAMES
                    if getattr(self._matrix, axis) != getattr(reloaded, axis)
                ]
                self._matrix = reloaded
                if changed:
                    now_iso = datetime.now(UTC).isoformat()
                    for axis in changed:
                        self._pins[axis] = now_iso
                    try:
                        self._persist(self._matrix)
                    except Exception:
                        logger.warning(
                            "LivePersona.poll_settings_file: failed to persist pins for an "
                            "app-detected explicit edit (axes=%s) to %s; the in-memory "
                            "matrix/pins still apply",
                            changed,
                            self._settings_path,
                            exc_info=True,
                        )
        except Exception:
            if mtime != self._last_warned_mtime:
                logger.warning(
                    "LivePersona.poll_settings_file: failed to reload the persona matrix from "
                    "%s; the current in-memory matrix stands, retrying on the next poll",
                    self._settings_path,
                    exc_info=True,
                )
                self._last_warned_mtime = mtime
        else:
            # advance the cursor ONLY on a successful generation — re-read fresh so a pin-persist
            # write above (which itself bumps the file's mtime) is never mistaken for a NEW
            # external edit on the next poll (the set() else-branch precedent).
            self._last_mtime = self._current_mtime()

    def _current_mtime(self) -> float | None:
        try:
            return self._settings_path.stat().st_mtime
        except OSError:
            return None

    def _load_pins(self) -> dict[str, str]:
        """Best-effort load of the TK-214 pins map at construction — absent/malformed always
        yields no pins, NEVER raises."""
        try:
            if not self._settings_path.exists():
                return {}
            loaded = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return {}
            pins = loaded.get(_PERSONA_PINS_KEY)
            if not isinstance(pins, dict):
                return {}
            return {
                axis: stamped_at
                for axis, stamped_at in pins.items()
                if isinstance(axis, str) and isinstance(stamped_at, str)
            }
        except Exception:
            return {}

    def _persist(self, matrix: PersonaMatrix) -> None:
        """Read-modify-write ``wombat.settings.json``: preserve every other key, write ONLY the
        five persona keys plus the TK-214 ``wombat_persona_pins`` key (never ``.env``, never a
        secret field — this class never touches either)."""
        existing: dict[str, Any] = {}
        if self._settings_path.exists():
            loaded = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        bare = to_strings(matrix)
        for key in _PERSONA_KEYS:
            existing[key] = bare[key.removeprefix(_PERSONA_KEY_PREFIX)]
        existing[_PERSONA_PINS_KEY] = dict(self._pins)
        self._settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


__all__ = ["PERSONA_PIN_DAYS", "LivePersona"]

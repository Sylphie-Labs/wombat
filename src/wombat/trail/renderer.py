"""ActionTrailRenderer — appends the human-auditable ``wombat-trail.log`` (TK-147, EP-27,
Q-63/Q-89).

Renders ``action_trail_projection`` rows (via ``ActionTrailReader``, Q-63 seam) into a
plain-language, APPEND-ONLY text log (CON-4 posture: human-pure, no dashboards, NG-3). The
log itself carries NO ``action_id``s and NO JSON — every line is a self-identifying,
plain-language sentence (Q-89 ruling 1). The log file is ONLY EVER opened in append mode and
is never truncated or mutated — a lost sidecar with a surviving log re-renders duplicates on
the next pass; this is a documented, honest failure mode, not a bug (Q-89 ruling 2).

Line formats (Q-89 ruling 1), ``action_type``/``human_summary`` verbatim from the row:
  proposed (new PENDING row):   ``[PROPOSED <proposed_at ISO>] <action_type>: <human_summary>``
  blocked (new BLOCKED row,
    rendered ONCE, absorbing):  ``[BLOCKED <proposed_at ISO>] <action_type>: <human_summary>``
  dispatched (transition,
    indented, appended at
    the tail):                  ``  [DISPATCHED <dispatched_at ISO>] <action_type>: ...``
  cancelled (transition,
    indented, appended at
    the tail):                  ``  [CANCELLED <cancelled_at ISO>] <action_type>: ...``

Dedup cursor (Q-89 ruling 2): a sidecar JSON file next to the log (``<log_path>.sidecar.json``
by default derivation), mapping ``action_id -> last rendered status``. ``render()`` is the ONE
public pass: read rows by ``seq``, diff each row's CURRENT status against the sidecar's last
recorded status for that ``action_id``, append whatever lines are newly implied, then persist
the updated sidecar. A row never before rendered may already be past PENDING/BLOCKED by the
time it is first observed (e.g. a renderer restart that skipped a full pending->dispatched
cycle) — in that case BOTH the origin line (PROPOSED, or BLOCKED if the row is blocked) and the
transition line are appended together in one pass, so no history is silently lost. BLOCKED and
any already-terminal (DISPATCHED/CANCELLED) row never emits again — the writer enforces these
statuses as absorbing (``wombat.trail.writer``), so this renderer never re-checks them.

NO polling loop, NO daemon — a later consumer drives ``render()`` (Q-89 ruling 3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

from wombat.trail.reader import ActionTrailReader
from wombat.trail.schema import TrailRow

__all__ = ["ActionTrailRenderer"]

_DEFAULT_LOG_PATH = "wombat-trail.log"

_STATUS_PENDING = "pending"
_STATUS_BLOCKED = "blocked"
_STATUS_DISPATCHED = "dispatched"
_STATUS_CANCELLED = "cancelled"


def _sidecar_path_for(log_path: Path) -> Path:
    """The sidecar cursor path, deterministically derived from ``log_path`` (Q-89 ruling 2)."""
    return log_path.with_name(log_path.name + ".sidecar.json")


def _origin_line(row: TrailRow) -> str:
    """The first line ever rendered for ``row``: PROPOSED, or BLOCKED for a refused action."""
    if row.status == _STATUS_BLOCKED:
        return f"[BLOCKED {row.proposed_at.isoformat()}] {row.action_type}: {row.human_summary}"
    return f"[PROPOSED {row.proposed_at.isoformat()}] {row.action_type}: {row.human_summary}"


def _transition_line(row: TrailRow, status: str) -> str:
    """The indented transition line for ``row`` moving to ``status`` (DISPATCHED/CANCELLED)."""
    if status == _STATUS_DISPATCHED:
        assert row.dispatched_at is not None
        at = row.dispatched_at.isoformat()
        label = "DISPATCHED"
    else:
        assert row.cancelled_at is not None
        at = row.cancelled_at.isoformat()
        label = "CANCELLED"
    return f"  [{label} {at}] {row.action_type}: {row.human_summary}"


class ActionTrailRenderer:
    """Appends the human-auditable trail log from ``action_trail_projection`` rows (Q-89)."""

    def __init__(
        self,
        dsn_or_conn: str | psycopg.Connection[Any],
        log_path: Path | str = _DEFAULT_LOG_PATH,
    ) -> None:
        self._reader = ActionTrailReader(dsn_or_conn)
        self._log_path = Path(log_path)
        self._sidecar_path = _sidecar_path_for(self._log_path)

    def close(self) -> None:
        """Release the reader's connection, if this renderer's reader opened it itself."""
        self._reader.close()

    def _load_sidecar(self) -> dict[str, str]:
        if not self._sidecar_path.exists():
            return {}
        with self._sidecar_path.open(encoding="utf-8") as fh:
            data: dict[str, str] = json.load(fh)
        return data

    def _save_sidecar(self, sidecar: dict[str, str]) -> None:
        with self._sidecar_path.open("w", encoding="utf-8") as fh:
            json.dump(sidecar, fh)

    def render(self) -> None:
        """One render pass: read rows by ``seq``, diff against the sidecar, append new lines.

        Newly-implied lines are appended to the log in a SINGLE append-mode open (never
        truncated), in ``seq`` order; the sidecar is rewritten only if anything was appended.
        A row whose current status already matches its sidecar entry contributes nothing.
        """
        rows = self._reader.rows()
        sidecar = self._load_sidecar()
        new_lines: list[str] = []

        for row in rows:
            last = sidecar.get(row.action_id)

            if last is None:
                new_lines.append(_origin_line(row))
                last = _STATUS_BLOCKED if row.status == _STATUS_BLOCKED else _STATUS_PENDING

            if last == _STATUS_PENDING and row.status in (_STATUS_DISPATCHED, _STATUS_CANCELLED):
                new_lines.append(_transition_line(row, row.status))
                last = row.status

            sidecar[row.action_id] = last

        if not new_lines:
            return

        # newline="" disables Python's universal-newline translation on write, so the log's
        # line endings are always a plain "\n" regardless of platform (Windows would otherwise
        # silently widen every "\n" to "\r\n", breaking byte-identity across append calls).
        with self._log_path.open("a", encoding="utf-8", newline="") as fh:
            for line in new_lines:
                fh.write(line + "\n")

        self._save_sidecar(sidecar)

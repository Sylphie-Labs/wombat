"""wombat.domain.item_identity — the ONE canonical item identity rule (TK-12, EP-2, Q-18/D).

Q-18/D: before this module, dedup keyed three different ways — TK-2's queue on a
freeform ``idempotency_key``, TK-72 (calendar) on a raw ``event_id``, TK-75 (Gmail) on a
raw ``message_id`` — so an ``OUTCOME_*`` claim written for a surfaced item had no single
stable identity to bind back to. This module fixes the ONE mapping: every source item is
identified by a pair ``(source_id, source_natural_id)`` — the source's own registration id
(``"calendar"``, ``"gmail"``, ``"presence"``, ...) plus whatever natural id that source
already uses (``event_id``, ``message_id``, an extracted-task id, a generated ephemeral
id) — and ``idempotency_key()`` is the ONE pure function that turns that pair into the
queue's stable string identity.

This ticket does not wire anyone up. It is a convention + a derivation function that
FUTURE tickets call: ``QueueItem`` will carry an ``ItemRef`` (TK-2's dedup path), the
gate's pending-set entries will key on it (TK-21), and an ``OUTCOME_*`` claim's subject
will carry it (TK-45/TK-49/TK-50) so an outcome always traces back to the exact source
item it rates. No store, no rewiring of ``queue.py`` — only the shared rule.

Design:
  * ``idempotency_key(source_id, source_natural_id)`` is PURE and TOTAL over any two
    strings. It length-prefixes ``source_id`` (a netstring-style encoding) before joining,
    so the two fields can never be reassembled into each other's boundary — a naive
    ``f"{source_id}:{source_natural_id}"`` join would let ``("a:b", "c")`` and
    ``("a", "b:c")`` collide; this encoding cannot.
  * ``ItemRef`` is a frozen dataclass pairing ``source_id`` with ``source_natural_id``; its
    ``.idempotency_key()`` method calls the same canonical function, so there is exactly
    one place the mapping is defined.
  * ``derive_task_natural_id`` / ``parent_natural_id_of_task`` are the minimal linking
    convention for an extracted-task item (TK-77): a task's natural id carries its parent
    message's natural id, so the task's ``ItemRef`` is distinct from (does not collide
    with) its parent's, but the parent is always recoverable from it.
  * ``new_ephemeral_natural_id`` generates a fresh natural id for ephemeral sources
    (presence/asr) that have no natural id of their own, so the derivation function stays
    total. It is impure (``uuid4``) by design — production code may call it freely, but
    tests must inject/pass an explicit natural id rather than asserting on its output
    (nondeterminism belongs in production, never in a test assertion).
  * ``split_idempotency_key`` (TK-111, Q-98) is the pure INVERSE of ``idempotency_key``:
    ``key -> (source_id, source_natural_id)``. It exists because ``idempotency_key`` is a
    length-prefixed (netstring-style) encoding, not a plain delimiter join, so recovering
    ``source_id`` requires reading that length prefix rather than a naive ``str.split``.
    The nightly behavioral event log (TK-111) is the first consumer: it stores the
    canonical ``idempotency_key`` as its primary key but also needs ``source_id`` as its
    own column, so it inverts the key rather than persisting a second copy of the pair.
    Raises ``ValueError`` on any key not built by ``idempotency_key`` — a caller writing a
    durable row must skip (never guess at) a malformed key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# The separator between the length-prefixed source_id and source_natural_id. Any string
# works here since the LENGTH prefix (not this separator) is what makes the encoding
# unambiguous; a readable separator just keeps keys inspectable in logs/DB rows.
_SEPARATOR = ":"

# The separator a task's natural id uses to carry its parent message's natural id
# (derive_task_natural_id / parent_natural_id_of_task). Chosen to be unlikely to collide
# with a real source natural id (event_id/message_id) prefix.
_TASK_LINK_SEPARATOR = "::task::"


def idempotency_key(source_id: str, source_natural_id: str) -> str:
    """THE canonical derivation: ``(source_id, source_natural_id) -> idempotency_key``.

    Pure and total over any two strings. Deterministic/stable: the same pair always
    derives the same key. Collision-free: two different pairs never derive the same key,
    even adversarially — ``source_id`` is length-prefixed (netstring-style) so the join
    point between the two fields can never be reinterpreted differently, which a plain
    ``f"{source_id}:{source_natural_id}"`` join would not guarantee.
    """
    return f"{len(source_id)}{_SEPARATOR}{source_id}{_SEPARATOR}{source_natural_id}"


def split_idempotency_key(key: str) -> tuple[str, str]:
    """THE pure inverse: ``idempotency_key -> (source_id, source_natural_id)`` (TK-111, Q-98).

    Reads the length prefix ``idempotency_key`` writes (``f"{len(source_id)}:{source_id}:
    {source_natural_id}"``) rather than a naive split on ``_SEPARATOR`` — ``source_id`` or
    ``source_natural_id`` may itself contain the separator character, and the length prefix is
    exactly what makes the encoding unambiguous despite that (see ``idempotency_key``'s own
    docstring). Round-trips: ``split_idempotency_key(idempotency_key(a, b)) == (a, b)`` for any
    two strings ``a``/``b``, including adversarial ones containing ``_SEPARATOR``.

    Raises ``ValueError`` (never guesses) if ``key`` was not built by ``idempotency_key`` —
    missing/non-digit length prefix, or the byte at the length offset isn't the separator.
    """
    prefix, found_separator, rest = key.partition(_SEPARATOR)
    if not found_separator or not prefix.isdigit():
        raise ValueError(
            f"split_idempotency_key: {key!r} has no valid length-prefix header "
            f"(expected '<len>{_SEPARATOR}<source_id>{_SEPARATOR}<source_natural_id>')"
        )
    length = int(prefix)
    if len(rest) < length + 1 or rest[length] != _SEPARATOR:
        raise ValueError(
            f"split_idempotency_key: {key!r} declares source_id length {length} but the "
            f"remainder does not have a {_SEPARATOR!r} at that offset — not a key produced by "
            f"idempotency_key()"
        )
    source_id = rest[:length]
    source_natural_id = rest[length + 1 :]
    return source_id, source_natural_id


@dataclass(frozen=True, slots=True)
class ItemRef:
    """A typed reference to one source item: ``(source_id, source_natural_id)``.

    Carried on every ``QueueItem`` and propagated onto the gate's pending-set entries and
    an outcome claim's subject by FUTURE tickets (TK-2/TK-21/TK-45/TK-49/TK-50) — this
    ticket only defines the type and its key. No motive/why field (CON-6/NG-1, non_goal).
    """

    source_id: str
    source_natural_id: str

    def idempotency_key(self) -> str:
        """The canonical key for this ref — delegates to the module-level function so
        there is exactly one place the ``(source_id, source_natural_id) -> key`` mapping
        is defined."""
        return idempotency_key(self.source_id, self.source_natural_id)


def derive_task_natural_id(parent_source_natural_id: str, task_local_id: str) -> str:
    """Build a natural id for a task extracted from a parent item (TK-77).

    The result carries ``parent_source_natural_id`` so the parent is always recoverable
    via ``parent_natural_id_of_task`` — the extracted-task identity is DISTINCT from (a
    different ``idempotency_key`` than) its parent's, but LINKABLE back to it.
    """
    return f"{parent_source_natural_id}{_TASK_LINK_SEPARATOR}{task_local_id}"


def parent_natural_id_of_task(task_natural_id: str) -> str:
    """Recover the parent item's natural id from a task natural id built by
    ``derive_task_natural_id``. Raises ``ValueError`` if it wasn't built that way."""
    if _TASK_LINK_SEPARATOR not in task_natural_id:
        raise ValueError(
            f"{task_natural_id!r} was not built by derive_task_natural_id() "
            f"(missing {_TASK_LINK_SEPARATOR!r})"
        )
    parent_natural_id, _, _ = task_natural_id.partition(_TASK_LINK_SEPARATOR)
    return parent_natural_id


def new_ephemeral_natural_id() -> str:
    """Generate a fresh natural id for an ephemeral source (presence/asr) that has no
    natural id of its own, so ``idempotency_key``/``ItemRef`` stay total over every
    source. IMPURE (``uuid4``) by design — production code may call this freely; TESTS
    must inject/pass an explicit natural id instead of asserting on this function's
    (nondeterministic) output."""
    return uuid.uuid4().hex


__all__ = [
    "ItemRef",
    "derive_task_natural_id",
    "idempotency_key",
    "new_ephemeral_natural_id",
    "parent_natural_id_of_task",
    "split_idempotency_key",
]

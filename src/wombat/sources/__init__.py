"""wombat.sources — production InputSource domain home (TK-3/TK-11).

``presence.py`` is the canonical home for the presence types (``PresenceState``,
``PresenceSnapshot``), the hardened OS idle reader, the pure ``classify``
function, and the ``make_presence_provider`` factory (Q-54). The pure hold
predicate itself lives in ``wombat.gate.presence_hold`` — it is a gate concern,
not a source concern.
"""

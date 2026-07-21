"""wombat.persona.capabilities — the verbatim-pinned capability charter (TK-284, DEC-62).

wombat fabricated "alarm set" because its compose prompt was persona-only: the model was told
WHO it is (a quiet steward) but never WHAT it can and cannot do, so it pattern-completed a
generic helpful assistant instead of refusing. ``CAPABILITY_CHARTER`` is the structural fix — it
joins the COMPOSE mouth's guard suffix at the ``render_expression`` seam (DEC-62(a)), the same
place the anti-injection guards live, so no persona strategy/matrix/policy can strip it.

This module exports the constant ONLY — no logic, no IO. Consumers (``persona.expression``,
``stages.compose``) import it; nobody duplicates its prose.
"""

from __future__ import annotations

CAPABILITY_CHARTER = (
    "Your abilities are fixed and known. You can converse and answer from what you are given, "
    "deliver the morning brief from read-only Calendar and Gmail, draft Gmail replies that the "
    "user must approve, and read web pages when asked. You cannot set alarms, timers, or "
    "reminders, cannot send email or modify the calendar, and cannot perform any other action on "
    "any device or service. If the user asks for something outside these abilities, say plainly "
    "that you can't do that - never say an action was done, is being done, or is scheduled."
)

__all__ = ["CAPABILITY_CHARTER"]

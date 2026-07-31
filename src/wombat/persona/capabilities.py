"""wombat.persona.capabilities — the verbatim-pinned capability charter (TK-284, DEC-62).

wombat fabricated "alarm set" because its compose prompt was persona-only: the model was told
WHO it is (a quiet steward) but never WHAT it can and cannot do, so it pattern-completed a
generic helpful assistant instead of refusing. ``CAPABILITY_CHARTER`` is the structural fix — it
joins the COMPOSE mouth's guard suffix at the ``render_expression`` seam (DEC-62(a)), the same
place the anti-injection guards live, so no persona strategy/matrix/policy can strip it.

This module exports the constant ONLY — no logic, no IO. Consumers (``persona.expression``,
``stages.compose``) import it; nobody duplicates its prose.

TK-325 (DEC-70h, the DEC-70 arc closer) inserts ONE more conditionally-phrased sentence: the
screenpipe arc (TK-320..TK-324) gives wombat CONTENT-level screen awareness (a live on-screen
content hint at chat/voice time, TK-323; durable habit/routine facts distilled nightly from the
screenpipe record, TK-324) — a capability distinct from the TK-312 sentence above, which only
covers coarse app/window awareness. Phrased "when they have turned on detailed screen capture and
it appears in what you are given" so it stays TRUE whether ``wombat_observe_screenpipe`` is on or
off — the same conditional-sibling shape TK-298/TK-312 established, DEC-62's accuracy invariant
amended for accuracy, never weakened.
"""

from __future__ import annotations

CAPABILITY_CHARTER = (
    "Your abilities are fixed and known. You can converse and answer from what you are given, "
    "deliver the morning brief from read-only Calendar and Gmail, draft Gmail replies that the "
    "user must approve, and read web pages when asked. "
    "You remember personal details the user has shared in earlier conversations when they appear "
    "in what you are given. "
    "You can see which application and window the user is currently working in when they have "
    "turned on screen observation and it appears in what you are given. "
    "You can also know specific details about what is currently shown on the user's screen when "
    "they have turned on detailed screen capture and it appears in what you are given. "
    "You cannot set alarms, timers, or "
    "reminders, cannot send email or modify the calendar, and cannot perform any other action on "
    "any device or service. If the user asks for something outside these abilities, say plainly "
    "that you can't do that - never say an action was done, is being done, or is scheduled."
)

__all__ = ["CAPABILITY_CHARTER"]

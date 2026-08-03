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

TK-354 (DEC-81, charter honesty for on-demand calendar and email) inserts a FOURTH sentence: the
charter told the model it can deliver the morning brief from Calendar and Gmail but never that the
same read-only grounding — today's calendar window and the five most recent email subjects/senders,
already riding every chat and voice turn via bootstrap.py's asr_context_hook — may be used to
answer an ordinary question asked in the moment, so a model following the charter literally
disclaimed a capability it was already holding live data for. Phrased "when they appear in what you
are given" so it stays TRUE on a boot with no Google connection, an empty calendar day, or the
CON-3 degrade, the same conditional-sibling shape TK-298/TK-312/TK-325 established, with an inline
bound — subjects and senders, not message bodies — because the projection genuinely carries no
body text.

TK-339 (DEC-78, DeviceSurface) inserts a FIFTH sentence: paired personal devices (a phone or
watch) can now carry a spoken message into the same reply pipeline and passively feed body-
activity data, gated on the two DEC-78(d) consent toggles (default off) and on a device actually
being paired. Phrased "when the user has enabled that and it appears in what you are given" so it
stays TRUE with both toggles off, with a toggle on but no device ever paired, and on every boot in
between — the same conditional-sibling shape TK-298/TK-312/TK-325/TK-354 established.
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
    "You can tell the user what is on their calendar today and which emails they have recently "
    "received - subjects and senders, not message bodies - when they appear in what you are given. "
    "You can also receive spoken messages and passive body-activity data from the user's own "
    "paired phone or watch when the user has enabled that and it appears in what you are given. "
    "You cannot set alarms, timers, or "
    "reminders, cannot send email or modify the calendar, and cannot perform any other action on "
    "any device or service. If the user asks for something outside these abilities, say plainly "
    "that you can't do that - never say an action was done, is being done, or is scheduled."
)

__all__ = ["CAPABILITY_CHARTER"]

"""One-time builder for scoring_fixture.real.yaml (DEC-24 / Q-38).

PROVENANCE: this script was run ONCE against a LOCAL read of ~50 real Google Calendar events
and 33 real Gmail threads (metadata-only view: sender, date, labels, thread length — NO
bodies were fetched). It extracts ONLY de-identified numeric SCORING FEATURES and a sender
priority CLASS (no names, no emails, no subjects). The output .real.yaml is gitignored
(NG-7 storage-residency: local read, no egress). The proxy label per event is computed by the
rubric in RUBRIC.md (vision gate: relevance AND importance AND user-state AND confidence;
quiet-by-default => default hold). Jim does a small confirmatory pass over these labels.

This builder is committed (it has no PII); the .real.yaml it emits is not.

Run:  .venv/Scripts/python.exe tests/gate/fixtures/_build_scoring_fixture.py
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

# "Now" anchor for time-to-event: the moment of the local pull (2026-06-21 ~16:45Z).
NOW = dt.datetime(2026, 6, 21, 16, 45, 0, tzinfo=dt.UTC)


def _hours(h: float) -> float:
    return h * 3600.0


# --- Calendar events (de-identified): all self-authored personal blocks. ---------------
# Each tuple: (hours_from_now_to_start, duration_min, surrounding_meeting_density).
# Density = number of other blocks within +-2h on that day (real value from the pull).
# These are SELF-class items; the gate treats personal calendar blocks as low-urgency unless
# imminent. We sample one representative day plus the imminent/near-term tail.
_CAL: list[tuple[float, int, float]] = [
    # Today/near-term tail (drives the few genuine surfacings).
    (-0.5, 30, 5.0),  # currently in a dense work block (past start)
    (0.25, 90, 5.0),  # next focus block, 15 min out
    (0.75, 45, 5.0),
    (1.5, 45, 5.0),
    (2.0, 90, 6.0),
    (3.5, 120, 4.0),
    (5.0, 60, 3.0),
    (7.0, 90, 5.0),
    # A representative full day (Mon) further out — all low time-pressure.
    (20.0, 30, 6.0),
    (20.5, 60, 6.0),
    (21.5, 90, 5.0),
    (23.0, 60, 4.0),
    (24.0, 120, 5.0),
    (25.0, 45, 5.0),
    (25.75, 45, 5.0),
    (27.0, 90, 4.0),
    (29.0, 120, 3.0),
    (44.0, 30, 6.0),
    (45.0, 90, 5.0),
    (47.0, 90, 4.0),
    (48.5, 120, 5.0),
    (50.0, 45, 5.0),
    (68.0, 30, 6.0),
    (69.0, 90, 5.0),
    (71.0, 90, 4.0),
    (72.5, 120, 5.0),
    (74.0, 45, 4.0),
    (92.0, 30, 5.0),
    (93.0, 90, 4.0),
    (95.0, 90, 3.0),
]


# --- Gmail threads (de-identified): (sender_class, thread_depth, hours_ago). ------------
# Mapped from the real pull. ALL inbound mail in the window was automated/transactional;
# there were no VIP/known-human direct emails. We add a small number of synthetic VIP/known
# items (clearly flagged) so the fixture exercises the surfacing arm — without them the day is
# trivially all-hold and proves nothing about the surface side. These are marked synthetic in
# the YAML provenance and labeled by the same rubric.
_MAIL_REAL: list[tuple[str, int, float]] = [
    ("automated", 1, 0.0),  # posthog product email
    ("automated", 5, 19.8),  # railway deploy notifications (thread of 5)
    ("automated", 1, 20.1),  # github noreply
    ("automated", 5, 24.7),  # railway (5)
    ("automated", 1, 41.3),  # github notifications (PR thread, cc)
    ("automated", 1, 41.3),
    ("automated", 1, 41.4),
    ("automated", 1, 41.6),
    ("automated", 1, 41.7),
    ("automated", 1, 41.9),
    ("automated", 1, 42.0),
    ("automated", 1, 42.1),
    ("automated", 1, 42.3),
    ("automated", 1, 42.5),
    ("automated", 1, 42.7),
    ("automated", 1, 42.8),
    ("automated", 1, 43.0),
    ("automated", 1, 44.9),  # railway newsletter
    ("automated", 5, 45.5),  # railway (5)
    ("automated", 1, 45.0),  # npm support
    ("automated", 1, 45.4),  # npm support
    ("automated", 1, 56.7),  # google search console
    ("automated", 1, 75.2),  # railway news
    ("automated", 1, 74.5),  # github noreply
    ("automated", 1, 110.6),  # npm
    ("automated", 1, 110.8),  # npm
    ("automated", 1, 113.3),  # npm
    ("transactional", 1, 134.6),  # stripe invoice/statement
    ("transactional", 1, 136.0),  # stripe-related
    ("automated", 5, 131.0),  # railway (5)
    ("automated", 1, 187.0),  # railway news
    ("automated", 1, 184.0),  # npm
    ("automated", 1, 159.0),  # posthog
]

# Synthetic human items (clearly flagged) to exercise the surface arm. The day of real mail
# is all-automated, so these are required to test the >=80%-agreement thesis on BOTH classes.
_MAIL_SYNTH: list[tuple[str, int, float]] = [
    ("vip", 1, 0.5),  # a recruiter/VIP direct reply, fresh
    ("vip", 4, 6.0),  # VIP thread mid-conversation
    ("known_human", 2, 2.0),  # known correspondent, recent
    ("known_human", 9, 30.0),  # deep known-human thread (high load)
    ("transactional", 1, 1.0),  # a statement that needs action soon
    ("known_human", 1, 50.0),  # known human, stale
]


def _proxy_label(urgency_val: float, load_val: float, sender_class: str) -> str:
    """Proxy human label derived from the vision gate (RUBRIC.md).

    surface iff (relevance AND importance) clear the bar AND quiet-by-default is overcome.
    Operationalized: a human-originated, sufficiently-urgent item surfaces; an automated item
    holds unless it is BOTH a real deadline AND imminent (which automated mail never is here).
    Default = hold (quiet-by-default).
    """
    human = sender_class in {"vip", "known_human", "transactional", "self"}
    # Importance gate: automated/marketing never clears importance on its own.
    if sender_class == "automated":
        return "hold"
    # SELF calendar blocks: surface only when imminent (the user already scheduled them; the
    # steward only nudges on the edge of the boundary).
    if sender_class == "self":
        return "surface" if urgency_val >= 0.78 else "hold"
    # Human mail: surface when urgency clears the bar; very deep threads are load-bearing too.
    if human and urgency_val >= 0.6:
        return "surface"
    return "hold"


def build() -> dict[str, object]:
    from wombat.gate.models import GateItem, ItemKind
    from wombat.gate.scoring import cognitive_load, urgency
    from wombat.rating.params import RatingParams

    # Identity params: base=0, gain=1 reproduce the spike's raw scores EXACTLY (Q-42), so the
    # emitted fixture labels stay bitwise-true after the ScoringParams -> RatingParams port.
    params = RatingParams(urgency_base=0.0, urgency_gain=1.0, load_base=0.0, load_gain=1.0)
    items: list[dict[str, object]] = []
    idx = 0

    for hrs, _dur, density in _CAL:
        sec = _hours(hrs)
        payload = {
            "is_timed": True,
            "seconds_to_event": sec,
            "sender_class": "self",
            "meeting_density": density,
            "thread_depth": 0,
        }
        gi = GateItem(item_id=f"cal-{idx}", item_kind=ItemKind.GENERIC, created_at=0.0,
                      payload=payload)
        u = round(urgency(gi, params), 4)
        load = round(cognitive_load(gi, params), 4)
        items.append({
            "item_id": f"cal-{idx}",
            "source": "calendar",
            "synthetic": False,
            "features": payload,
            "scores": {"urgency": u, "load": load},
            "proxy_label": _proxy_label(u, load, "self"),
        })
        idx += 1

    def _add_mail(rows: list[tuple[str, int, float]], synthetic: bool) -> None:
        nonlocal idx
        for sender_class, depth, hours_ago in rows:
            payload = {
                "is_timed": False,
                "seconds_to_event": 0.0,
                "sender_class": sender_class,
                "meeting_density": 0.0,
                "thread_depth": depth,
            }
            gi = GateItem(item_id=f"mail-{idx}", item_kind=ItemKind.GENERIC, created_at=0.0,
                          payload=payload)
            u = round(urgency(gi, params), 4)
            load = round(cognitive_load(gi, params), 4)
            items.append({
                "item_id": f"mail-{idx}",
                "source": "gmail",
                "synthetic": synthetic,
                "hours_ago": hours_ago,
                "features": payload,
                "scores": {"urgency": u, "load": load},
                "proxy_label": _proxy_label(u, load, sender_class),
            })
            idx += 1

    _add_mail(_MAIL_REAL, synthetic=False)
    _add_mail(_MAIL_SYNTH, synthetic=True)

    return {
        "provenance": {
            "source": "Local one-time MCP read of real Google Calendar + Gmail (metadata "
                      "only, no bodies) on 2026-06-21, de-identified to numeric features and "
                      "sender CLASS only (DEC-24/Q-38/NG-7). Synthetic human items flagged.",
            "now_anchor_utc": NOW.isoformat(),
            "real_calendar_events": len(_CAL),
            "real_gmail_threads": len(_MAIL_REAL),
            "synthetic_items": len(_MAIL_SYNTH),
            "label_rubric": "RUBRIC.md (vision gate: relevance AND importance AND user-state "
                            "AND confidence; quiet-by-default => default hold).",
            "human_confirmation": "PENDING Jim's one-time confirmatory pass (RISK-1 gate).",
        },
        "items": items,
    }


if __name__ == "__main__":
    # JSON is a strict subset of YAML, so the .real.yaml stays a valid YAML file while being
    # readable with the stdlib json module (no PyYAML dependency — not this lane's to add).
    out = Path(__file__).with_name("scoring_fixture.real.yaml")
    data = build()
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    n = len(data["items"])  # type: ignore[arg-type]
    print(f"wrote {out} with {n} items")

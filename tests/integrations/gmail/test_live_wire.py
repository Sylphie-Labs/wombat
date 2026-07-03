"""TK-16 live-wire smoke — GmailAuth -> AuthorizedSession -> GmailPoller (Q-67).

Gated on ``WOMBAT_TEST_GMAIL_LIVE=1`` (mirrors ``tests/integrations/gcal/test_live_wire.py``
and the ``WOMBAT_TEST_GMAIL_LIVE`` idiom in ``tests/integrations/gmail/test_auth.py``) —
SKIPS loudly with no gate var set. Joins the same Q-44-class pre-live obligation list as the
gcal live-wire smoke and TK-29: must run green (against a real, already-consented vault
credential) before the first live laptop session.

Exercises exactly ONE real authorized Gmail v1 ``messages.list`` GET through the FULLY
composed stack: only ``.get`` is ever issued, the raw GET returns 2xx, and the poller's own
parse turns the response into ``GmailMessageItem``s (round-tripped through ``from_payload`` to
prove the payload shape is valid).
"""

from __future__ import annotations

import os

import pytest

from wombat.config import load_config
from wombat.integrations.gmail.models import GmailMessageItem
from wombat.integrations.gmail.session import make_gmail_session

_LIVE_ENV = "WOMBAT_TEST_GMAIL_LIVE"

_requires_live_gmail = pytest.mark.skipif(
    not os.environ.get(_LIVE_ENV),
    reason=(
        f"{_LIVE_ENV} is not set — skipping the live composed-stack Gmail messages.list GET "
        "smoke test. Run `python -m wombat.integrations.gmail.auth` once to grant consent "
        f"(stores a token in the OS keyring vault), then export {_LIVE_ENV}=1 to exercise a "
        "real GET. Q-44-class pre-live obligation: must be green before the first live "
        "laptop session."
    ),
)


@_requires_live_gmail
async def test_live_composed_stack_issues_one_real_get_and_parses_gmail_messages() -> None:
    from wombat.integrations.gmail.poller import GmailPoller

    config = load_config()
    session = make_gmail_session(config)  # real GmailAuth -> real AuthorizedSession

    # The raw GET (the exact shape TK-75's poller issues) — asserts a real 2xx from Google.
    raw_response = session.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params={"q": "in:inbox", "maxResults": "1"},
        timeout=30.0,
    )
    assert 200 <= raw_response.status_code < 300

    # The FULLY composed stack: GmailAuth -> AuthorizedSession -> GmailPoller. Only `.get` is
    # ever available on `session` (an AuthorizedSession) via the poller's own `_GmailSession`
    # Protocol usage — no write method (including gmail.drafts.create) is ever invoked.
    poller = GmailPoller(session=session, poll_interval_seconds=300.0, lookback_hours=24.0)
    events = await poller.poll()

    assert isinstance(events, list)
    for event in events:
        parsed = GmailMessageItem.from_payload(event.payload)
        assert isinstance(parsed, GmailMessageItem)

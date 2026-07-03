"""wombat.integrations.gmail.poller — GmailPoller (TK-75, EP-17, Q-65).

The Gmail counterpart to ``wombat.integrations.gcal.poller.CalendarPoller`` (TK-72): reads
Gmail inbox messages READ-ONLY via the Gmail REST v1 API and yields them as ``SourceEvent``s for
the ``SourceRegistry`` to enqueue. It never writes to Gmail — it is TRANSPORT ONLY (Q-65 ruling
2): it uses an injected ``AuthorizedSession`` directly, registers NO cog-worx ``Capability``,
holds no tools, and is NOT a drive (DEC-26 untouched). It never constructs the ``QueueItem``
itself — the registry owns the ``SourceEvent -> QueueItem`` mapping.

Design (Q-65 BINDING rulings):
  * Conforms to the AS-BUILT ``InputSource`` Protocol exactly: ``id = "gmail"``,
    ``poll_interval_seconds``, ``async start()/stop()/poll() -> list[SourceEvent]``.
  * HTTP seam: ``_GmailSession`` is a minimal Protocol (mirrors ``CalendarPoller``'s
    ``_CalendarSession``) over ``google.auth.transport.requests.AuthorizedSession`` — the ONE
    method this poller needs is ``.get()``. The composition root builds the real
    ``AuthorizedSession`` from ``GmailAuth().get_credentials()`` and injects it here; this
    module never constructs ``GmailAuth`` and never imports ``googleapiclient`` (zero new
    deps). Only ``.get()`` is ever called — the no-write guarantee is structural: there is no
    ``post``/``put``/``patch``/``delete`` for this poller's code to even call, so it can never
    invoke or hold ``gmail.drafts.create`` (ruling 2, AC2).
  * Transient-error posture (ruling 5, mirrors TK-72's Q-59 ruling 4): network errors, HTTP
    401/403/5xx, and a malformed/blip response body (at either the list or the per-message GET)
    are ALL caught inside ``poll()`` -> logged as a WARNING naming "gmail" -> return ``[]``.
    ``poll()`` NEVER raises for these — a raising ``poll()`` would trip the registry's
    permanent-degrade path and stop gmail polling until restart (Q-59).
  * ``lookback_hours: float = 24.0`` (AC-fixed default, TK-8/TK-72 precedent). ``clock``
    (``Callable[[], datetime]``, aware UTC) is injected, mirroring ``CalendarPoller``.
  * Fetch shape (ruling 5): ``GET .../users/me/messages`` with
    ``q="in:inbox after:<epoch>"`` (epoch = ``clock() - lookback_hours``, a Gmail-search Unix
    timestamp), single page, ``maxResults=100``. If the list response carries a
    ``nextPageToken``, that is logged LOUDLY (wake-burst bounding is TK-28's job) but this
    poller does NOT paginate. Each returned message id is then fetched in full via
    ``GET .../users/me/messages/<id>``.
  * Body decoding (ruling 5, the minimal decode seam): Gmail's message ``payload`` is either a
    single part or a ``multipart/*`` tree. This poller recursively searches the part tree for
    the first ``text/plain`` part; if none exists, it falls back to the first ``text/html``
    part (kept AS-IS, no HTML stripping — minimal per the ruling); if neither exists, the body
    is ``""``. Gmail's part body data is base64url, decoded via ``base64.urlsafe_b64decode``
    (padded as needed) and decoded as UTF-8 (``errors="replace"`` — a decode blip must not
    crash the poll, ruling 5).
  * ``received_at`` (ruling 5): Gmail's top-level ``internalDate`` (epoch milliseconds, a
    UTC-anchored integer string) is the primary source — more reliable than parsing the
    ``Date`` header. If ``internalDate`` is absent, the ``Date`` header is parsed via
    ``email.utils.parsedate_to_datetime`` and normalized to UTC. If neither is present/parseable,
    a ``ValueError`` is raised, which ``poll()`` catches as a malformed-response degrade (ruling
    5, mirrors TK-72's malformed-interval handling).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

from wombat.integrations.gmail.models import GmailMessageItem
from wombat.sources.base import SourceEvent

logger = logging.getLogger(__name__)

# The Gmail v1 REST endpoints this poller reads (read-only: only GET is ever issued, ruling 2).
_MESSAGES_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _message_url(message_id: str) -> str:
    return f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"


# A conservative fixed request timeout — not a TK-13 tunable (no ticket asked for one), just a
# guard against an authorized session hanging forever on a dead connection.
_REQUEST_TIMEOUT_S = 30.0


def _utc_now() -> datetime:
    """The real-clock default for ``GmailPoller``'s injected ``clock``."""
    return datetime.now(UTC)


class _GmailSession(Protocol):
    """The ONE HTTP method ``GmailPoller`` needs (mirrors ``CalendarPoller``'s
    ``_CalendarSession``). Production injects a real ``AuthorizedSession``; tests inject a bare
    fake exposing only ``get`` — which makes the no-write guarantee structural (Q-65 ruling 2):
    there is no ``post``/``put``/``patch``/``delete`` for this poller's code to even call, so it
    cannot invoke or hold ``gmail.drafts.create``."""

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> requests.Response: ...


def _find_part_data(node: dict[str, Any], mime_type: str) -> str | None:
    """Recursively search a Gmail message ``payload`` (or one of its ``parts``) for the first
    part whose ``mimeType`` matches, returning its base64url body data (or ``None``)."""
    if node.get("mimeType") == mime_type:
        data = node.get("body", {}).get("data")
        if data:
            return str(data)
    for part in node.get("parts") or []:
        found = _find_part_data(part, mime_type)
        if found is not None:
            return found
    return None


def _decode_body_data(data: str) -> str:
    """Decode Gmail's base64url part body data (padded as needed) as UTF-8."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body_text(payload: dict[str, Any]) -> str:
    """The minimal body-decode seam (ruling 5): prefer ``text/plain``; fall back to
    ``text/html`` as-is if no plain part exists; ``""`` if neither exists."""
    plain = _find_part_data(payload, "text/plain")
    if plain is not None:
        return _decode_body_data(plain)
    html = _find_part_data(payload, "text/html")
    if html is not None:
        return _decode_body_data(html)
    return ""


def _parse_received_at(raw: dict[str, Any], headers: dict[str, str]) -> datetime:
    """``internalDate`` (epoch ms, UTC-anchored) is the primary source; the ``Date`` header is
    the fallback. Raises ``ValueError`` if neither is present/parseable (ruling 5) — ``poll()``
    catches this as a malformed-response degrade."""
    internal_date = raw.get("internalDate")
    if internal_date is not None:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    date_header = headers.get("Date")
    if date_header:
        parsed = parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise ValueError("gmail message has neither internalDate nor a parseable Date header")


def _parse_message(raw: dict[str, Any]) -> GmailMessageItem:
    """Map one Gmail v1 ``messages.get`` response to a ``GmailMessageItem``.

    Raises ``KeyError``/``ValueError``/``TypeError`` on a malformed/unexpected shape — ``poll()``
    catches these and degrades to ``[]`` rather than crashing (ruling 5).
    """
    message_id = raw["id"]
    payload = raw["payload"]
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    return GmailMessageItem(
        message_id=message_id,
        subject=headers.get("Subject", ""),
        sender=headers.get("From", ""),
        received_at=_parse_received_at(raw, headers),
        body_text=_extract_body_text(payload),
    )


class GmailPoller:
    """Reads Gmail inbox messages (read-only) and yields them as ``SourceEvent``s.

    Conforms to ``sources.base.InputSource``. Constructor-injects the authorized HTTP session
    and the clock — this class never constructs ``GmailAuth`` and never reads real wall-clock
    time itself, matching every other injected-dependency seam in this codebase. TRANSPORT
    ONLY (Q-65 ruling 2): registers no cog-worx ``Capability``, holds no tools, is not a drive.
    """

    id: str = "gmail"

    def __init__(
        self,
        *,
        session: _GmailSession,
        poll_interval_seconds: float,
        lookback_hours: float = 24.0,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._session = session
        self._lookback_hours = lookback_hours
        self._clock = clock

    async def start(self) -> None:
        """No lifecycle setup needed — the injected session is already authorized."""
        return None

    async def stop(self) -> None:
        """No lifecycle teardown needed."""
        return None

    def fetch_recent(self, *, lookback_hours: float | None = None) -> list[GmailMessageItem]:
        """Fetch inbox messages received in the last ``lookback_hours`` — the RAISING read seam
        (TK-98).

        ``lookback_hours`` defaults to the ctor's ``lookback_hours`` when omitted (``None``).
        Unlike ``poll()``, this method does NOT catch anything: a network error, an HTTP
        401/403/5xx, or a malformed response body (at either the list or per-message fetch) all
        propagate to the caller as-is. This lets a caller (e.g. ``BriefGatherStage``, TK-98)
        distinguish "source unavailable" from "zero messages" — something a swallowed-to-``[]``
        result cannot. ``poll()`` below is the transient-error-tolerant wrapper around this
        method (ruling 5 unchanged).
        """
        hours = self._lookback_hours if lookback_hours is None else lookback_hours
        now = self._clock()
        after_epoch = int((now - timedelta(hours=hours)).timestamp())
        params = {"q": f"in:inbox after:{after_epoch}", "maxResults": "100"}
        list_response = self._session.get(
            _MESSAGES_LIST_URL, params=params, timeout=_REQUEST_TIMEOUT_S
        )
        list_response.raise_for_status()
        list_body = list_response.json()
        if list_body.get("nextPageToken"):
            logger.warning(
                "gmail source %r: list response has more pages (nextPageToken present) — "
                "wake-burst bounding is TK-28's job; NOT paginating this poll",
                self.id,
            )
        message_refs = list_body.get("messages") or []
        items: list[GmailMessageItem] = []
        for ref in message_refs:
            message_id = ref["id"]
            msg_response = self._session.get(
                _message_url(message_id), params={"format": "full"}, timeout=_REQUEST_TIMEOUT_S
            )
            msg_response.raise_for_status()
            items.append(_parse_message(msg_response.json()))
        return items

    async def poll(self) -> list[SourceEvent]:
        """Fetch inbox messages received in the last ``lookback_hours`` and yield them as
        ``SourceEvent``s.

        NEVER raises (ruling 5): a network error, an HTTP 401/403/5xx, or a malformed response
        body (at either the list or per-message fetch) are all logged as a WARNING naming
        "gmail" and degrade to ``[]`` — the registry keeps polling this source on the next
        cycle instead of marking it degraded. A thin wrapper around the RAISING
        ``fetch_recent`` (TK-98) — behavior-preserving.
        """
        try:
            items = self.fetch_recent()
        except requests.exceptions.RequestException:
            logger.warning(
                "gmail source %r: Gmail API request failed (network/auth/server error) — "
                "degrading this poll to no events",
                self.id,
                exc_info=True,
            )
            return []
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "gmail source %r: malformed Gmail API response — degrading this poll to "
                "no events",
                self.id,
                exc_info=True,
            )
            return []

        return [SourceEvent(event_key=item.message_id, payload=item.to_payload()) for item in items]


__all__ = ["GmailPoller"]

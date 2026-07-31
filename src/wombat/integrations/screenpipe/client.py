"""wombat.integrations.screenpipe.client — ScreenpipeClient (TK-320, EP-37, DEC-70a/f/i).

The ONE module in this codebase that ever talks to screenpipe (an operator-installed,
on-host screen-capture/OCR service — DEC-70). Read-only: exposes ``health() -> bool`` and
``search(start, end, app_name=None, limit=None) -> list[ScreenpipeItem]`` over screenpipe's
local REST API — ``GET /health`` and ``GET /search`` are the ONLY two endpoints ever
referenced; no other endpoint string (write, settings, or removal) exists anywhere in this
module (grep AC).

RULING r3 (binding): transport is stdlib ``urllib.request`` with a pinned short timeout —
``httpx``/``requests`` stay extra/dev-only in this repo; NO new pip dependency. RULING r4
(binding): ``content_type`` is ALWAYS the literal ``"ocr"`` — no other content-type value is
ever requested; screenpipe's non-visual capture channel is entirely out of scope here (DEF-16
— that consumption is deferred, full stop; this module structurally cannot reach it). RULING
r5 pins ``_MAX_RESULTS``/``_MAX_TEXT_CHARS``/``_TIMEOUT_S`` module-private, no knobs (DEC-63).

CUSTODY (DEC-70a/(i)):
  (1) loopback-only guard — a ``base_url`` whose host is not ``localhost``/``127.0.0.1`` is
      REFUSED at construction with one loud ERROR; the client becomes PERMANENTLY degraded
      and never issues a request (a config typo can never turn this into cloud egress).
  (2) every text field is char-capped at ``_MAX_TEXT_CHARS`` and every result list is
      truncated at ``_MAX_RESULTS`` BEFORE returning to any caller.
  (3) READ-ONLY — only ``GET`` is ever issued.

DEGRADE (DEC-70i): every public read catches ALL exceptions -> ``health()`` returns ``False``
/ ``search()`` returns ``[]``, logging AT MOST one WARNING per consecutive failure streak (the
``observe_screen``/``read_idle_ms`` posture) — screenpipe not installed/not running/API down
is an ordinary state, never a raise, never a boot concern. A successful call re-arms the
single warning.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlparse

logger = logging.getLogger(__name__)

# DEC-63 no-knob pins — module-private, no ticket has asked for an operator-facing tunable.
_MAX_RESULTS = 50
_MAX_TEXT_CHARS = 400
_TIMEOUT_S = 2.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

# The ONLY two endpoints this module ever references (grep AC — no other endpoint string
# exists anywhere below).
_HEALTH_PATH = "/health"
_SEARCH_PATH = "/search"

# The ONLY content_type this module ever requests (RULING r4 / DEF-16 structural).
_CONTENT_TYPE = "ocr"


@dataclass(frozen=True, slots=True)
class ScreenpipeItem:
    """One screenpipe OCR search result — every text field already char-capped at
    ``_MAX_TEXT_CHARS`` by the time this is constructed (``ScreenpipeClient.search`` is the
    only producer)."""

    app: str
    title: str
    text_snippet: str
    captured_at: datetime
    ref_id: str


def _cap(value: object) -> str:
    """Coerce to ``str`` and char-cap at ``_MAX_TEXT_CHARS`` — applied to every text field
    before a ``ScreenpipeItem`` is ever constructed (custody (2))."""
    return str(value)[:_MAX_TEXT_CHARS]


def _parse_captured_at(raw_timestamp: object) -> datetime:
    """Parse screenpipe's timestamp and normalize to a UTC-AWARE ``datetime`` (ISS-37 m1) — a
    naive ``fromisoformat`` result (no ``tzinfo``) is treated as UTC rather than left naive, since
    a mix of naive and aware ``captured_at`` values raises ``TypeError`` in downstream sort/dwell
    math (``screenpipe_source.py``). An already-aware value is converted to UTC for consistency."""
    parsed = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_item(raw: dict[str, Any]) -> ScreenpipeItem:
    """Map one screenpipe ``/search`` result (``{"type": "OCR", "content": {...}}``) to a
    ``ScreenpipeItem``. Raises ``KeyError``/``ValueError``/``TypeError`` on a malformed shape —
    the caller's blanket ``except Exception`` degrades the whole call to ``[]`` (DEC-70i)."""
    content = raw["content"]
    return ScreenpipeItem(
        app=_cap(content.get("app_name", "")),
        title=_cap(content.get("window_name", "")),
        text_snippet=_cap(content.get("text", "")),
        captured_at=_parse_captured_at(content["timestamp"]),
        ref_id=_cap(content.get("frame_id", "")),
    )


def _urlopen(request: urllib.request.Request) -> Any:
    """The ONE transport seam — a thin wrapper so tests can spy on it (proving a degraded
    client never calls this at all) without touching real sockets. Production always routes
    through this; ``ScreenpipeClient`` never calls ``urllib.request.urlopen`` directly."""
    return urllib.request.urlopen(request, timeout=_TIMEOUT_S)


class ScreenpipeClient:
    """Read-only REST client over a local screenpipe instance (DEC-70a). The ONE class that
    ever talks to screenpipe — see the module docstring for the full custody/degrade contract.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._failure_streak_warned = False
        # ISS-37-RIDER m5: set on EVERY `search()` call — True whenever that call's `[]`/result
        # came from degrade (permanent non-loopback refusal or a caught exception) rather than a
        # genuine, trustworthy query result. Callers (``screenpipe_source.ScreenpipeEventSource``)
        # read this to decide whether the polled window was actually observed (safe to advance
        # the cursor) or lost to an outage (must be retried, never silently dropped).
        self.last_search_degraded = False

        host = urlparse(base_url).hostname
        self._degraded = host not in _LOOPBACK_HOSTS
        if self._degraded:
            logger.error(
                "ScreenpipeClient: refusing non-loopback base_url %r (host=%r) — screenpipe "
                "is an on-host service only; this client is now permanently degraded and will "
                "never issue a request",
                base_url,
                host,
            )

    def health(self) -> bool:
        """``GET /health`` — ``True`` on any successful response, ``False`` on any exception or
        while permanently degraded (DEC-70i). Never raises."""
        if self._degraded:
            return False
        try:
            self._get(_HEALTH_PATH)
        except Exception:
            self._warn_once()
            return False
        self._rearm()
        return True

    def search(
        self,
        start: datetime,
        end: datetime,
        *,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> list[ScreenpipeItem]:
        """``GET /search`` over ``[start, end]`` with ``content_type=ocr`` — bounded to at most
        ``_MAX_RESULTS`` items, each text field char-capped at ``_MAX_TEXT_CHARS``. ``[]`` on
        any exception or while permanently degraded (DEC-70i). Never raises. Sets
        ``last_search_degraded`` on EVERY call (ISS-37-RIDER m5) so a caller can distinguish a
        genuine empty window from a degraded one."""
        if self._degraded:
            self.last_search_degraded = True
            return []
        capped_limit = _MAX_RESULTS if limit is None else min(limit, _MAX_RESULTS)
        params = {
            "content_type": _CONTENT_TYPE,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "limit": str(capped_limit),
        }
        if app_name is not None:
            params["app_name"] = app_name
        try:
            body = self._get(_SEARCH_PATH, params)
            items = [_parse_item(raw) for raw in body.get("data", [])]
        except Exception:
            self._warn_once()
            self.last_search_degraded = True
            return []
        self._rearm()
        self.last_search_degraded = False
        return items[:_MAX_RESULTS]

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = urllib.request.Request(url, method="GET")
        with _urlopen(request) as response:
            raw_body = response.read()
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
        return payload

    def _warn_once(self) -> None:
        """AT MOST one WARNING per consecutive failure streak (DEC-70i, the ``observe_screen``
        posture) — re-armed by ``_rearm`` on the next success."""
        if not self._failure_streak_warned:
            logger.warning(
                "ScreenpipeClient: request failed — degrading (health False / search []); "
                "further consecutive failures stay silent until a success re-arms this warning",
                exc_info=True,
            )
            self._failure_streak_warned = True

    def _rearm(self) -> None:
        self._failure_streak_warned = False


__all__ = ["ScreenpipeClient", "ScreenpipeItem"]

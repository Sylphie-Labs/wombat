"""wombat.capabilities.playwright_capability — the ``browser`` Capability (TK-131, Q-113):
the first browser ticket, heading the browser arc. A headless-Chromium, accessibility-tree
session over Playwright, hand-registered on cog-worx's ``Registry``.

Seams verified in the installed cog-worx source (Q-113): the ``Capability`` protocol
(``cogworx/capability/base.py``) is STRUCTURAL — attrs ``name``, ``tier``, ``input_schema`` plus
the sole dispatch method ``async def invoke(self, args)``. There is no close/teardown hook on
the protocol and ``Engine`` performs no capability teardown (Q-32) — wombat owns session
lifecycle here, via ``BrowserSession.close()``.

``playwright`` rides the optional ``browser`` extra (never a hard dep — the ``voice``/
``voice-cloud`` precedent): ``BrowserSession.ensure_open`` is the ONLY place that imports it, and
does so lazily, so a base checkout still boots clean without the extra installed. Dev setup:
``uv sync --extra browser`` then ``uv run playwright install chromium``.

``PlaywrightCapability`` is hand-written (not ``registry.function_capability``): its input
schema needs a top-level ``action`` enum plus per-action fields, which the auto-derived-from-
signature builder does not produce. It is registered WITHOUT the ``trusted-output`` tag, so any
gate dispatch latches ``TaintState`` (external-tier rule, ``cogworx/capability/policy.py``).
``dispatch_one`` latches taint BEFORE ``invoke`` — the FIRST external dispatch in a drive taints
AND still executes, but every SUBSEQUENT external dispatch then raises ``TierViolation``
(taint drops the external tier from the effective set). Consequently ``navigate`` returns the
structured a11y snapshot in the SAME ``invoke`` call — a separate follow-up dispatch would never
be reached; the standalone ``snapshot`` action exists for direct, ungated use.

Snapshot shape: Playwright's legacy ``page.accessibility.snapshot()`` (a nested role/name/state
dict) has been removed upstream in favour of ``page.aria_snapshot()``, which renders the same
role/name/state information as a YAML outline string (e.g. ``heading "Title" [level=1]``).
``_capture_snapshot`` parses that outline via ``yaml.safe_load`` (wombat's existing ``pyyaml``
dependency — no new parser) so the returned snapshot is a real JSON-native nested structure
(lists/dicts/strings), never an opaque blob and never a screenshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml
from cogworx.capability.base import PermissionTier

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

BROWSER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["navigate", "snapshot"]},
        "url": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
    "if": {"properties": {"action": {"const": "navigate"}}},
    "then": {"required": ["action", "url"]},
}
"""Hand-authored (Q-113(b)): a top-level ``action`` enum plus the per-action ``url`` field
(required only when ``action == "navigate"``), ``additionalProperties: false`` so
``dispatch_one``'s framework-side jsonschema validation rejects anything else."""


class BrowserSession:
    """wombat-owned Chromium session lifecycle (Q-113(d)) — opens lazily on first
    ``ensure_open()`` call (``async_playwright().start()``, a headless Chromium launch, and ONE
    page), never at construction time. ``close()`` is explicit and idempotent: it stops the
    page, the browser, and the playwright driver itself, and a second call is a no-op.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    @property
    def is_open(self) -> bool:
        """True once ``ensure_open`` has launched a Chromium process and page."""
        return self._browser is not None

    async def ensure_open(self) -> Page:
        """Return the session's single ``Page``, lazily launching headless Chromium on first
        call. ``playwright`` is imported HERE, not at module scope, so importing/constructing
        this class never requires the ``browser`` extra — only calling this does."""
        if self._page is None:
            from playwright.async_api import async_playwright  # lazy import — `browser` extra

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._page = await self._browser.new_page()
        return self._page

    async def close(self) -> None:
        """Idempotent teardown: stop the page, the browser, then the playwright driver. Safe to
        call twice — every handle is already ``None`` on the second call, so each step is
        skipped."""
        if self._page is not None:
            await self._page.close()
            self._page = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


async def _capture_snapshot(page: Page) -> Any:
    """Capture ``page``'s accessibility tree via ``aria_snapshot`` and parse its YAML outline
    into a JSON-native nested structure (see the module docstring for why ``aria_snapshot``
    rather than the removed ``accessibility.snapshot()``). Never takes a screenshot."""
    raw = await page.aria_snapshot()
    parsed = yaml.safe_load(raw)
    return parsed if parsed is not None else []


class PlaywrightCapability:
    """The ``browser`` external-tier ``Capability`` (Q-113(b)/(c)) — see the module docstring
    for the taint mechanic that shapes ``invoke``'s ``navigate`` branch."""

    name: str = "browser"
    tier: PermissionTier = "external"
    input_schema: Mapping[str, Any] = BROWSER_INPUT_SCHEMA

    def __init__(self, session: BrowserSession | None = None) -> None:
        self.session: BrowserSession = session if session is not None else BrowserSession()

    async def invoke(self, args: Mapping[str, Any]) -> Any:
        action = args["action"]
        page = await self.session.ensure_open()
        if action == "navigate":
            url = args["url"]
            await page.goto(url)
            return {"url": url, "snapshot": await _capture_snapshot(page)}
        if action == "snapshot":
            return {"snapshot": await _capture_snapshot(page)}
        raise ValueError(f"unknown action {action!r}")  # unreachable past schema validation


__all__ = [
    "BROWSER_INPUT_SCHEMA",
    "BrowserSession",
    "PlaywrightCapability",
]

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

TK-132 (Q-113(e)) extends the action set with ``click``/``type``/``select`` interaction actions
plus a ``screenshot`` fallback. All three interaction actions resolve their target EXCLUSIVELY
via Playwright's ``page.get_by_role(role, name=...)`` a11y locator — never CSS/XPath — through
the shared ``_act_role`` helper: on success it returns ``{"ok": True, "snapshot": ...}`` (the
post-action a11y snapshot, mirroring ``navigate``); if the role+name locator does not resolve
within ``ELEMENT_TIMEOUT_MS``, Playwright's ``TimeoutError`` is caught and converted into a
STRUCTURED ``{"ok": False, "error": "element_not_found", "role": ..., "name": ...}`` result rather
than propagating — ``dispatch_one`` relays ``invoke`` exceptions raw, so a caller-actionable
not-found signal has to be a return value, not a raise. ``screenshot`` is the clearly-labelled
pixel fallback (CST-5: the a11y tree is the workhorse) — it returns raw PNG bytes and logs a
``"screenshot-fallback used"`` record so its (expected-rare) use is auditable; deciding WHEN to
fall back to it is the caller's call, never automatic here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import yaml
from cogworx.capability.base import PermissionTier

if TYPE_CHECKING:
    from playwright.async_api import Browser, Locator, Page, Playwright

logger = logging.getLogger(__name__)

ELEMENT_TIMEOUT_MS: int = 3000
"""Bounded wait (Q-113(e)) for a ``get_by_role`` locator to resolve on click/type/select — a
locator that never resolves within this window is reported as ``element_not_found`` rather than
hanging or raising."""

BROWSER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["navigate", "snapshot", "click", "type", "select", "screenshot"],
        },
        "url": {"type": "string"},
        "role": {"type": "string"},
        "name": {"type": "string"},
        "value": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"properties": {"action": {"const": "navigate"}}},
            "then": {"required": ["action", "url"]},
        },
        {
            "if": {"properties": {"action": {"const": "click"}}},
            "then": {"required": ["action", "role", "name"]},
        },
        {
            "if": {"properties": {"action": {"const": "type"}}},
            "then": {"required": ["action", "role", "name", "value"]},
        },
        {
            "if": {"properties": {"action": {"const": "select"}}},
            "then": {"required": ["action", "role", "name", "value"]},
        },
    ],
}
"""Hand-authored (Q-113(b)/(e)): a top-level ``action`` enum plus per-action fields — ``url`` for
``navigate``; ``role``/``name`` (the a11y locator) for ``click``; ``role``/``name``/``value`` for
``type``/``select``; ``screenshot`` needs nothing beyond ``action``. ``additionalProperties:
false`` so ``dispatch_one``'s framework-side jsonschema validation rejects anything else."""


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


async def _act_role(
    page: Page, role: str, name: str, act: Callable[[Locator], Awaitable[Any]]
) -> dict[str, Any]:
    """Resolve ``role``/``name`` via ``page.get_by_role`` (Q-113(e): the ONLY locator strategy —
    never CSS/XPath) and run ``act`` against it. On success, returns
    ``{"ok": True, "snapshot": <post-action a11y snapshot>}``. If the locator does not resolve
    within ``ELEMENT_TIMEOUT_MS``, Playwright's ``TimeoutError`` is caught here and converted to
    a structured ``{"ok": False, "error": "element_not_found", "role": ..., "name": ...}`` —
    ``dispatch_one`` propagates ``invoke`` exceptions raw, so this has to be a return value."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # lazy: see module

    # get_by_role's stub narrows role to a Literal[AriaRole] union; wombat accepts any string
    # role from the caller-supplied args (schema-validated as a plain string, not that enum).
    locator = page.get_by_role(role, name=name)  # type: ignore[arg-type]
    try:
        await act(locator)
    except PlaywrightTimeoutError:
        return {"ok": False, "error": "element_not_found", "role": role, "name": name}
    return {"ok": True, "snapshot": await _capture_snapshot(page)}


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
        if action == "click":
            role, name = args["role"], args["name"]
            return await _act_role(
                page, role, name, lambda loc: loc.click(timeout=ELEMENT_TIMEOUT_MS)
            )
        if action == "type":
            role, name, value = args["role"], args["name"], args["value"]
            return await _act_role(
                page, role, name, lambda loc: loc.fill(value, timeout=ELEMENT_TIMEOUT_MS)
            )
        if action == "select":
            role, name, value = args["role"], args["name"], args["value"]
            return await _act_role(
                page,
                role,
                name,
                lambda loc: loc.select_option(value, timeout=ELEMENT_TIMEOUT_MS),
            )
        if action == "screenshot":
            logger.warning(
                "screenshot-fallback used (CST-5: a11y tree is the workhorse, pixels are the "
                "labelled fallback) — role/name locators were not sufficient for this step"
            )
            return await page.screenshot(type="png")
        raise ValueError(f"unknown action {action!r}")  # unreachable past schema validation


__all__ = [
    "BROWSER_INPUT_SCHEMA",
    "ELEMENT_TIMEOUT_MS",
    "BrowserSession",
    "PlaywrightCapability",
]

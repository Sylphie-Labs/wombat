"""TK-131 acceptance criteria — ``PlaywrightCapability`` / ``BrowserSession`` (Q-113): the
``browser`` external-tier Capability scaffold, an a11y-tree session over Playwright with
wombat-owned teardown.

``playwright`` rides the optional ``browser`` extra — this whole module SKIPS LOUDLY when it is
absent (module-level ``pytest.importorskip``, below) and again when Chromium itself cannot
launch (a session-scoped, autouse launch-probe fixture that ``pytest.skip()``s rather than
failing the suite). Dev setup:

    uv sync --extra browser
    uv run playwright install chromium

AC1: ``invoke`` a ``navigate`` call against a LOCAL stdlib ``http.server`` page (bound to port 0
— never the live internet) returns a structured role/name/state a11y snapshot; ``page.screenshot``
is proven never called (a poisoned stand-in raises if invoked).
AC2: the capability, registered on a real cog-worx ``Registry`` behind an external-tier-permitting
``ToolGate``, resolves by id ``"browser"`` and dispatches via ``dispatch_one`` (async invoke) — the
session's ``is_open`` is False before the first dispatch and True after (lazy open on first use).
Also pins the taint mechanic the ruled design leans on (Q-113(c)): the first external dispatch
taints the drive; a second one is then refused with ``TierViolation``.
AC3: ``await session.close()`` fully stops the browser/playwright driver (``is_open`` False, the
underlying ``Browser`` handle disconnected); a second ``close()`` call is a documented no-op.

Bonus (pure jsonschema, no Chromium needed but still gated by the module-level skip above): the
hand-authored ``BROWSER_INPUT_SCHEMA`` (Q-113(b)) requires ``url`` for ``navigate`` and rejects
unknown keys (``additionalProperties: false``).
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
from cogworx.capability.policy import StageToolPolicy, TierViolation, ToolGate
from cogworx.capability.registry import Registry
from cogworx.capability.router import dispatch_one

from wombat.capabilities.playwright_capability import (
    BROWSER_INPUT_SCHEMA,
    BrowserSession,
    PlaywrightCapability,
)

pytest.importorskip("playwright")


@pytest.fixture(scope="session", autouse=True)
def _chromium_launch_probe() -> None:
    """Loudly SKIP the whole module if Chromium cannot launch (e.g. ``playwright install
    chromium`` was never run on this checkout) — never a hard failure. A throwaway sync-API
    launch/close, fully torn down before any async-API test in this module runs."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"chromium launch failed — run `uv run playwright install chromium`: {exc}")


_TEST_PAGE_HTML = (
    b"<html><body><h1>Hello Wombat</h1><p>a paragraph</p><button>Click me</button></body></html>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_TEST_PAGE_HTML)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default stderr request logging


@pytest.fixture
def local_page_url() -> Iterator[str]:
    """Serve ``_TEST_PAGE_HTML`` from a stdlib ``http.server`` bound to an OS-assigned port
    (port 0) — AC1 must never hit the live internet."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# AC1 — navigate returns a structured snapshot, never a screenshot
# ---------------------------------------------------------------------------


async def test_ac1_navigate_returns_structured_snapshot_no_screenshot(
    local_page_url: str,
) -> None:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    try:
        # Pre-open and poison `page.screenshot` BEFORE the navigate dispatch, so any call to it
        # during `invoke` raises — proving AC1's "no screenshot taken".
        page = await session.ensure_open()

        async def _poison_screenshot(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("screenshot must never be called by invoke()")

        page.screenshot = _poison_screenshot  # type: ignore[method-assign]

        result = await cap.invoke({"action": "navigate", "url": local_page_url})

        assert result["url"] == local_page_url
        snapshot = result["snapshot"]
        assert isinstance(snapshot, list)
        assert snapshot, "the a11y snapshot must be a non-empty structured tree"
        rendered = str(snapshot)
        assert "heading" in rendered
        assert "Hello Wombat" in rendered
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# AC2 — registered, resolved by id, dispatched; session opens lazily on first use
# ---------------------------------------------------------------------------


async def test_ac2_registered_dispatch_opens_session_lazily_then_taints(
    local_page_url: str,
) -> None:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    registry = Registry()
    registry.register(cap)  # NO "trusted-output" tag — external dispatch latches taint
    policy = StageToolPolicy(allowed_tiers=frozenset({"read", "write", "external"}))
    gate = ToolGate(registry, policy=policy)

    assert registry.get("browser") is cap
    assert not session.is_open, "no browser process before the first invoke"

    try:
        result = await dispatch_one(
            gate, registry, "browser", {"action": "navigate", "url": local_page_url}
        )
        assert session.is_open, "session opens lazily on first dispatch"
        assert result["url"] == local_page_url
        assert gate.taint.tainted, "external dispatch without trusted-output must taint"

        # Q-113(c): taint drops the external tier, so a SUBSEQUENT external dispatch in the same
        # drive is refused — this is why `navigate` must return the full snapshot in one call.
        with pytest.raises(TierViolation):
            await dispatch_one(gate, registry, "browser", {"action": "snapshot"})
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# AC3 — explicit, idempotent teardown
# ---------------------------------------------------------------------------


async def test_ac3_close_is_idempotent_and_fully_tears_down(local_page_url: str) -> None:
    session = BrowserSession()
    page = await session.ensure_open()
    await page.goto(local_page_url)
    browser = page.context.browser
    assert browser is not None
    assert session.is_open
    assert browser.is_connected()

    await session.close()
    assert not session.is_open
    assert not browser.is_connected(), "no live Chromium child must remain after close()"

    # Idempotent: a second close() is a documented no-op, never raises.
    await session.close()
    assert not session.is_open


def test_input_schema_requires_url_for_navigate_and_rejects_unknown_keys() -> None:
    jsonschema.validate({"action": "snapshot"}, BROWSER_INPUT_SCHEMA)
    jsonschema.validate({"action": "navigate", "url": "http://x.test"}, BROWSER_INPUT_SCHEMA)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"action": "navigate"}, BROWSER_INPUT_SCHEMA)  # missing url

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"action": "snapshot", "unexpected": 1}, BROWSER_INPUT_SCHEMA
        )  # additionalProperties: false

"""TK-132 acceptance criteria — ``PlaywrightCapability`` interaction actions (Q-113(e)): click,
type, select resolved via a11y role+name locators, plus the ``screenshot`` pixel fallback.

``playwright`` rides the optional ``browser`` extra — this whole module SKIPS LOUDLY when it is
absent (module-level ``pytest.importorskip``) and again when Chromium itself cannot launch (a
session-scoped, autouse launch-probe fixture that ``pytest.skip()``s rather than failing), same
gating as TK-131's ``test_playwright_capability.py``.

AC1: a ``type`` action naming the input's accessible name and a value locates it via
``get_by_role`` and fills it — the returned snapshot reflects the typed value.
AC2: a ``click`` action naming a button's accessible name resolves via role+name and clicks it —
a post-click snapshot is returned.
AC3: a ``screenshot`` action returns raw PNG bytes (PNG magic header asserted) and emits a log
record containing the substring ``"screenshot-fallback used"`` (``caplog``).
AC4: an action naming a nonexistent element returns the structured
``{"ok": False, "error": "element_not_found", ...}`` result within the bounded timeout — no
unhandled exception propagates.
"""

from __future__ import annotations

import http.server
import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from wombat.capabilities.playwright_capability import BrowserSession, PlaywrightCapability

pytest.importorskip("playwright")


@pytest.fixture(scope="session", autouse=True)
def _chromium_launch_probe() -> None:
    """Loudly SKIP the whole module if Chromium cannot launch (e.g. ``playwright install
    chromium`` was never run on this checkout) — never a hard failure."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"chromium launch failed — run `uv run playwright install chromium`: {exc}")


_TEST_PAGE_HTML = (
    b"<html><body>"
    b'<label for="nm">Your name</label>'
    b'<input id="nm" type="text" />'
    b"<button>Submit form</button>"
    b"</body></html>"
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
    (port 0) — never the live internet."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def opened_capability(local_page_url: str) -> Any:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    page = await session.ensure_open()
    await page.goto(local_page_url)
    try:
        yield cap
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# AC1 — type: role+name locator, value filled, snapshot reflects it
# ---------------------------------------------------------------------------


async def test_ac1_type_fills_named_input_and_snapshot_reflects_value(
    opened_capability: PlaywrightCapability,
) -> None:
    result = await opened_capability.invoke(
        {"action": "type", "role": "textbox", "name": "Your name", "value": "Wombat"}
    )
    assert result["ok"] is True
    assert "Wombat" in str(result["snapshot"])


# ---------------------------------------------------------------------------
# AC2 — click: role+name locator, post-click snapshot returned
# ---------------------------------------------------------------------------


async def test_ac2_click_resolves_named_button_and_returns_snapshot(
    opened_capability: PlaywrightCapability,
) -> None:
    result = await opened_capability.invoke(
        {"action": "click", "role": "button", "name": "Submit form"}
    )
    assert result["ok"] is True
    assert "snapshot" in result


# ---------------------------------------------------------------------------
# AC3 — screenshot: PNG bytes + auditable log record
# ---------------------------------------------------------------------------


async def test_ac3_screenshot_returns_png_bytes_and_logs_fallback_usage(
    opened_capability: PlaywrightCapability,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = await opened_capability.invoke({"action": "screenshot"})

    assert isinstance(result, bytes)
    assert result.startswith(b"\x89PNG\r\n\x1a\n"), "must be a real PNG (magic header)"
    assert any("screenshot-fallback used" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# AC4 — element-not-found returns a structured result, never raises
# ---------------------------------------------------------------------------


async def test_ac4_element_not_found_returns_structured_result(
    opened_capability: PlaywrightCapability,
) -> None:
    result = await opened_capability.invoke(
        {"action": "click", "role": "button", "name": "Does Not Exist"}
    )
    assert result == {
        "ok": False,
        "error": "element_not_found",
        "role": "button",
        "name": "Does Not Exist",
    }


async def test_ac4_type_element_not_found_returns_structured_result(
    opened_capability: PlaywrightCapability,
) -> None:
    result = await opened_capability.invoke(
        {"action": "type", "role": "textbox", "name": "Nope", "value": "x"}
    )
    assert result["ok"] is False
    assert result["error"] == "element_not_found"


# ---------------------------------------------------------------------------
# Bonus — select action, same role+name resolution path (not a numbered AC, but part of the
# ticket's goal: click/type/select all riding the shared _act_role helper).
# ---------------------------------------------------------------------------


async def test_select_option_resolves_named_combobox_and_returns_snapshot() -> None:
    html = (
        b"<html><body>"
        b'<label for="color">Favourite colour</label>'
        b'<select id="color">'
        b'<option value="r">Red</option>'
        b'<option value="b">Blue</option>'
        b"</select>"
        b"</body></html>"
    )

    class _SelectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _SelectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    try:
        page = await session.ensure_open()
        await page.goto(f"http://127.0.0.1:{server.server_port}/")

        result = await cap.invoke(
            {"action": "select", "role": "combobox", "name": "Favourite colour", "value": "b"}
        )
        assert result["ok"] is True
    finally:
        await session.close()
        server.shutdown()
        thread.join(timeout=5)

"""TK-135 acceptance criteria — ``PlaywrightCapability``'s ``submit_form`` action (Q-114) over a
REAL Chromium session: fill every field then click submit, all inside ONE ``invoke`` call.

``playwright`` rides the optional ``browser`` extra — this whole module SKIPS LOUDLY when it is
absent (module-level ``pytest.importorskip``) and again when Chromium itself cannot launch (a
session-scoped, autouse launch-probe fixture that ``pytest.skip()``s rather than failing), the
same gating as ``tests/capabilities/test_playwright_actions.py``/``test_playwright_capability.py``.

A local stdlib ``ThreadingHTTPServer`` bound to ``127.0.0.1``/port 0 serves a real HTML form —
NEVER the live internet. Submitting the form (a plain GET) navigates to a distinct "thanks" page,
so a successful submit is provable by the post-submit a11y snapshot showing that page's content —
not just an ``ok`` flag.

AC1: filling every field and clicking submit navigates to the thanks page; the returned snapshot
reflects the post-submit state.
AC2: a missing submit target returns the structured ``element_not_found`` result and the page
never navigates (no submit click happened).
AC3: a missing FIELD target (before submit is ever reached) returns the structured
``element_not_found`` result naming that field, and the page never navigates.
"""

from __future__ import annotations

import http.server
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


_FORM_HTML = (
    b"<html><body>"
    b'<form action="/thanks" method="get">'
    b'<label for="nm">Your name</label>'
    b'<input id="nm" name="name" type="text" />'
    b'<label for="msg">Message</label>'
    b'<input id="msg" name="message" type="text" />'
    b'<button type="submit">Send message</button>'
    b"</form>"
    b"</body></html>"
)

_THANKS_HTML = b"<html><body><h1>Thanks for your message</h1></body></html>"


class _FormHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.path.startswith("/thanks"):
            self.wfile.write(_THANKS_HTML)
        else:
            self.wfile.write(_FORM_HTML)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default stderr request logging


@pytest.fixture
def local_form_url() -> Iterator[str]:
    """Serve ``_FORM_HTML``/``_THANKS_HTML`` from a stdlib ``http.server`` bound to an
    OS-assigned port (port 0) — NEVER the live internet."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _FormHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def opened_capability(local_form_url: str) -> Any:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    try:
        yield cap, session, local_form_url
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# AC1 — every field filled, submit clicked, post-submit state proven
# ---------------------------------------------------------------------------


async def test_ac1_submit_form_fills_every_field_and_clicks_submit(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, url = opened_capability

    result = await cap.invoke(
        {
            "action": "submit_form",
            "url": url,
            "fields": [
                {"role": "textbox", "name": "Your name", "value": "Wombat"},
                {"role": "textbox", "name": "Message", "value": "hello there"},
            ],
            "submit": {"role": "button", "name": "Send message"},
        }
    )

    assert result["ok"] is True
    rendered = str(result["snapshot"])
    assert "Thanks for your message" in rendered  # proves the click actually navigated

    page = await session.ensure_open()
    assert page.url.startswith(f"{url}thanks")


# ---------------------------------------------------------------------------
# AC2 — submit target not found: structured result, no navigation
# ---------------------------------------------------------------------------


async def test_ac2_submit_target_not_found_returns_structured_result_no_navigation(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, url = opened_capability

    result = await cap.invoke(
        {
            "action": "submit_form",
            "url": url,
            "fields": [
                {"role": "textbox", "name": "Your name", "value": "Wombat"},
                {"role": "textbox", "name": "Message", "value": "hello there"},
            ],
            "submit": {"role": "button", "name": "Does Not Exist"},
        }
    )

    assert result == {
        "ok": False,
        "error": "element_not_found",
        "role": "button",
        "name": "Does Not Exist",
    }

    page = await session.ensure_open()
    assert not page.url.startswith(f"{url}thanks"), "no submit click must mean no navigation"


# ---------------------------------------------------------------------------
# AC3 — a field target not found: structured result naming that field, no submit click
# ---------------------------------------------------------------------------


async def test_ac3_field_not_found_returns_structured_result_and_never_clicks_submit(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, url = opened_capability

    result = await cap.invoke(
        {
            "action": "submit_form",
            "url": url,
            "fields": [
                {"role": "textbox", "name": "Your name", "value": "Wombat"},
                {"role": "textbox", "name": "Nonexistent Field", "value": "x"},
            ],
            "submit": {"role": "button", "name": "Send message"},
        }
    )

    assert result == {
        "ok": False,
        "error": "element_not_found",
        "role": "textbox",
        "name": "Nonexistent Field",
    }

    page = await session.ensure_open()
    assert not page.url.startswith(f"{url}thanks"), "the first unresolved field must skip submit"

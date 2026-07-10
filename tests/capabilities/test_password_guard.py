"""TK-136 AC3 — the deny-always password-fill guard in ``PlaywrightCapability`` (Q-114 rulings
(f)-(j)): before ANY fill, the shared ``_checked_fill`` helper reads the LIVE element's ``type``
attribute; ``type == "password"`` blocks the fill unconditionally.

``playwright`` rides the optional ``browser`` extra — this whole module SKIPS LOUDLY when it is
absent (module-level ``pytest.importorskip``) and again when Chromium itself cannot launch, the
same gating as ``tests/capabilities/test_playwright_actions.py`` (Q-113(b)).

A local stdlib ``ThreadingHTTPServer``-style fixture (``http.server.HTTPServer``, port 0,
``127.0.0.1``) serves a real HTML form with an ``<input type="password">`` — never the live
internet.

AC3(i): a ``type`` action targeting the password textbox returns the structured
``{"ok": False, "error": "password_field_blocked", ...}`` result and the value is NOT in the
field (proven via a real page evaluation of the input's ``.value``).
AC3(ii): a ``submit_form`` whose ``fields`` include the password textbox is blocked with NO
submit click (proven via the page never navigating to the distinct "thanks" page).
AC3(iii): bypass hunt — both fill call sites (``type`` and ``submit_form``) route through the
ONE shared ``_checked_fill`` helper, and no schema/arg path can disable it (``additionalProperties:
false`` at every level of ``BROWSER_INPUT_SCHEMA`` rejects any invented bypass argument).
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest

from wombat.capabilities import playwright_capability
from wombat.capabilities.playwright_capability import (
    BROWSER_INPUT_SCHEMA,
    BrowserSession,
    PlaywrightCapability,
)

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


_LOGIN_FORM_HTML = (
    b"<html><body>"
    b'<form action="/thanks" method="get">'
    b'<label for="nm">Your name</label>'
    b'<input id="nm" name="name" type="text" />'
    b'<label for="pw">Password</label>'
    b'<input id="pw" name="password" type="password" />'
    b'<button type="submit">Send</button>'
    b"</form>"
    b"</body></html>"
)

_THANKS_HTML = b"<html><body><h1>Thanks</h1></body></html>"


class _LoginFormHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.path.startswith("/thanks"):
            self.wfile.write(_THANKS_HTML)
        else:
            self.wfile.write(_LOGIN_FORM_HTML)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default stderr request logging


@pytest.fixture
def local_login_form_url() -> Iterator[str]:
    """Serve ``_LOGIN_FORM_HTML``/``_THANKS_HTML`` from a stdlib ``http.server`` bound to an
    OS-assigned port (port 0) — never the live internet."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _LoginFormHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def opened_capability(local_login_form_url: str) -> Any:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    page = await session.ensure_open()
    await page.goto(local_login_form_url)
    try:
        yield cap, session, local_login_form_url
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# AC3(i) — type action on the password textbox: blocked, value never lands
# ---------------------------------------------------------------------------


async def test_ac3i_type_action_on_password_field_is_blocked_and_value_never_lands(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, _url = opened_capability

    result = await cap.invoke(
        {"action": "type", "role": "textbox", "name": "Password", "value": "hunter2"}
    )

    assert result == {
        "ok": False,
        "error": "password_field_blocked",
        "role": "textbox",
        "name": "Password",
    }

    page = await session.ensure_open()
    live_value = await page.eval_on_selector("#pw", "el => el.value")
    assert live_value == "", "the guarded fill must never reach the live element"


# ---------------------------------------------------------------------------
# AC3(ii) — submit_form containing the password field: blocked, no submit click
# ---------------------------------------------------------------------------


async def test_ac3ii_submit_form_with_password_field_is_blocked_no_submit_click(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, url = opened_capability

    result = await cap.invoke(
        {
            "action": "submit_form",
            "url": url,
            "fields": [
                {"role": "textbox", "name": "Your name", "value": "Wombat"},
                {"role": "textbox", "name": "Password", "value": "hunter2"},
            ],
            "submit": {"role": "button", "name": "Send"},
        }
    )

    assert result == {
        "ok": False,
        "error": "password_field_blocked",
        "role": "textbox",
        "name": "Password",
    }

    page = await session.ensure_open()
    assert not page.url.startswith(f"{url}thanks"), "no submit click must mean no navigation"
    live_value = await page.eval_on_selector("#pw", "el => el.value")
    assert live_value == "", "the guarded fill must never reach the live element"


# ---------------------------------------------------------------------------
# AC3(iii) — bypass hunt: one shared guard, no way to disable it
# ---------------------------------------------------------------------------


async def test_ac3iii_both_fill_call_sites_route_through_the_one_shared_guard(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct proof there is exactly ONE guarded-fill call site both actions route through:
    patch ``_checked_fill`` to record every call, then exercise both the ``type`` action and
    ``submit_form``'s field loop and confirm each routes through it."""
    cap, _session, url = opened_capability
    calls: list[tuple[str, str]] = []
    real_checked_fill = playwright_capability._checked_fill

    async def _recording_checked_fill(
        locator: Any, role: str, name: str, value: str
    ) -> dict[str, Any] | None:
        calls.append((role, name))
        return await real_checked_fill(locator, role, name, value)

    monkeypatch.setattr(playwright_capability, "_checked_fill", _recording_checked_fill)

    await cap.invoke({"action": "type", "role": "textbox", "name": "Password", "value": "x"})
    assert ("textbox", "Password") in calls

    calls.clear()
    await cap.invoke(
        {
            "action": "submit_form",
            "url": url,
            "fields": [{"role": "textbox", "name": "Password", "value": "x"}],
            "submit": {"role": "button", "name": "Send"},
        }
    )
    assert ("textbox", "Password") in calls


def test_ac3iii_no_bypass_argument_exists_anywhere_in_the_schema() -> None:
    """No human-confirm token, or any other bypass argument, can ever reach ``invoke`` — every
    level of ``BROWSER_INPUT_SCHEMA`` sets ``additionalProperties: false`` (Q-114 ruling h)."""
    jsonschema.validate(
        {"action": "type", "role": "textbox", "name": "Password", "value": "x"},
        BROWSER_INPUT_SCHEMA,
    )

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "action": "type",
                "role": "textbox",
                "name": "Password",
                "value": "x",
                "human_confirm": True,
            },
            BROWSER_INPUT_SCHEMA,
        )

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "action": "submit_form",
                "url": "http://x.test",
                "fields": [
                    {
                        "role": "textbox",
                        "name": "Password",
                        "value": "x",
                        "human_confirm": True,
                    }
                ],
                "submit": {"role": "button", "name": "Send"},
            },
            BROWSER_INPUT_SCHEMA,
        )

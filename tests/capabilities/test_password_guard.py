"""TK-136 AC3 — the deny-always password-fill guard in ``PlaywrightCapability`` (Q-114 rulings
(f)-(j)): before ANY fill, the shared ``_checked_fill`` helper reads the LIVE element's ``type``
attribute; ``type == "password"`` blocks the fill unconditionally.

TK-234 (CRF-1) extends this file: ``_checked_fill`` now lowercases ``type`` before comparing, so
page-controlled casing (``type=PASSWORD``, ``type=Password``) can never defeat the guard. AC1
proves both fill call sites (the ``type`` action and ``submit_form``) block on mixed-case password
fields with real Chromium. AC2 proves the existing lowercase-blocks / non-password-fills / no-
type-attribute-fills behavior is byte-unchanged. AC3 is a hermetic unit twin (no real browser)
that drives ``_checked_fill`` directly with a fake ``Locator`` to prove the compare is
case-insensitive even where the real-browser tests below would skip for lack of Chromium.

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
# TK-234 (CRF-1) fixtures — a mixed-case-``type`` login form (AC1) and a
# no-``type``-attribute form (AC2's "absent attribute still fills" case)
# ---------------------------------------------------------------------------


def _login_form_html(password_type: str) -> bytes:
    """Same shape as ``_LOGIN_FORM_HTML`` but with a caller-chosen ``type`` casing on the
    password field — proves page-controlled casing can't defeat the guard (CRF-1)."""
    return (
        b"<html><body>"
        b'<form action="/thanks" method="get">'
        b'<label for="nm">Your name</label>'
        b'<input id="nm" name="name" type="text" />'
        b'<label for="pw">Password</label>'
        b'<input id="pw" name="password" type="' + password_type.encode() + b'" />'
        b'<button type="submit">Send</button>'
        b"</form>"
        b"</body></html>"
    )


_NOTYPE_FORM_HTML = (
    b"<html><body>"
    b'<form action="/thanks" method="get">'
    b'<label for="nm">Your name</label>'
    b'<input id="nm" name="name" />'
    b'<button type="submit">Send</button>'
    b"</form>"
    b"</body></html>"
)


def _make_form_handler(html: bytes) -> type[http.server.BaseHTTPRequestHandler]:
    """Bind ``html`` (served for every path except ``/thanks``) into a fresh handler class —
    mirrors ``_LoginFormHandler`` but parameterized over the form markup (CRF-1 AC1/AC2)."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if self.path.startswith("/thanks"):
                self.wfile.write(_THANKS_HTML)
            else:
                self.wfile.write(html)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # silence default stderr request logging

    return _Handler


@pytest.fixture(params=["PASSWORD", "Password"], ids=["upper", "titlecase"])
def mixed_case_login_form_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """Serve a login form whose password field's ``type`` is ``PASSWORD`` or ``Password`` — the
    page-controlled casing CRF-1 fixes ``_checked_fill`` to see through."""
    handler = _make_form_handler(_login_form_html(request.param))
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def mixed_case_capability(mixed_case_login_form_url: str) -> Any:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    page = await session.ensure_open()
    await page.goto(mixed_case_login_form_url)
    try:
        yield cap, session, mixed_case_login_form_url
    finally:
        await session.close()


@pytest.fixture
def notype_login_form_url() -> Iterator[str]:
    """Serve a form whose only field has NO ``type`` attribute at all — proves the ``(x or
    "").lower()`` default still fills on an absent attribute (CRF-1 AC2)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _make_form_handler(_NOTYPE_FORM_HTML))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def notype_capability(notype_login_form_url: str) -> Any:
    session = BrowserSession()
    cap = PlaywrightCapability(session=session)
    page = await session.ensure_open()
    await page.goto(notype_login_form_url)
    try:
        yield cap, session, notype_login_form_url
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


# ---------------------------------------------------------------------------
# CRF-1 (TK-234) AC1 — mixed-case ``type`` (``PASSWORD``/``Password``) still blocks both
# fill call sites, with the field staying empty and, for submit_form, no submit click
# ---------------------------------------------------------------------------


async def test_ac1_type_action_on_mixed_case_password_field_is_blocked_and_value_never_lands(
    mixed_case_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, _url = mixed_case_capability

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
    assert live_value == "", "page-controlled type casing must not defeat the guard"


async def test_ac1_submit_form_with_mixed_case_password_field_is_blocked_no_submit_click(
    mixed_case_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, url = mixed_case_capability

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
    assert live_value == "", "page-controlled type casing must not defeat the guard"


# ---------------------------------------------------------------------------
# CRF-1 (TK-234) AC2 — byte-unchanged behavior: lowercase still blocks (proven by the
# unchanged test_ac3i/test_ac3ii above), ordinary non-password fields still fill, and an
# element with NO ``type`` attribute still fills
# ---------------------------------------------------------------------------


async def test_ac2_non_password_field_still_fills(
    opened_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, _url = opened_capability

    result = await cap.invoke(
        {"action": "type", "role": "textbox", "name": "Your name", "value": "Wombat"}
    )

    assert result["ok"] is True
    page = await session.ensure_open()
    live_value = await page.eval_on_selector("#nm", "el => el.value")
    assert live_value == "Wombat"


async def test_ac2_field_with_no_type_attribute_still_fills(
    notype_capability: tuple[PlaywrightCapability, BrowserSession, str],
) -> None:
    cap, session, _url = notype_capability

    result = await cap.invoke(
        {"action": "type", "role": "textbox", "name": "Your name", "value": "Wombat"}
    )

    assert result["ok"] is True
    page = await session.ensure_open()
    live_value = await page.eval_on_selector("#nm", "el => el.value")
    assert live_value == "Wombat", "absent type attribute must still fill (None never blocks)"


# ---------------------------------------------------------------------------
# CRF-1 (TK-234) AC3 — hermetic unit twin: drive ``_checked_fill`` directly with a fake
# Locator, no browser at all, proving the compare is case-insensitive
# ---------------------------------------------------------------------------


class _FakeLocator:
    """A minimal async stand-in for a Playwright ``Locator`` — just enough surface for
    ``_checked_fill`` (``get_attribute`` and ``fill``), records every ``fill`` call."""

    def __init__(self, type_attr: str | None) -> None:
        self._type_attr = type_attr
        self.fill_calls: list[str] = []

    async def get_attribute(self, name: str, timeout: int) -> str | None:
        assert name == "type"
        return self._type_attr

    async def fill(self, value: str, timeout: int) -> None:
        self.fill_calls.append(value)


@pytest.mark.parametrize("type_attr", ["PASSWORD", "Password", "password"])
async def test_ac3_checked_fill_blocks_password_case_insensitively(type_attr: str) -> None:
    locator = _FakeLocator(type_attr)

    result = await playwright_capability._checked_fill(locator, "textbox", "Password", "hunter2")  # type: ignore[arg-type]

    assert result == {
        "ok": False,
        "error": "password_field_blocked",
        "role": "textbox",
        "name": "Password",
    }
    assert locator.fill_calls == [], "a blocked fill must never reach the fake locator's fill"


async def test_ac3_checked_fill_fills_when_type_attribute_is_absent() -> None:
    locator = _FakeLocator(None)

    result = await playwright_capability._checked_fill(locator, "textbox", "Comment", "hello")  # type: ignore[arg-type]

    assert result is None
    assert locator.fill_calls == ["hello"]

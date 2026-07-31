"""tests/integrations/test_screenpipe_client.py — ScreenpipeClient acceptance criteria
(TK-320, EP-37, DEC-70a/f/i).

  AC(a): a local fake screenpipe server (stdlib ``http.server``) returning well-formed and
      oversized results -> items parse into frozen ``ScreenpipeItem``, text capped at 400,
      list capped at 50: ``test_ac_a_*``.
  AC(b): connection refused, then a hanging socket past ``_TIMEOUT_S`` -> ``False``/``[]``
      every time, zero raises, AT MOST one WARNING per consecutive failure streak
      (caplog-counted), a success re-arms: ``test_ac_b_*``.
  AC(c): a non-loopback ``base_url`` -> one loud ERROR at construction, every read degrades,
      and NO network request is ever issued (a transport spy): ``test_ac_c_*``.
  AC(d): the module source, grepped/structurally checked — only the documented GET
      search/health surface exists, ``content_type=ocr`` only, no forbidden endpoint/verb
      strings: ``test_ac_d_*``.
  AC(e): ``WOMBAT_TEST_SCREENPIPE_LIVE`` unset loud-skips; set with a live screenpipe it
      round-trips health + one bounded search: ``test_ac_e_*``.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import logging
import os
import re
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest

import wombat.integrations.screenpipe.client as client_module
from wombat.integrations.screenpipe.client import ScreenpipeClient, ScreenpipeItem

_START = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
_END = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


def _ocr_item(app: str, title: str, text: str, ref_id: str, ts: datetime) -> dict[str, Any]:
    return {
        "type": "OCR",
        "content": {
            "app_name": app,
            "window_name": title,
            "text": text,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "frame_id": ref_id,
        },
    }


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class _FakeScreenpipeHandler(http.server.BaseHTTPRequestHandler):
    """Serves the fake screenpipe responses ``_FakeScreenpipeServer`` is configured with."""

    def log_message(self, format: str, *args: Any) -> None:
        return  # silence the default stderr access log

    def do_GET(self) -> None:
        server = cast("_FakeScreenpipeServer", self.server)
        parsed = urlparse(self.path)
        server.requested_paths.append(parsed.path)
        server.requested_queries.append(parse_qs(parsed.query))

        if parsed.path == "/health":
            body = b'{"status": "healthy"}'
        elif parsed.path == "/search":
            body = json.dumps({"data": server.search_items}).encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeScreenpipeServer(http.server.HTTPServer):
    """A real loopback HTTP server (stdlib) — ``search_items`` is mutated per-test."""

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _FakeScreenpipeHandler)
        self.search_items: list[dict[str, Any]] = []
        self.requested_paths: list[str] = []
        self.requested_queries: list[dict[str, list[str]]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


def _serve(server: _FakeScreenpipeServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server: _FakeScreenpipeServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=5.0)
    server.server_close()


@pytest.fixture
def fake_server() -> Iterator[_FakeScreenpipeServer]:
    server = _FakeScreenpipeServer()
    thread = _serve(server)
    try:
        yield server
    finally:
        _stop(server, thread)


def _unused_port() -> int:
    """Bind then immediately release a loopback port — nothing is listening there afterward,
    so a connection to it fails fast with ECONNREFUSED (AC(b) phase 1)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------------- AC(a) ---


def test_ac_a_items_parse_into_frozen_dataclass_with_correct_fields(
    fake_server: _FakeScreenpipeServer,
) -> None:
    fake_server.search_items = [_ocr_item("Chrome", "Some Title", "hello world", "frame-1", _START)]
    client = ScreenpipeClient(fake_server.base_url)

    items = client.search(_START, _END, app_name="Chrome", limit=10)

    assert items == [
        ScreenpipeItem(
            app="Chrome",
            title="Some Title",
            text_snippet="hello world",
            captured_at=_START,
            ref_id="frame-1",
        )
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        items[0].app = "Edge"  # type: ignore[misc]

    query = fake_server.requested_queries[-1]
    assert query["content_type"] == ["ocr"]
    assert query["app_name"] == ["Chrome"]
    assert query["limit"] == ["10"]


def test_ac_a_oversized_results_are_capped_at_max_results_and_max_text_chars(
    fake_server: _FakeScreenpipeServer,
) -> None:
    long_text = "x" * (client_module._MAX_TEXT_CHARS * 3)
    fake_server.search_items = [
        _ocr_item(f"App{i}", f"Title{i}", long_text, f"frame-{i}", _START)
        for i in range(client_module._MAX_RESULTS + 25)
    ]
    client = ScreenpipeClient(fake_server.base_url)

    items = client.search(_START, _END)

    assert len(items) == client_module._MAX_RESULTS
    for item in items:
        assert isinstance(item, ScreenpipeItem)
        assert len(item.text_snippet) == client_module._MAX_TEXT_CHARS

    query = fake_server.requested_queries[-1]
    assert query["content_type"] == ["ocr"]
    assert "app_name" not in query
    assert query["limit"] == [str(client_module._MAX_RESULTS)]


def test_ac_a_health_true_on_a_well_formed_server(fake_server: _FakeScreenpipeServer) -> None:
    client = ScreenpipeClient(fake_server.base_url)

    assert client.health() is True
    assert fake_server.requested_paths[-1] == "/health"


# --------------------------------------------------------------------------------- AC(b) ---


def test_ac_b_connection_refused_degrades_streak_suppressed_success_rearms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=client_module.logger.name)
    port = _unused_port()
    client = ScreenpipeClient(f"http://127.0.0.1:{port}")

    # phase 1: nobody listening -> connection refused, twice -> exactly ONE warning (the
    # consecutive-failure streak is suppressed after the first).
    assert client.health() is False
    assert client.search(_START, _END) == []
    assert len(_warnings(caplog)) == 1

    # phase 2: bring a real server up on the EXACT same port, same client instance -> success.
    server = _FakeScreenpipeServer(port=port)
    thread = _serve(server)
    try:
        assert client.health() is True
    finally:
        _stop(server, thread)

    # phase 3: down again -> the prior success re-armed the warning, so failing anew warns again.
    caplog.clear()
    assert client.health() is False
    assert len(_warnings(caplog)) == 1


def test_ac_b_hanging_socket_times_out_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=client_module.logger.name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        client = ScreenpipeClient(f"http://127.0.0.1:{port}")
        assert client.health() is False
        assert client.search(_START, _END) == []
    finally:
        sock.close()

    assert len(_warnings(caplog)) == 1


# --------------------------------------------------------------------------------- AC(c) ---


def test_ac_c_non_loopback_base_url_refused_and_never_issues_a_request(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.ERROR, logger=client_module.logger.name)
    calls: list[Any] = []

    def _spy(request: Any) -> Any:
        calls.append(request)
        raise AssertionError("a degraded ScreenpipeClient must never issue a request")

    monkeypatch.setattr(client_module, "_urlopen", _spy)

    client = ScreenpipeClient("http://example.com:3030")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1

    assert client.health() is False
    assert client.search(_START, _END) == []
    assert calls == []


# --------------------------------------------------------------------------------- AC(d) ---


def test_ac_d_structural_grep_over_the_package_source() -> None:
    package_dir = Path(client_module.__file__).parent
    sources = "\n".join(p.read_text(encoding="utf-8") for p in package_dir.glob("*.py"))

    assert '"/health"' in sources
    assert '"/search"' in sources
    assert '"ocr"' in sources

    forbidden_substrings = [
        "audio",
        "AUDIO",
        "/config",
        "/delete",
        "/write",
        '"PUT"',
        '"POST"',
        '"PATCH"',
        '"DELETE"',
    ]
    for token in forbidden_substrings:
        assert token not in sources, f"forbidden token {token!r} found in screenpipe package source"

    methods = re.findall(r'method\s*=\s*"([A-Z]+)"', sources)
    assert methods, "expected at least one explicit HTTP method literal"
    assert set(methods) <= {"GET"}


# --------------------------------------------------------------------------------- AC(e) ---

_LIVE_ENV = "WOMBAT_TEST_SCREENPIPE_LIVE"

_requires_live_screenpipe = pytest.mark.skipif(
    not os.environ.get(_LIVE_ENV),
    reason=(
        f"{_LIVE_ENV} is not set — skipping the live screenpipe round-trip smoke. Install and "
        f"run screenpipe locally (default http://127.0.0.1:3030), then export {_LIVE_ENV}=1 to "
        "exercise a real GET /health + GET /search. Operator-only (DEC-70i)."
    ),
)


@_requires_live_screenpipe
def test_ac_e_live_round_trips_health_and_one_bounded_search() -> None:
    client = ScreenpipeClient("http://127.0.0.1:3030")

    assert client.health() is True

    now = datetime.now(UTC)
    items = client.search(now - timedelta(hours=1), now, limit=5)

    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, ScreenpipeItem)

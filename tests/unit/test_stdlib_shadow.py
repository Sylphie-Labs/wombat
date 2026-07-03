"""TK-15 regression test — the stdlib `calendar` module must never be shadowed by
``tests/calendar/`` during collection.

Before this ticket, pytest's `pythonpath = ["tests"]` insertion bound `tests/calendar/`
(a wombat test package with no `tests/__init__.py` above it) under the bare top-level
name `calendar`, shadowing the stdlib module. `requests` (pulled in transitively by
google-auth's requests transport, TK-71) needs the REAL stdlib `calendar` for
`http.cookiejar`'s `from calendar import timegm`. If the shadow ever returns, importing
a google-auth transitive dependency and then `from calendar import timegm` will either
raise ImportError or resolve `calendar` to a module living under `tests/`, not the
Python stdlib — this test MUST fail in that case.
"""

from __future__ import annotations

import calendar
import sysconfig
from calendar import timegm

import requests  # noqa: F401 — google-auth transitive dependency (TK-71); import must not shadow stdlib calendar


def test_stdlib_calendar_is_not_shadowed_by_tests_calendar_package() -> None:
    assert callable(timegm)

    stdlib_dir = sysconfig.get_path("stdlib")
    assert calendar.__file__ is not None
    assert calendar.__file__.startswith(stdlib_dir), (
        f"calendar module resolved to {calendar.__file__!r}, expected it under the "
        f"Python stdlib directory {stdlib_dir!r} — tests/calendar/ is shadowing the "
        "stdlib calendar module again."
    )
    assert "tests" not in calendar.__file__.split("\\") and "tests" not in calendar.__file__.split(
        "/"
    )

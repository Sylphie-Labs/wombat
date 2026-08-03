"""tests/unit/test_device_biometric_ingest.py — TK-341 acceptance criteria — ``POST
/v1/biometrics`` closed-projection batch ingest (``devices.biometric_ingest.
BiometricIngestHandler``) into ``wombat_observations`` on channel ``'biometric'``
(``planning/design/wire-contract.md`` §3, R1, R3).

ALL tests here drive the batch through a REAL ``DeviceSurface`` over a REAL Postgres, and are
gated on ``WOMBAT_TEST_PG_DSN`` (the same convention as ``tests/behavior/test_event_log.py`` /
``tests/unit/test_observations.py``): absent it, tests are skipped LOUDLY, never faked, never
failed on a fresh clone.

    docker run --rm -d -p 5440:5432 -e POSTGRES_PASSWORD=wombat postgres:16
    WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5440/postgres

Socket-level HTTP is a hand-rolled minimal HTTP/1.1 client, mirroring
``tests/unit/test_device_voice_ingest.py`` / ``tests/unit/test_device_surface.py`` exactly — no
fastapi/uvicorn/httpx.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from wombat.devices.biometric_ingest import BiometricIngestHandler
from wombat.devices.credentials import DeviceCredentialStore
from wombat.devices.surface import DeviceSurface
from wombat.observations import ObservationStore, ensure_schema
from wombat.wipe import archive_and_wipe

_DSN = os.environ.get("WOMBAT_TEST_PG_DSN")

_requires_pg = pytest.mark.skipif(
    not _DSN,
    reason=(
        "WOMBAT_TEST_PG_DSN is not set — skipping biometric-ingest DB tests that require a "
        "real throwaway Postgres. Start one with:\n"
        "  docker run --rm -d -p 5440:5432 -e POSTGRES_PASSWORD=wombat postgres:16\n"
        "then export WOMBAT_TEST_PG_DSN=postgresql://postgres:wombat@localhost:5440/postgres"
    ),
)

_TZ = ZoneInfo("UTC")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACTIVITY_ENUM = {"walking", "running", "cycling", "strength", "swimming", "hiit", "yoga", "other"}

_T0 = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 8, 3, 23, 59, tzinfo=UTC)
# A window wide enough to catch every sample any test in this module writes, without relying on
# unbounded datetime.min/max (outside Postgres's timestamptz range on some builds).
_WIDE_START = datetime(2000, 1, 1, tzinfo=UTC)
_WIDE_END = datetime(2100, 1, 1, tzinfo=UTC)


@pytest.fixture
def clean_table() -> None:
    """Ensure the schema exists and the table is empty before each DB test — mirrors
    ``tests/unit/test_observations.py``'s ``fresh_table``/``tests/behavior/test_event_log.py``'s
    ``clean_table`` convention exactly."""
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE wombat_observations")
        conn.commit()


class _FakeDeviceVault:
    """Mirrors ``tests/unit/test_device_surface.py``'s own fake — unit tests never touch the
    real keyring."""

    def __init__(self) -> None:
        self._blob: str | None = None

    def load(self) -> str | None:
        return self._blob

    def save(self, blob: str) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


def _paired_store() -> tuple[DeviceCredentialStore, str, str]:
    store = DeviceCredentialStore(vault=_FakeDeviceVault())
    device_id, token = store.mint("iphone")
    return store, device_id, token


async def _http_request(
    host: str,
    port: int,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """Mirrors ``tests/unit/test_device_surface.py``'s own hand-rolled HTTP/1.1 client."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request_lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
        for name, value in (headers or {}).items():
            request_lines.append(f"{name}: {value}")
        request_lines.append(f"Content-Length: {len(body)}")
        request_lines.append("Connection: close")
        writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

        status_line = await reader.readline()
        status = int(status_line.decode("latin-1").split(" ")[1])

        response_headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            response_headers[name.strip().lower()] = value.strip()

        content_length = int(response_headers.get("content-length", "0") or "0")
        response_body = await reader.readexactly(content_length) if content_length else b""
        return status, response_headers, response_body
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _build_surface(handler: BiometricIngestHandler | None) -> tuple[DeviceSurface, str]:
    store, _device_id, token = _paired_store()
    surface = DeviceSurface(
        credential_store=store,
        host="127.0.0.1",
        port=0,
        remote_voice_enabled=False,
        biometrics_enabled=True,
        biometric_ingest_handler=handler,
    )
    return surface, token


async def _post_biometrics(
    surface: DeviceSurface, token: str, batch: dict[str, Any], *, raw_body: bytes | None = None
) -> tuple[int, dict[str, Any]]:
    body = raw_body if raw_body is not None else json.dumps(batch).encode("utf-8")
    await surface.start()
    host, port = surface.address
    try:
        status, _headers, response_body = await _http_request(
            host,
            port,
            method="POST",
            path="/v1/biometrics",
            headers={"X-Wombat-Device-Token": token, "Content-Type": "application/json"},
            body=body,
        )
    finally:
        await surface.stop()
    payload: dict[str, Any] = json.loads(response_body) if response_body else {}
    return status, payload


def _sample(
    kind: str, payload: dict[str, Any], *, started_at: datetime, ended_at: datetime
) -> dict[str, Any]:
    return {
        "kind": kind,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "payload": payload,
    }


def _full_kind_batch() -> dict[str, Any]:
    """One sample per v1 kind (§3.1) — the AC1 full-coverage batch."""
    return {
        "v": 1,
        "samples": [
            _sample(
                "sleep_session",
                {"asleep_minutes": 420, "in_bed_minutes": 450, "awakenings": 2},
                started_at=_T0,
                ended_at=_T1,
            ),
            _sample(
                "workout",
                {
                    "activity": "running",
                    "duration_seconds": 1800,
                    "active_energy_kcal": 320.5,
                    "avg_hr_bpm": 145,
                    "max_hr_bpm": 172,
                    "distance_meters": 5000.0,
                },
                started_at=_T0,
                ended_at=_T0 + timedelta(seconds=1800),
            ),
            _sample("resting_hr_daily", {"bpm": 54}, started_at=_T0, ended_at=_T1),
            _sample("hrv_daily", {"sdnn_ms": 42.3}, started_at=_T0, ended_at=_T1),
            _sample(
                "steps_hourly",
                {"steps": 843},
                started_at=_T0,
                ended_at=_T0 + timedelta(hours=1),
            ),
        ],
    }


# --------------------------------------------------------------------------------------- AC1


@_requires_pg
async def test_full_kind_batch_writes_one_row_per_sample_with_closed_payload(
    clean_table: None,
) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    status, payload = await _post_biometrics(surface, token, _full_kind_batch())

    assert status == 202
    assert payload == {"v": 1, "accepted": 5, "deduplicated": 0}

    rows = store.get_window("biometric", _WIDE_START, _WIDE_END)
    assert len(rows) == 5
    kinds = {row["kind"] for row in rows}
    assert kinds == {"sleep_session", "workout", "resting_hr_daily", "hrv_daily", "steps_hourly"}
    for row in rows:
        assert row["channel"] == "biometric"
        assert row["day_key"] == _T0.date()
        assert row["started_at"] is not None
        assert row["ended_at"] is not None
        for value in row["payload"].values():
            # AC1: the stored payload contains ONLY numbers and enum strings — no null, no
            # free text. A str value is permitted ONLY if it is a closed activity-enum member.
            assert isinstance(value, int | float | str)
            if isinstance(value, str):
                assert value in _ACTIVITY_ENUM


# --------------------------------------------------------------------------------------- AC2


@_requires_pg
async def test_free_text_field_is_rejected_and_writes_nothing(clean_table: None) -> None:
    """A free-text field anywhere in the body — here, a sample-level ``note`` — is a 4xx and
    writes ZERO rows. Named explicitly: 'no free text crosses the wire' is a test, not just a
    comment (§3: "There is no field into which a ... note can be placed")."""
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    sample = _sample("resting_hr_daily", {"bpm": 54}, started_at=_T0, ended_at=_T1)
    sample["note"] = "felt great on today's run, could use more sleep though"
    batch = {"v": 1, "samples": [sample]}

    status, _payload = await _post_biometrics(surface, token, batch)

    assert 400 <= status < 500
    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


@_requires_pg
async def test_unrecognized_payload_key_is_rejected_and_writes_nothing(clean_table: None) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    batch = {
        "v": 1,
        "samples": [
            _sample(
                "resting_hr_daily", {"bpm": 54, "extra_metric": 7}, started_at=_T0, ended_at=_T1
            )
        ],
    }

    status, _payload = await _post_biometrics(surface, token, batch)

    assert 400 <= status < 500
    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


@_requires_pg
async def test_activity_enum_value_outside_vocabulary_is_rejected_and_writes_nothing(
    clean_table: None,
) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    batch = {
        "v": 1,
        "samples": [
            _sample(
                "workout",
                {
                    "activity": "rock_climbing",  # not in the closed §3.2 enum
                    "duration_seconds": 1200,
                    "active_energy_kcal": 200,
                },
                started_at=_T0,
                ended_at=_T0 + timedelta(seconds=1200),
            )
        ],
    }

    status, _payload = await _post_biometrics(surface, token, batch)

    assert 400 <= status < 500
    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


@_requires_pg
async def test_nan_and_infinity_payload_values_are_rejected_and_write_nothing(
    clean_table: None,
) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    hrv_sample = _sample("hrv_daily", {"sdnn_ms": 1}, started_at=_T0, ended_at=_T1)
    nan_batch = {"v": 1, "samples": [hrv_sample]}
    nan_body = json.dumps(nan_batch).replace('"sdnn_ms": 1', '"sdnn_ms": NaN').encode("utf-8")
    status, _payload = await _post_biometrics(surface, token, nan_batch, raw_body=nan_body)
    assert 400 <= status < 500

    surface2, token2 = _build_surface(handler)
    inf_body = json.dumps(nan_batch).replace('"sdnn_ms": 1', '"sdnn_ms": Infinity').encode("utf-8")
    status2, _payload2 = await _post_biometrics(surface2, token2, nan_batch, raw_body=inf_body)
    assert 400 <= status2 < 500

    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


@_requires_pg
async def test_out_of_plausible_range_value_is_rejected_and_writes_nothing(
    clean_table: None,
) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    batch = {
        "v": 1,
        "samples": [
            # 999 is out of the pinned 20..250 plausible range for resting_hr_daily.bpm
            _sample("resting_hr_daily", {"bpm": 999}, started_at=_T0, ended_at=_T1)
        ],
    }

    status, _payload = await _post_biometrics(surface, token, batch)

    assert 400 <= status < 500
    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


@_requires_pg
async def test_batch_over_500_samples_is_rejected_and_writes_nothing(clean_table: None) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    batch = {
        "v": 1,
        "samples": [
            _sample("steps_hourly", {"steps": 1}, started_at=_T0, ended_at=_T0 + timedelta(hours=1))
            for _ in range(501)
        ],
    }

    status, _payload = await _post_biometrics(surface, token, batch)

    assert 400 <= status < 500
    assert store.get_window("biometric", _WIDE_START, _WIDE_END) == []


# --------------------------------------------------------------------------------------- AC3


@_requires_pg
async def test_identical_batch_posted_twice_leaves_row_count_unchanged(clean_table: None) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    batch = _full_kind_batch()

    surface1, token1 = _build_surface(handler)
    status1, payload1 = await _post_biometrics(surface1, token1, batch)
    rows_after_first = store.get_window("biometric", _WIDE_START, _WIDE_END)

    surface2, token2 = _build_surface(handler)
    status2, payload2 = await _post_biometrics(surface2, token2, batch)
    rows_after_second = store.get_window("biometric", _WIDE_START, _WIDE_END)

    assert status1 == 202
    assert status2 == 202
    assert payload1 == {"v": 1, "accepted": 5, "deduplicated": 0}
    assert payload2 == {"v": 1, "accepted": 5, "deduplicated": 5}
    assert len(rows_after_first) == 5
    assert len(rows_after_second) == len(rows_after_first)


# --------------------------------------------------------------------------------------- AC4


@_requires_pg
async def test_wipe_after_biometric_rows_archives_and_empties_wipe_py_byte_untouched(
    clean_table: None, tmp_path: Path
) -> None:
    assert _DSN is not None
    store = ObservationStore(_DSN)
    handler = BiometricIngestHandler(store=store, tz=_TZ)
    surface, token = _build_surface(handler)

    status, _payload = await _post_biometrics(surface, token, _full_kind_batch())
    assert status == 202
    rows_before = store.get_window("biometric", _WIDE_START, _WIDE_END)
    assert len(rows_before) == 5

    report = archive_and_wipe(_DSN, tmp_path)

    # AC4: the information_schema enumeration sweeps wombat_observations for free — nothing in
    # wipe.py needed to know 'biometric' rows exist for this to work.
    assert "wombat_observations" in report.truncated
    archived_rows = json.loads((tmp_path / "wombat_observations.json").read_text(encoding="utf-8"))
    archived_biometric_rows = [row for row in archived_rows if row["channel"] == "biometric"]
    assert len(archived_biometric_rows) == 5

    rows_after = store.get_window("biometric", _WIDE_START, _WIDE_END)
    assert rows_after == []

    diff = subprocess.run(
        ["git", "diff", "--", "src/wombat/wipe.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout == "", "src/wombat/wipe.py must stay byte-untouched by this ticket"

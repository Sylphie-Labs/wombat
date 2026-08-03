"""wombat.devices.biometric_ingest — BiometricIngestHandler: ``POST /v1/biometrics`` closed-
projection batch ingest (TK-341, R1, ``planning/design/wire-contract.md`` §3).

This is a passive body-data PIPE — no migration, no schema change. ``'biometric'`` is a new
``wombat_observations.channel`` VALUE (``observations.ObservationStore``, byte-untouched here),
not a schema change: every accepted sample lands as one ``append_segment(channel='biometric',
kind=..., started_at=..., ended_at=..., payload=..., day_key=...)`` call, ``day_key`` derived via
``domain.daily_ledger.wombat_today`` exactly as ``observe_screen.ScreenActivityCollector`` does
for the sibling ``'screen'`` channel.

§3's CLOSED-PAYLOAD invariant is the whole point of this module: every ``kind``, every payload
field name/type/plausible-range and the closed ``activity`` enum are taken FROM THE SPEC
(``_KIND_SCHEMAS`` below), never invented. **ANY** violation — an unrecognized ``kind``, an extra
or missing payload key, a wrong type, an out-of-range value, an enum string outside its
vocabulary, ``NaN``/``Infinity``, or free text anywhere in the request body — rejects the WHOLE
batch with ``400`` and writes ZERO rows; partial acceptance is structurally impossible because
every sample in the batch is validated before any ``append_segment`` call is made.

Idempotency (R3, §3.3) is SERVER-DERIVED, never a client field and never a stored column: the key
is ``sha256(kind, UTC-normalized started_at, UTC-normalized ended_at, canonical sorted-key
whitespace-free JSON payload)``. It is enforced by RE-DERIVING that same key from existing rows
read back via ``ObservationStore.get_window(channel='biometric', start=min(started_at) in batch,
end=max(started_at) in batch)`` — no new column, no new index, no idempotency field inside the
JSONB payload (a sha256 hex string is none of number/enum/timestamp and would itself violate the
closed-payload invariant this ticket exists to enforce) — plus de-duplication within the batch
itself.

``handle()`` is the SAME pure ``(device_id, headers, body) -> (status, json_body)`` seam
``voice_ingest.VoiceIngestHandler.handle`` establishes; ``DeviceSurface._dispatch`` calls it and
forwards the result to its own ``_respond``. ``max_body_bytes`` is a PUBLIC attribute for the
SAME reason: ``DeviceSurface._handle_connection`` reads it, for this one route, to decide how
many bytes to ``readexactly`` off the wire before dispatch (§3: 1 MiB body cap).

OUT OF SCOPE (v1, structural): no free text of any kind, no raw sample series, no GPS routes, no
consumption of these rows (nothing in this module reads them back for any purpose other than the
idempotency re-derivation above), no HealthKit write-back, no clinical interpretation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from wombat.domain.daily_ledger import wombat_today
from wombat.observations import ObservationStore

# §3: this route's OWN pinned caps.
MAX_BIOMETRIC_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
_MAX_BATCH_SAMPLES = 500

_CHANNEL = "biometric"

_ERROR_BAD_REQUEST: dict[str, object] = {"v": 1, "error": "bad_request"}

# §3.3: the field separator baked into the idempotency key's digest input — never a character
# that can appear inside a canonical-JSON payload string, so the four joined parts can never
# collide across a boundary.
_KEY_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class _FieldSpec:
    """One payload field's closed shape, taken verbatim from §3.1's table."""

    value_type: str  # "int" | "number" | "enum"
    lo: float = 0
    hi: float = 0
    nullable: bool = False


# §3.2: the closed activity enum, including its deliberate "other" catch-all (DEC-80(b)).
_ACTIVITY_ENUM: frozenset[str] = frozenset(
    {"walking", "running", "cycling", "strength", "swimming", "hiit", "yoga", "other"}
)

# §3.1: the closed v1 kind set (DEC-80(a)) and each kind's exact payload schema. Every numeric
# field name here already carries its unit suffix per §0's anti-drift rule — taken from the wire
# contract, never invented.
_KIND_SCHEMAS: dict[str, dict[str, _FieldSpec]] = {
    "sleep_session": {
        "asleep_minutes": _FieldSpec("int", 0, 1440),
        "in_bed_minutes": _FieldSpec("int", 0, 1440),
        "awakenings": _FieldSpec("int", 0, 200),
    },
    "workout": {
        "activity": _FieldSpec("enum"),
        "duration_seconds": _FieldSpec("int", 1, 86400),
        "active_energy_kcal": _FieldSpec("number", 0, 20000),
        "avg_hr_bpm": _FieldSpec("int", 20, 250, nullable=True),
        "max_hr_bpm": _FieldSpec("int", 20, 250, nullable=True),
        "distance_meters": _FieldSpec("number", 0, 500000, nullable=True),
    },
    "resting_hr_daily": {
        "bpm": _FieldSpec("int", 20, 250),
    },
    "hrv_daily": {
        "sdnn_ms": _FieldSpec("number", 1, 500),
    },
    "steps_hourly": {
        "steps": _FieldSpec("int", 0, 100000),
    },
}


def _reject_json_constant(constant: str) -> float:
    """``json.loads``'s ``parse_constant`` hook — the stdlib's non-standard extension otherwise
    happily parses the bare tokens ``NaN``/``Infinity``/``-Infinity`` anywhere in the document.
    §3: "``NaN`` and ``Infinity`` are rejected (they are not valid JSON numbers regardless)."
    Raising here turns any occurrence, at any depth, into the SAME ``ValueError`` the caller
    already catches for a malformed body."""
    msg = f"unsupported JSON constant: {constant}"
    raise ValueError(msg)


def _parse_declared_length(raw: str | None) -> int:
    """Mirrors ``voice_ingest._parse_declared_length`` exactly — the ORIGINAL ``Content-Length``
    header value, parsed defensively, never ``len(body)`` (``DeviceSurface`` already clamps the
    actual read to ``max_body_bytes``)."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _parse_timestamp(raw: Any) -> datetime | None:
    """§0: ISO-8601 with an explicit UTC offset or ``Z``. ``None`` on anything that is not a
    non-empty string, unparseable, or naive (no ``tzinfo``) — every one of those is a 400
    (mirrors ``voice_ingest._parse_captured_at``)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _validate_payload(kind: str, payload: Any) -> dict[str, Any] | None:
    """Validate ``payload`` against ``kind``'s closed schema and return a NORMALIZED dict —
    ``None`` on any violation. Normalization drops every nullable field that was omitted or sent
    explicitly as JSON ``null`` (never as an empty string, per §3.1): the AC1 invariant is that a
    STORED payload contains ONLY numbers, enum strings and timestamps, so a JSON ``null`` never
    survives into it either way it was spelled on the wire — this also makes the §3.3
    idempotency key stable regardless of which spelling a client used."""
    if not isinstance(payload, dict):
        return None
    schema = _KIND_SCHEMAS[kind]
    if set(payload.keys()) - set(schema.keys()):
        return None  # an extra key -- the closed-payload invariant, no free text can hide here

    normalized: dict[str, Any] = {}
    for field_name, spec in schema.items():
        if field_name not in payload or payload[field_name] is None:
            if spec.nullable:
                continue
            return None  # a required field missing, or explicitly null -- both a 400
        value = payload[field_name]
        if spec.value_type == "enum":
            if not isinstance(value, str) or value not in _ACTIVITY_ENUM:
                return None
            normalized[field_name] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if spec.value_type == "int" and not isinstance(value, int):
            return None
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None  # defensive: _reject_json_constant already refuses these at parse time
        if not (spec.lo <= value <= spec.hi):
            return None
        normalized[field_name] = value
    return normalized


def _validate_sample(sample: Any) -> dict[str, Any] | None:
    """Validate ONE ``{kind, started_at, ended_at, payload}`` sample -- the closed sample-level
    key set from §3's example envelope. Returns a dict with parsed ``started_at``/``ended_at``
    (``datetime``) and a normalized ``payload``, or ``None`` on any violation."""
    if not isinstance(sample, dict):
        return None
    if set(sample.keys()) != {"kind", "started_at", "ended_at", "payload"}:
        return None
    kind = sample["kind"]
    if not isinstance(kind, str) or kind not in _KIND_SCHEMAS:
        return None
    started_at = _parse_timestamp(sample["started_at"])
    ended_at = _parse_timestamp(sample["ended_at"])
    if started_at is None or ended_at is None:
        return None
    if started_at > ended_at:
        return None
    payload = _validate_payload(kind, sample["payload"])
    if payload is None:
        return None
    return {"kind": kind, "started_at": started_at, "ended_at": ended_at, "payload": payload}


def _validate_batch(data: Any) -> list[dict[str, Any]] | None:
    """Validate the WHOLE envelope + every sample -- ``None`` on any single violation anywhere in
    the batch (§3: "ANY violation rejects the WHOLE batch"). Never returns a partial list."""
    if not isinstance(data, dict) or set(data.keys()) != {"v", "samples"}:
        return None
    if data.get("v") != 1:
        return None
    samples = data.get("samples")
    if not isinstance(samples, list) or len(samples) > _MAX_BATCH_SAMPLES:
        return None
    normalized: list[dict[str, Any]] = []
    for sample in samples:
        validated = _validate_sample(sample)
        if validated is None:
            return None
        normalized.append(validated)
    return normalized


def _canonical_json(payload: dict[str, Any]) -> str:
    """§3.3's ``canonical_json``: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _idempotency_key(
    kind: str, started_at: datetime, ended_at: datetime, payload: dict[str, Any]
) -> str:
    """§3.3, R3: ``sha256(kind, UTC-normalized started_at, UTC-normalized ended_at,
    canonical_json(payload))``, joined on ``_KEY_SEPARATOR``. Applied identically to a freshly
    validated sample and to a row read back via ``ObservationStore.get_window`` -- the SAME
    function both directions is what makes the re-derivation in R3 actually work."""
    digest_input = _KEY_SEPARATOR.join(
        [
            kind,
            started_at.astimezone(UTC).isoformat(),
            ended_at.astimezone(UTC).isoformat(),
            _canonical_json(payload),
        ]
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


class BiometricIngestHandler:
    """``POST /v1/biometrics`` (TK-341, R1): constructed at the composition root ONLY when
    ``config.wombat_observe_biometrics`` is true, then injected into ``DeviceSurface`` as its
    optional ``biometric_ingest_handler`` kwarg -- a ``None`` handler leaves that route
    indistinguishable from an unknown path (the SAME 401 fallback, DEC-78(b))."""

    def __init__(
        self,
        *,
        store: ObservationStore,
        tz: ZoneInfo,
        max_body_bytes: int = MAX_BIOMETRIC_BODY_BYTES,
    ) -> None:
        self._store = store
        self._tz = tz
        self.max_body_bytes = max_body_bytes

    async def handle(
        self, *, device_id: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, object]]:
        """Validate and accept one batch. Never raises -- every rejection path returns a
        ``(400, ...)`` pair for ``DeviceSurface`` to send via its own ``_respond``; ZERO rows are
        ever written on a rejection (validated in full before the first ``append_segment``
        call)."""
        if _parse_declared_length(headers.get("content-length")) > self.max_body_bytes:
            return 400, _ERROR_BAD_REQUEST

        try:
            data = json.loads(body, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            return 400, _ERROR_BAD_REQUEST

        samples = _validate_batch(data)
        if samples is None:
            return 400, _ERROR_BAD_REQUEST

        accepted = len(samples)
        if accepted == 0:
            return 202, {"v": 1, "accepted": 0, "deduplicated": 0}

        existing_keys = self._existing_keys(samples)
        seen_in_batch: set[str] = set()
        deduplicated = 0
        for sample in samples:
            key = _idempotency_key(
                sample["kind"], sample["started_at"], sample["ended_at"], sample["payload"]
            )
            if key in existing_keys or key in seen_in_batch:
                deduplicated += 1
                continue
            seen_in_batch.add(key)
            day_key = wombat_today(sample["started_at"], self._tz)
            self._store.append_segment(
                _CHANNEL,
                sample["kind"],
                sample["started_at"],
                sample["ended_at"],
                sample["payload"],
                day_key,
            )

        return 202, {"v": 1, "accepted": accepted, "deduplicated": deduplicated}

    def _existing_keys(self, samples: list[dict[str, Any]]) -> set[str]:
        """R3: re-derive the idempotency key from EXISTING rows read back via ``get_window`` over
        ``[min(started_at), max(started_at)]`` across the batch -- never a stored key, never a
        second lookup structure."""
        starts = [sample["started_at"] for sample in samples]
        existing_rows = self._store.get_window(_CHANNEL, min(starts), max(starts))
        return {
            _idempotency_key(row["kind"], row["started_at"], row["ended_at"], row["payload"])
            for row in existing_rows
        }


__all__ = ["MAX_BIOMETRIC_BODY_BYTES", "BiometricIngestHandler"]

"""TK-75 build-time guard — the ``body_text`` payload key is referenced ONLY by sanctioned
modules (Q-65 ruling 3, the DEC-25/DEC-26 declared-guard pattern; mirrors
``tests/unit/test_wombat_params.py``'s AC4 ``_PRODUCTION_CONSUMER_PATHS`` scan style).

The Q-65 briefing text names the sanctioned allowlist as "the gmail producer
(``integrations/gmail/poller.py``, ``integrations/gmail/models.py``) and TK-148's
``body_provider`` wiring in ``src/wombat/safety/taint.py``". Reading the AS-BUILT
``src/wombat/safety/taint.py`` (TK-148, already landed) for this ticket showed that file does
NOT reference the ``body_text`` string as a dict key anywhere — ``BodyProvider`` is a generic
``Callable[[str], Awaitable[str]]`` seam that never touches a ``"body_text"`` literal in code
(only once, in a docstring sentence describing the seam in prose). The ACTUAL production
call site that reads the ``body_provider`` return value and re-keys it as ``"body_text"`` is
``src/wombat/stages/ingest_email_body.py`` (also TK-148, already landed) —
``email_body_ingested_to_artifact_data()`` builds ``{"message_id": ..., "body_text": ...}`` and
``IngestEmailBody.run()`` binds the dispatched body to a local ``body_text`` variable. That
file's own docstring explicitly frames itself as TK-75's downstream consumer ("FROZEN CONTRACT
for TK-75 ... the raw body is reachable ONLY through the injected ``body_provider`` behind
``read_email_body``").

Per the Q-65 briefing's own instruction ("if taint.py currently reads the body via a different
key/mechanism, FLAG it rather than guessing the allowlist"), this is exactly that case: the
literal allowlist in the briefing (poller.py + models.py + taint.py) is INSUFFICIENT to satisfy
"any other src/wombat reference fails the build" against the CURRENT, already-committed tree —
scanning with just that 3-file allowlist would fail this build on
``stages/ingest_email_body.py``, a file outside this ticket's ``files_in_scope`` that this
ticket must not edit. This guard therefore allowlists FOUR files: the two gmail producer
modules, ``safety/taint.py`` (the docstring mention, matching the briefing literally), and
``stages/ingest_email_body.py`` (the real, already-built consumer the briefing meant to name).
This discrepancy is called out explicitly in TK-75's completion report for a contract/
architect-ruling follow-up — this test file does not edit ``planning/contract.yaml``.

Q-84 (2026-07-09) sanctions a FIFTH path: ``integrations/gmail/task_extractor.py`` (TK-77) —
``extract_tasks`` reads ``item.body_text`` directly to do its regex/keyword extraction, so it is
added to the allowlist below as the ticket's own sanctioned in-scope guard edit.
"""

from __future__ import annotations

from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "wombat"

_BODY_TEXT_KEY = "body_text"

# The sanctioned allowlist (see module docstring for why this differs from the Q-65 briefing's
# literal 3-file list). Any OTHER file under src/wombat referencing "body_text" fails the build.
_SANCTIONED_PATHS = (
    _SRC_ROOT / "integrations" / "gmail" / "poller.py",
    _SRC_ROOT / "integrations" / "gmail" / "models.py",
    _SRC_ROOT / "safety" / "taint.py",
    _SRC_ROOT / "stages" / "ingest_email_body.py",
    _SRC_ROOT / "integrations" / "gmail" / "task_extractor.py",
)


def test_body_text_key_referenced_only_by_sanctioned_modules() -> None:
    """AST/text scan (DEC-25/DEC-26 declared-guard style): any ``src/wombat`` file outside the
    sanctioned allowlist that references the ``body_text`` literal fails the build."""
    sanctioned = set(_SANCTIONED_PATHS)
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if py in sanctioned:
            continue
        if _BODY_TEXT_KEY in py.read_text(encoding="utf-8"):
            offenders.append(str(py))
    assert not offenders, (
        f"'{_BODY_TEXT_KEY}' referenced outside the sanctioned gmail-body allowlist: {offenders}"
    )


def test_sanctioned_paths_all_exist_and_do_reference_the_key() -> None:
    """Guard-scope sanity: every allowlisted path exists and genuinely uses the key (an empty
    or stale allowlist entry would make the guard above pass vacuously without proving
    anything)."""
    for path in _SANCTIONED_PATHS:
        assert path.exists(), f"sanctioned guard path does not exist: {path}"
        assert _BODY_TEXT_KEY in path.read_text(encoding="utf-8"), (
            f"sanctioned path does not reference {_BODY_TEXT_KEY!r}: {path}"
        )


def test_gmail_producer_modules_are_the_body_text_source_of_truth() -> None:
    """Narrower, load-bearing check: the two gmail producer modules specifically are in the
    allowlist (independent of the taint.py/ingest_email_body.py discrepancy noted above)."""
    names = {p.name for p in _SANCTIONED_PATHS}
    assert "poller.py" in names
    assert "models.py" in names

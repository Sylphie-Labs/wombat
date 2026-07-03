"""TK-12 — canonical item identity acceptance criteria (EP-2, Q-18/D).

All tests are pure property tests over representative ``(source_id, source_natural_id)``
pairs — no concrete future source types (CalendarEventItem/GmailMessageItem/TaskItem
don't exist yet; see the ticket's scoping ruling). A "calendar item" is represented as
``("calendar", "evt_abc")`` and a "gmail item" as ``("gmail", "msg_xyz")``.

  AC1 idempotency_key(source_id, source_natural_id) is stable (same pair -> same key,
      every time) and collision-free (different pairs -> different keys), including
      against a delimiter-reassembly attack.
  AC2 an extracted-task's natural id (derive_task_natural_id) is DISTINCT from its parent
      message's natural id but the parent is always RECOVERABLE from it
      (parent_natural_id_of_task) — the linkability convention.
  AC3 two ItemRefs from different sources (a gmail-derived vs a calendar-derived "outcome
      claim subject") are distinguishable and each correctly attributes back to its own
      source_id/source_natural_id.
  AC4 source_id is part of the key: a calendar event and a Gmail message sharing the SAME
      raw natural-id string never collide.
"""

from __future__ import annotations

import pytest

from wombat.domain.item_identity import (
    ItemRef,
    derive_task_natural_id,
    idempotency_key,
    new_ephemeral_natural_id,
    parent_natural_id_of_task,
)

# ---------------------------------------------------------------------------
# AC1 — stable + collision-free
# ---------------------------------------------------------------------------


def test_idempotency_key_is_stable_for_the_same_pair() -> None:
    key_a = idempotency_key("calendar", "evt_abc")
    key_b = idempotency_key("calendar", "evt_abc")
    assert key_a == key_b


def test_idempotency_key_differs_for_different_source_items() -> None:
    calendar_key = idempotency_key("calendar", "evt_abc")
    gmail_key = idempotency_key("gmail", "msg_xyz")
    assert calendar_key != gmail_key


def test_idempotency_key_resists_a_delimiter_reassembly_attack() -> None:
    # A naive f"{source_id}:{source_natural_id}" join would let these two distinct pairs
    # reassemble into the same string. The length-prefix encoding must not.
    key_a = idempotency_key("a:b", "c")
    key_b = idempotency_key("a", "b:c")
    assert key_a != key_b


def test_item_ref_idempotency_key_matches_the_module_function() -> None:
    ref = ItemRef(source_id="calendar", source_natural_id="evt_abc")
    assert ref.idempotency_key() == idempotency_key("calendar", "evt_abc")


def test_idempotency_key_is_total_over_empty_strings() -> None:
    # The function must not raise for degenerate but legal string inputs.
    assert idempotency_key("", "") == idempotency_key("", "")
    assert idempotency_key("", "x") != idempotency_key("x", "")


# ---------------------------------------------------------------------------
# AC2 — extracted-task linkability
# ---------------------------------------------------------------------------


def test_task_natural_id_is_distinct_from_but_linkable_to_its_parent() -> None:
    parent_natural_id = "msg_xyz"
    parent_ref = ItemRef(source_id="gmail", source_natural_id=parent_natural_id)

    task_natural_id = derive_task_natural_id(parent_natural_id, "task_1")
    task_ref = ItemRef(source_id="gmail", source_natural_id=task_natural_id)

    # Distinct identity: the task never collides with its parent message.
    assert task_ref.idempotency_key() != parent_ref.idempotency_key()

    # Linkable: the parent's natural id is recoverable from the task's ItemRef.
    recovered_parent_natural_id = parent_natural_id_of_task(task_ref.source_natural_id)
    assert recovered_parent_natural_id == parent_natural_id


def test_two_tasks_from_the_same_parent_do_not_collide() -> None:
    parent_natural_id = "msg_xyz"
    task_a = derive_task_natural_id(parent_natural_id, "task_1")
    task_b = derive_task_natural_id(parent_natural_id, "task_2")
    assert idempotency_key("gmail", task_a) != idempotency_key("gmail", task_b)


def test_parent_natural_id_of_task_raises_on_an_unlinked_natural_id() -> None:
    with pytest.raises(ValueError):
        parent_natural_id_of_task("just_a_plain_message_id")


# ---------------------------------------------------------------------------
# AC3 — outcome attribution / distinguishability
# ---------------------------------------------------------------------------


def test_gmail_and_calendar_derived_claim_subjects_are_distinguishable() -> None:
    gmail_ref = ItemRef(source_id="gmail", source_natural_id="msg_1")
    calendar_ref = ItemRef(source_id="calendar", source_natural_id="evt_1")

    assert gmail_ref.idempotency_key() != calendar_ref.idempotency_key()


def test_claim_subject_attributes_back_to_its_source_and_natural_id() -> None:
    gmail_ref = ItemRef(source_id="gmail", source_natural_id="msg_1")
    calendar_ref = ItemRef(source_id="calendar", source_natural_id="evt_1")

    # Attribution: given only the ItemRef carried by an outcome claim, the source and
    # natural id it rates are directly (and correctly) recoverable.
    assert gmail_ref.source_id == "gmail"
    assert gmail_ref.source_natural_id == "msg_1"
    assert calendar_ref.source_id == "calendar"
    assert calendar_ref.source_natural_id == "evt_1"


# ---------------------------------------------------------------------------
# AC4 — one derivation, source_id is part of the key
# ---------------------------------------------------------------------------


def test_shared_raw_natural_id_does_not_collide_across_sources() -> None:
    assert idempotency_key("calendar", "123") != idempotency_key("gmail", "123")


def test_shared_raw_natural_id_does_not_collide_via_item_ref() -> None:
    calendar_ref = ItemRef(source_id="calendar", source_natural_id="123")
    gmail_ref = ItemRef(source_id="gmail", source_natural_id="123")
    assert calendar_ref.idempotency_key() != gmail_ref.idempotency_key()


# ---------------------------------------------------------------------------
# Ephemeral sources (presence/asr) — totality via a generated natural id
# ---------------------------------------------------------------------------


def test_new_ephemeral_natural_id_is_a_nonempty_string() -> None:
    natural_id = new_ephemeral_natural_id()
    assert isinstance(natural_id, str)
    assert natural_id != ""


def test_new_ephemeral_natural_id_is_fresh_each_call() -> None:
    # Not asserting on a specific (random) value — only that repeated calls don't
    # collide, which is what "generated" is for.
    assert new_ephemeral_natural_id() != new_ephemeral_natural_id()


def test_ephemeral_natural_id_is_injected_not_asserted_in_tests() -> None:
    # Callers that need a deterministic test key pass an explicit natural id rather than
    # relying on new_ephemeral_natural_id()'s output — this pins that convention.
    fixed_natural_id = "presence-fixture-1"
    ref = ItemRef(source_id="presence", source_natural_id=fixed_natural_id)
    assert ref.idempotency_key() == idempotency_key("presence", fixed_natural_id)

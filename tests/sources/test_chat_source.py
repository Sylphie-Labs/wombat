"""TK-222 — ChatSource acceptance criteria (EP-32, Q-110(d) ruling 1).

``ChatSource`` is a bare ``PushSource`` (TK-161) registered under id ``"chat"`` — this module
proves exactly that shape (no behavior beyond what ``PushSource`` already provides) plus the
registration-not-rewrite (DEC-5) + structural no-model-import guarantees the briefing calls out.
"""

from __future__ import annotations

import ast
from pathlib import Path

from wombat.domain.item_identity import idempotency_key as derive_key
from wombat.queue import EnqueueResult, QueueItem
from wombat.sources.base import InputSource, PushSource, SourceEvent
from wombat.sources.chat_source import CHAT_POLL_INTERVAL_SECONDS, CHAT_SOURCE_ID, ChatSource
from wombat.sources.registry import SourceRegistry

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "wombat"
_CHAT_SOURCE_PATH = _SRC_ROOT / "sources" / "chat_source.py"

# CON-1 (Q-110(d)): the chat source module may never import a model/compose/mouth module — the
# mouth never sees a correlation id, and the source has no business reaching into the mouth.
_FORBIDDEN_IMPORT_PREFIXES = (
    "openai",
    "httpx",
    "requests",
    "cogworx.model",
    "wombat.compose",
    "wombat.stages.compose",
)


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def enqueue(self, item: QueueItem) -> EnqueueResult:
        self.items.append(item)
        return EnqueueResult.QUEUED


def _imported_module_names(source: str) -> set[str]:
    """AST-based import scan (mirrors ``tests/integrations/gmail/test_task_extractor.py``):
    every module name this source ``import``s or ``from``-imports, absolute (level-0) only."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


def test_chat_source_is_a_push_source_with_the_documented_id_and_cadence() -> None:
    source = ChatSource()

    assert isinstance(source, PushSource)
    assert source.id == "chat" == CHAT_SOURCE_ID
    assert source.poll_interval_seconds == 1.0 == CHAT_POLL_INTERVAL_SECONDS


def test_chat_source_satisfies_the_input_source_protocol() -> None:
    source: InputSource = ChatSource()
    assert source.id == "chat"


async def test_pushed_event_enqueues_via_the_registry_with_the_canonical_idempotency_key() -> (
    None
):
    """Registration-not-rewrite (DEC-5): ChatSource rides the EXACT SAME registry/poll/enqueue
    path as every other PushSource — no chat-specific branch anywhere in that path."""
    enqueuer = _FakeEnqueuer()
    registry = SourceRegistry(enqueuer)
    source = ChatSource()
    registry.register(source)

    source.push(SourceEvent(event_key="ek-1", payload={"item_kind": "chat", "text": "hi"}))

    drained = await source.poll()
    assert [e.event_key for e in drained] == ["ek-1"]

    # Mirrors what the registry itself would do on a poll tick (tests/sources/test_push_source.py
    # AC1 precedent) — proving the SAME canonical derivation the surface pre-computes against.
    key = derive_key(source.id, "ek-1")
    assert key == derive_key("chat", "ek-1")


async def test_poll_fires_wake_exactly_once_when_events_drained() -> None:
    """TK-269 (DEC-56a): a poll that drains >=1 event fires ``source.wake`` exactly once, AFTER
    the drain (the module's atomicity argument relies on the call happening inside ``poll()``,
    not after)."""
    source = ChatSource()
    calls = 0

    def _wake() -> None:
        nonlocal calls
        calls += 1

    source.wake = _wake
    source.push(SourceEvent(event_key="ek-1", payload={}))
    source.push(SourceEvent(event_key="ek-2", payload={}))

    drained = await source.poll()

    assert [e.event_key for e in drained] == ["ek-1", "ek-2"]
    assert calls == 1  # one poll, one wake — never one-per-event


async def test_poll_does_not_fire_wake_when_nothing_drained() -> None:
    """An empty poll (nothing pushed since the last one) must not fire the wake — AC3's spurious-
    wake avoidance starts here, at the source."""
    source = ChatSource()
    calls = 0

    def _wake() -> None:
        nonlocal calls
        calls += 1

    source.wake = _wake

    drained = await source.poll()

    assert drained == []
    assert calls == 0


async def test_poll_with_no_wake_configured_behaves_like_plain_push_source() -> None:
    """Default ``wake=None`` (unwired, e.g. a chat-disabled boot) — polling still drains events
    exactly like today, just without firing anything."""
    source = ChatSource()
    assert source.wake is None

    source.push(SourceEvent(event_key="ek-1", payload={}))
    drained = await source.poll()

    assert [e.event_key for e in drained] == ["ek-1"]


def test_chat_source_default_context_hook_is_none() -> None:
    """TK-296 (DEC-65f): the default -- unwired boot -- context_hook is None, matching the wake
    attribute's own default-None precedent."""
    source = ChatSource()
    assert source.context_hook is None


def test_chat_source_accepts_a_context_hook_ctor_kwarg_and_holds_it_publicly() -> None:
    def _hook() -> dict[str, str]:
        return {"known_user_context": "Likes coffee"}

    source = ChatSource(context_hook=_hook)
    assert source.context_hook is _hook
    assert source.context_hook() == {"known_user_context": "Likes coffee"}


def test_chat_source_module_imports_no_model_compose_or_mouth_module() -> None:
    """Structural CON-1 guard: the source module itself never reaches toward the mouth."""
    imported = _imported_module_names(_CHAT_SOURCE_PATH.read_text(encoding="utf-8"))
    offenders = [
        name
        for name in imported
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _FORBIDDEN_IMPORT_PREFIXES
        )
    ]
    assert not offenders, f"chat_source.py imports forbidden module(s): {offenders}"

"""TK-1 — wombat composition root acceptance criteria."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from wombat import bootstrap
from wombat.bootstrap import MODEL_PROFILE, build_engine, reset_engine
from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.params import load_operating_params
from wombat.substrate import cold_boot_bundle

# The ten seams the Engine must carry after composition (4 required substrate + 6 optional).
_ENGINE_SEAMS = (
    "_models",
    "_journal",
    "_graph_store",
    "_latent",
    "_pathways",
    "_budget_policy",
    "_registry",
    "_recall_stack",
    "_personality",
    "_rules",
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    reset_engine()
    yield
    reset_engine()


def _config() -> WombatConfig:
    return WombatConfig(deepseek_api_key="sk-test", deepseek_base_url="https://api.deepseek.com")


def test_ac1_cold_launch_returns_engine_with_all_ten_seams() -> None:
    engine = build_engine(cold_boot_bundle(), config=_config())
    for seam in _ENGINE_SEAMS:
        assert getattr(engine, seam) is not None, f"seam {seam} is None"
    assert engine._model_profile == MODEL_PROFILE


def test_ac2_missing_api_key_raises_configuration_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        load_config()


def test_ac2_missing_base_url_raises_configuration_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DEEPSEEK_BASE_URL"):
        load_config()


def test_ac3_second_call_returns_same_singleton_no_duplicate() -> None:
    first = build_engine(cold_boot_bundle(), config=_config())
    second = build_engine(cold_boot_bundle(), config=_config())
    assert first is second


def test_deepseek_profile_registered_as_spec_no_model_built() -> None:
    # The model is a descriptor only — composition stays model-silent (registry resolves the spec).
    engine = build_engine(cold_boot_bundle(), config=_config())
    registry = engine._models
    assert registry.resolve_spec(MODEL_PROFILE) is not None


def test_module_exposes_build_engine() -> None:
    assert callable(bootstrap.build_engine)


# --- TK-101: WOMBAT_BRIEF_PATH / WOMBAT_VOICE_ENABLED are OPTIONAL -------------------------------


def test_wombat_config_boots_without_brief_path_or_voice_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WOMBAT_BRIEF_PATH", raising=False)
    monkeypatch.delenv("WOMBAT_VOICE_ENABLED", raising=False)
    config = _config()  # must not raise -- neither is in REQUIRED_ENV
    assert config.wombat_brief_path is None
    assert config.wombat_voice_enabled is False


# --- TK-172 (CR-10): the mid-batch-surface/whole-batch-ack coupling guard -----------------------


def test_guard_drain_batch_size_raises_for_non_one() -> None:
    with pytest.raises(ValueError, match="mid-batch"):
        bootstrap._guard_drain_batch_size(2)


def test_guard_drain_batch_size_noop_for_one() -> None:
    bootstrap._guard_drain_batch_size(1)  # must not raise


def test_assemble_runtime_still_succeeds_at_current_batch_size_of_one() -> None:
    """AC1: the guard is a no-op at the current composition (_DRAIN_BATCH_SIZE == 1) -- assembly
    is byte-identical, no new raise on the real boot path."""
    op = load_operating_params()
    # A fake Postgres DSN -- every adapter assemble_runtime wires is lazy (no connection at
    # construction), so this never touches a real Postgres (mirrors tests/unit/test_runtime.py).
    bundle = bootstrap.assemble_runtime(
        config=_config(), dsn="postgresql://fake-host/fake-db", params=op
    )
    assert bundle.drain_pathway_id == bootstrap.DRAIN_PATHWAY_ID

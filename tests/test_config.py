from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from core.config import DomainSettings, ModelSpec, Settings


class _Demo(DomainSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", env_file=None)

    scratch_url: str = "http://scratch"
    lg_url: str = "http://lg"
    model: ModelSpec = "ollama:llama3.1:8b"


def test_domain_settings_defaults() -> None:
    demo = _Demo()
    assert demo.max_attempts == 3
    assert demo.max_steps == 25


def test_domain_settings_read_their_own_prefix(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("MAX_ATTEMPTS", "9")
    assert _Demo().max_attempts == 5


@pytest.mark.parametrize("spec", ["nonsense", "unknown_provider:some-model", "anthropic:"])
def test_invalid_model_spec_raises(spec: str) -> None:
    with pytest.raises(ValidationError):
        _Demo(model=spec)


def test_model_names_may_contain_colons() -> None:
    assert _Demo(model="ollama:llama3.1:8b").model == "ollama:llama3.1:8b"


def test_blank_values_mean_unset() -> None:
    harness = Settings(_env_file=None, domain="  ", runs_dir="")
    assert harness.domain is None
    assert harness.runs_dir is None

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings, settings


def test_max_attempts_default() -> None:
    assert settings.max_attempts == 3


def test_invalid_model_spec_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(sql_model="nonsense")


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(schema_model="unknown_provider:some-model")
